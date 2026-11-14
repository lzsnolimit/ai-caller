"""API-owned selected-phone lifecycle; native controller scope stays unchanged."""
import asyncio
import contextlib

from bumble import pairing, hci
from bumble.device import Device, DeviceConfiguration

from server.common.contracts import GatewayError, address
from server.common.storage import Store
from .adapter import MacPhoneAdapter, MacSCOAudio
from .native.acipc_pair import run, SelectedKeyStore
from .native.acipc_stack import NativeTransport, NoFirmwareHost

# 远端名称请求的上限；超时或失败只表示名称未知，不影响连接和通话。
NAME_TIMEOUT = 10


def native_released(transport):
    """True when the native bridge pump has stopped; the next connect may start it again."""
    if transport is None:
        return True
    pump = getattr(transport, 'pump_task', None)
    return not getattr(transport, 'running', False) and (pump is None or pump.done())


class MacService:
    mac_only = True
    def __init__(self, store=None):
        self.store = store or Store()
        self.known = self.store.load("devices", "mac", [])
        self.controllers = self.store.load("devices", "mac-controllers", {})
        # 手机报告的蓝牙名称，按地址保存在本机设备配置中；不写诊断日志或会话记录。
        self.names = self.store.load("devices", "mac-names", {})
        self.adapter = None
        self.connection_task = None
        self.stop_link = None
        self.lock = asyncio.Lock()
        self.pairings = None
        # cleanup_failed 只表示本地 native 资源确实没有释放；链路清理失败或手机电话状态未知不算。
        self.cleanup_failed = False
        self.release_error = None
        self.connection_epoch = 0
        self.on_change = lambda: None

    async def start(self):
        pass

    async def capabilities(self):
        base = await self.adapter.capabilities() if self.adapter else {"platform": "macOS", "nativeTelephony": False, "phones": []}
        base.update(pairing=True, discovery=True)
        return base

    async def devices(self):
        live = self.adapter.address if self.adapter else None
        return [{"address": item, "paired": True, "connected": item == live,
                 "hfp": item == live, "name": self.names.get(item)} for item in self.known]

    async def learn_name(self, selected, device, connection, transport):
        try:
            name = await transport.guarded(device.request_remote_name(connection), NAME_TIMEOUT)
            name = name.strip("\x00").strip() if isinstance(name, str) else ""
            if not name or self.names.get(selected) == name:
                return
            names = {**self.names, selected: name}
            self.store.save("devices", "mac-names", names)
            self.names = names
        except Exception:
            return
        self.on_change()

    async def discover(self):
        async with self.lock:
            if self.connection_task:
                raise GatewayError("disconnect_before_scan", 409)
            if self.cleanup_failed:
                raise GatewayError("controller_cleanup_failed", 503)
            transport = NativeTransport()
            device = Device(config=DeviceConfiguration(name="AI-Phone", le_enabled=False,
                classic_enabled=True, classic_smp_enabled=False, classic_accept_any=False,
                connectable=False, discoverable=False, keystore=None),
                host=NoFirmwareHost(controller_source=transport, controller_sink=transport))
            results = set()
            device.on(device.EVENT_INQUIRY_RESULT,
                      lambda found, *_: results.add(found.to_string(with_type_qualifier=False)))
            done = asyncio.Event()
            device.once(device.EVENT_INQUIRY_COMPLETE, done.set)
            pending = False
            try:
                await transport.start()
                await transport.guarded(device.power_on(), 15)
                await transport.guarded(device.send_sync_command(hci.HCI_Write_Inquiry_Mode_Command(
                    inquiry_mode=hci.HCI_EXTENDED_INQUIRY_MODE)), 3)
                device.auto_restart_inquiry = False
                pending = True
                await transport.guarded(device.send_async_command(hci.HCI_Inquiry_Command(
                    lap=hci.HCI_GENERAL_INQUIRY_LAP, inquiry_length=4, num_responses=0)), 3)
                await transport.guarded(done.wait(), 7)
                pending = False
                return [{"address": address(item)} for item in sorted(results)]
            finally:
                try:
                    if pending and not transport.failure:
                        await transport.guarded(device.send_sync_command(hci.HCI_Inquiry_Cancel_Command()), 2)
                except BaseException:
                    self.cleanup_failed = True
                    raise
                finally:
                    try:
                        await transport.stop()
                    except BaseException:
                        self.cleanup_failed = True
                        raise

    async def pair(self, selected, key):
        return await self._connect(selected, key)

    async def _connect(self, selected, key=None):
        selected = address(selected)
        async with self.lock:
            if self.cleanup_failed:
                raise GatewayError("controller_cleanup_failed", 503)
            if self.connection_task:
                if self.adapter and self.adapter.address == selected:
                    return
                if self.adapter is None:
                    # 连接已丢失，但 native 资源仍在释放中：如实报告，不当作另一目标占用。
                    raise GatewayError("controller_releasing", 503)
                raise GatewayError("controller_busy", 409)
            if key is None and selected not in self.known:
                raise GatewayError("pairing_required", 409)
            ready = asyncio.get_running_loop().create_future()
            self.stop_link = asyncio.Event()
            self.connection_epoch += 1
            epoch = self.connection_epoch
            owner = self
            live = {}

            class Delegate(pairing.PairingDelegate):
                def __init__(self):
                    super().__init__(io_capability=self.DISPLAY_OUTPUT_AND_YES_NO_INPUT)
                async def confirm(self, auto=False):
                    return False
                async def compare_numbers(self, number, digits):
                    if key is None:
                        return False
                    return await owner.pairings.ask(key, selected, "confirmation", f"{number:0{digits}d}")

            async def serve(device, connection, protocol, transport):
                live['transport'] = transport
                self.adapter = MacPhoneAdapter(selected, device, connection, protocol, transport, epoch)
                self.on_change()
                try:
                    self.controllers[selected] = str(device.public_address)
                    self.store.save("devices", "mac-controllers", self.controllers)
                    if selected not in self.known:
                        updated = [*self.known, selected]
                        self.store.save("devices", "mac", updated)
                        self.known = updated
                    ready.set_result(True)
                    # 名称请求放在后台：不阻塞 HFP 建立，链路结束时一并取消。
                    naming = asyncio.create_task(self.learn_name(selected, device, connection, transport))
                    try:
                        await transport.guarded(self.stop_link.wait(), float("inf"))
                    finally:
                        naming.cancel()
                        await asyncio.gather(naming, return_exceptions=True)
                finally:
                    await self.adapter.close()
                    self.adapter = None
                    self.on_change()

            async def connect():
                try:
                    await run(selected, serve, Delegate())
                except BaseException as error:
                    if not ready.done():
                        ready.set_exception(GatewayError("connection_failed", 503))
                    elif not isinstance(error, asyncio.CancelledError):
                        # 链路清理或手机电话收尾失败，与 native 资源是否释放分开判断。
                        self.release_error = type(error).__name__
                        if not native_released(live.get('transport')):
                            self.cleanup_failed = True
                finally:
                    self.adapter = None
                    self.connection_task = None
                    self.on_change()
            self.connection_task = asyncio.create_task(connect())
        try:
            await asyncio.shield(ready)
        except BaseException:
            await self._disconnect()
            if ready.done() and not ready.cancelled():
                with contextlib.suppress(Exception):
                    ready.exception()
            raise

    async def _disconnect(self):
        task = self.connection_task
        if not task:
            return
        self.stop_link.set()
        if self.adapter is None:
            task.cancel()
        await task

    async def connect(self, selected, connected):
        if connected:
            return await self._connect(selected)
        await self.device_path(selected)
        await self._disconnect()

    async def remove(self, selected):
        selected = address(selected)
        if self.adapter and self.adapter.address == selected:
            raise GatewayError("disconnect_before_remove", 409)
        if selected not in self.known:
            raise GatewayError("device_not_found", 404)
        controller = self.controllers.get(selected)
        if not controller:
            raise GatewayError("private_controller_identity_missing", 503)
        await SelectedKeyStore(hci.Address(controller), hci.Address(selected, hci.Address.PUBLIC_DEVICE_ADDRESS), self.store).delete()
        updated = [item for item in self.known if item != selected]
        self.store.save("devices", "mac", updated)
        self.known = updated
        if selected in self.names:
            names = {key: value for key, value in self.names.items() if key != selected}
            self.store.save("devices", "mac-names", names)
            self.names = names

    async def device_path(self, selected):
        if not self.adapter:
            raise GatewayError("device_disconnected", 409)
        return await self.adapter.device_path(selected)

    async def calls(self, selected):
        await self.device_path(selected)
        return await self.adapter.calls(selected)

    async def operate(self, selected, *args, **kwargs):
        await self.device_path(selected)
        return await self.adapter.operate(selected, *args, **kwargs)

    async def call_identity(self, selected, call_id):
        await self.device_path(selected)
        return await self.adapter.call_identity(selected, call_id)

    async def peer_number(self, selected, call_id):
        await self.device_path(selected)
        return await self.adapter.peer_number(selected, call_id)

    def subscribe_calls(self, selected, callback):
        if not self.adapter:
            raise GatewayError('device_disconnected', 409)
        return self.adapter.subscribe_calls(selected, callback)

    async def call_snapshot(self, selected, call_id, identity):
        await self.device_path(selected)
        return await self.adapter.call_snapshot(selected, call_id, identity)

    async def hangup_owned(self, selected, call_id, identity):
        await self.device_path(selected)
        return await self.adapter.hangup_owned(selected, call_id, identity)

    def audio(self, selected):
        if not self.adapter:
            raise GatewayError("device_disconnected", 409)
        return MacSCOAudio(selected, self.adapter)

    async def close(self):
        await self._disconnect()
