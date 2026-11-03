"""Resolve BlueZ and PipeWire objects by their actual address properties."""
import asyncio
import re
import xml.etree.ElementTree as ET

BLUEZ = "org.bluez"
TELEPHONY = "org.pipewire.Telephony"
ROOT = "/org/pipewire/Telephony"
DEVICE = "org.bluez.Device1"
GATEWAY = "org.pipewire.Telephony.AudioGateway1"
CALL = "org.pipewire.Telephony.Call1"
MANAGER = "org.freedesktop.DBus.ObjectManager"


from server.common.contracts import GatewayError, address


def plain(value):
    if hasattr(value, "value"):
        return plain(value.value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def device_name(props):
    """手机的蓝牙名称：优先 BlueZ Alias，其次 Name；未知为 None。

    设备没有名称时 BlueZ 用地址（以 - 分隔）作为 Alias，这不是名称。"""
    placeholder = str(props.get("Address", "")).replace(":", "-").upper()
    for key in ("Alias", "Name"):
        value = props.get(key)
        if isinstance(value, str) and value.strip() and value.strip().upper() != placeholder:
            return value.strip()
    return None


class DBusPhoneAdapter:
    # 接听结果以 Call1.State 为准，不以 D-Bus 回复为准。
    ANSWER_SECONDS = 5

    def __init__(self, system=None, session=None):
        self.system, self.session = system, session
        self.pairings = None
        self.agent = None
        self.call_generations = {}
        self.identity_tracking = False
        self.call_listeners = {}
        self.watch_gateways = {}
        self.on_change = lambda: None

    async def start(self):
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType
        # Keep the server available to report missing host capabilities.
        for field, bus_type in (("system", BusType.SYSTEM), ("session", BusType.SESSION)):
            if getattr(self, field) is None:
                try:
                    setattr(self, field, await asyncio.wait_for(MessageBus(bus_type=bus_type).connect(), 5))
                except Exception:
                    pass
        if self.session:
            try:
                match = "type='signal',sender='org.pipewire.Telephony',interface='org.freedesktop.DBus.ObjectManager'"
                await self._call(self.session, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                 "org.freedesktop.DBus", "AddMatch", "s", [match])
                await self._call(self.session, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                 "org.freedesktop.DBus", "AddMatch", "s",
                                 ["type='signal',sender='org.pipewire.Telephony',interface='org.freedesktop.DBus.Properties'"])
                self.session.add_message_handler(self._call_generation)
                self.identity_tracking = True
            except Exception:
                self.identity_tracking = False
        if self.system:
            try:
                await self._call(self.system, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                                 'org.freedesktop.DBus', 'AddMatch', 's',
                                 ["type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties'"])
                self.system.add_message_handler(self._device_change)
            except GatewayError:
                pass

    def _device_change(self, message):
        if message.interface == 'org.freedesktop.DBus.Properties' and message.member == 'PropertiesChanged' and message.body and message.body[0] == DEVICE:
            self.on_change()

    def _call_generation(self, message):
        body = message.body
        if message.interface == MANAGER and message.member in ('InterfacesAdded', 'InterfacesRemoved') and len(body) >= 2 and CALL in body[1]:
            path = body[0]
            self.call_generations[path] = self.call_generations.get(path, 0) + 1
        elif message.interface == 'org.freedesktop.DBus.Properties' and message.member == 'PropertiesChanged' and body and body[0] == CALL:
            path = message.path
        else:
            return
        self.on_change()
        for selected, callbacks in tuple(self.call_listeners.items()):
            gateway = self.watch_gateways.get(selected)
            # 尚未解析网关路径的订阅者也要收到通知（例如订阅后第一通就是来电），由其重新查询。
            if gateway is None or path.startswith(gateway + '/'):
                for callback in tuple(callbacks):
                    callback()

    def subscribe_calls(self, selected, callback):
        selected = address(selected)
        self.call_listeners.setdefault(selected, set()).add(callback)
        def unsubscribe():
            self.call_listeners[selected].discard(callback)
            if not self.call_listeners[selected]:
                self.call_listeners.pop(selected, None)
                self.watch_gateways.pop(selected, None)
        return unsubscribe

    async def call_snapshot(self, selected, call_id, identity):
        gateway = await self.gateway_path(selected)
        self.watch_gateways[address(selected)] = gateway
        objects = (await self._call(self.session, TELEPHONY, gateway, MANAGER, 'GetManagedObjects'))[0]
        path = gateway + '/' + call_id
        props = objects.get(path, {}).get(CALL)
        if props is None or (path, self.call_generations.get(path, 0)) != identity:
            return 'ended'
        return props.get('State', 'unknown')

    async def close(self):
        if self.agent and self.system:
            try:
                await self._call(self.system, BLUEZ, "/org/bluez", "org.bluez.AgentManager1",
                                 "UnregisterAgent", "o", ["/phone_gateway/agent"])
            finally:
                self.system.unexport("/phone_gateway/agent", self.agent)
        for bus in (self.system, self.session):
            if bus:
                bus.disconnect()

    async def _call(self, bus, service, path, interface, method, signature="", body=None, guard=None):
        from dbus_next import Message, MessageType
        if bus is None:
            raise GatewayError("host_bus_unavailable", 503)
        message = Message(destination=service, path=path,
            interface=interface, member=method, signature=signature, body=body or [])
        async def send():
            # guard 在消息交给 bus 前的同一同步段内执行，中间没有 await。
            if guard is not None and not guard():
                raise IdentityChanged()
            return await bus.call(message)
        try:
            reply = await asyncio.wait_for(send(), 10)
        except IdentityChanged:
            raise
        except asyncio.TimeoutError:
            # BlueZ accepted the call but did not reply in time (for example a stuck Connect).
            raise GatewayError("phone_operation_timeout", 504) from None
        except Exception:
            raise GatewayError("dbus_unavailable", 503) from None
        if reply is None or reply.message_type == MessageType.ERROR:
            name = getattr(reply, "error_name", "") or ""
            code = "unsupported_capability" if name.endswith(("ServiceUnknown", "UnknownObject", "UnknownInterface", "UnknownMethod", "NotSupported")) else "phone_operation_failed"
            raise GatewayError(code, 503 if code == "unsupported_capability" else 409)
        return plain(reply.body)

    async def objects(self, telephony=False):
        return (await self._call(self.session if telephony else self.system,
            TELEPHONY if telephony else BLUEZ, ROOT if telephony else "/", MANAGER, "GetManagedObjects"))[0]

    async def methods(self, path, interface):
        body = await self._call(self.session, TELEPHONY, path,
            "org.freedesktop.DBus.Introspectable", "Introspect")
        try:
            root = ET.fromstring(body[0])
            return {m.attrib["name"] for i in root.findall("interface") if i.get("name") == interface for m in i.findall("method")}
        except (ET.ParseError, KeyError, TypeError):
            raise GatewayError("invalid_host_capability", 503) from None

    async def device_path(self, requested):
        requested = address(requested)
        matches = [path for path, interfaces in (await self.objects()).items()
            if address_if_valid(interfaces.get(DEVICE, {}).get("Address")) == requested]
        if len(matches) != 1:
            raise GatewayError("device_not_found" if not matches else "ambiguous_device", 404 if not matches else 409)
        return matches[0]

    async def gateway_path(self, requested):
        requested = address(requested)
        await self.device_path(requested)
        matches = [path for path, interfaces in (await self.objects(True)).items()
            if address_if_valid(interfaces.get(GATEWAY, {}).get("Address")) == requested]
        if len(matches) != 1:
            raise GatewayError("unsupported_capability" if not matches else "ambiguous_phone", 503 if not matches else 409)
        return matches[0]

    async def devices(self):
        return [{"address": address_if_valid(props[DEVICE].get("Address")),
            "paired": bool(props[DEVICE].get("Paired")), "connected": bool(props[DEVICE].get("Connected")),
            "name": device_name(props[DEVICE])}
            for props in (await self.objects()).values() if DEVICE in props and address_if_valid(props[DEVICE].get("Address"))]

    async def capabilities(self):
        try:
            devices = await self.devices()
            bluez = True
        except GatewayError:
            devices, bluez = [], False
        try:
            objects = await self.objects(True)
            native = True
        except GatewayError:
            objects, native = {}, False
        phones = []
        for path, props in objects.items():
            addr = address_if_valid(props.get(GATEWAY, {}).get("Address"))
            if addr and any(d["address"] == addr for d in devices):
                try:
                    methods = sorted(await self.methods(path, GATEWAY))
                except GatewayError:
                    methods = []
                phones.append({"address": addr, "methods": methods})
        return {"bluez": bluez, "nativeTelephony": native, "phones": phones,
            "audio": {"rate": 24000, "channels": 1, "format": "pcm16le", "maxFrameBytes": 960,
                      "maxQueuedMilliseconds": 200,
                      "singleOwner": True, "readyTimeoutSeconds": 45},
            "control": {"leaseSeconds": 30},
            "unsupportedReason": None if native else "PipeWire native Telephony is not available; use PipeWire 1.4 or later with HFP support."}

    async def connect(self, requested, connected):
        path = await self.device_path(requested)
        await self._call(self.system, BLUEZ, path, DEVICE, "Connect" if connected else "Disconnect")

    async def discover(self):
        adapters = [path for path, value in (await self.objects()).items() if "org.bluez.Adapter1" in value]
        if not adapters:
            raise GatewayError("bluetooth_adapter_missing", 503)
        path = adapters[0]
        await self._call(self.system, BLUEZ, path, "org.bluez.Adapter1", "StartDiscovery")
        try:
            await asyncio.sleep(5)
            return await self.devices()
        finally:
            await self._call(self.system, BLUEZ, path, "org.bluez.Adapter1", "StopDiscovery")

    async def pair(self, requested, key):
        path = await self.device_path(requested)
        if not self.agent:
            from .pairing import Agent
            self.agent = Agent(self)
            self.system.export("/phone_gateway/agent", self.agent)
            try:
                await self._call(self.system, BLUEZ, "/org/bluez", "org.bluez.AgentManager1",
                                 "RegisterAgent", "os", ["/phone_gateway/agent", "KeyboardDisplay"])
            except BaseException:
                self.system.unexport("/phone_gateway/agent", self.agent)
                self.agent = None
                raise
        from dbus_next import Message, MessageType
        try:
            reply = await asyncio.wait_for(self.system.call(Message(destination=BLUEZ, path=path,
                interface=DEVICE, member="Pair")), 70)
            if reply is None or reply.message_type == MessageType.ERROR:
                raise GatewayError("pairing_failed", 409)
        except BaseException:
            try:
                await self._call(self.system, BLUEZ, path, DEVICE, "CancelPairing")
            finally:
                raise

    async def remove(self, requested):
        path = await self.device_path(requested)
        if any(d["address"] == address(requested) and d["connected"] for d in await self.devices()):
            raise GatewayError("disconnect_before_remove", 409)
        await self._call(self.system, BLUEZ, path.rsplit("/", 1)[0], "org.bluez.Adapter1", "RemoveDevice", "o", [path])

    async def calls(self, requested):
        path = await self.gateway_path(requested)
        if address(requested) in self.call_listeners:
            self.watch_gateways[address(requested)] = path
        objects = (await self._call(self.session, TELEPHONY, path, MANAGER, "GetManagedObjects"))[0]
        # Phone numbers, names and incoming line properties are not returned.
        return [{"id": p.rsplit("/", 1)[-1], "state": props[CALL].get("State", "unknown"),
                 "multiparty": bool(props[CALL].get("Multiparty"))}
                for p, props in objects.items() if p.startswith(path + "/") and CALL in props]

    async def call_identity(self, requested, call_id):
        if not self.identity_tracking:
            raise GatewayError("call_identity_tracking_unavailable", 503)
        gateway = await self.gateway_path(requested)
        objects = (await self._call(self.session, TELEPHONY, gateway, MANAGER, "GetManagedObjects"))[0]
        path = gateway + "/" + call_id
        if CALL not in objects.get(path, {}):
            return None
        return (path, self.call_generations.get(path, 0))

    async def hangup_owned(self, requested, call_id, identity):
        """只挂断仍是 identity（对象路径, 代次）的那通电话。

        True：已向仍是本次身份的电话发出 Hangup（不代表手机已结束）。
        False：本次电话已不存在或身份已变，未发送任何命令。
        异常：结果未知，不能当成功。
        """
        if not identity:
            return False
        if not self.identity_tracking:
            raise GatewayError("call_identity_tracking_unavailable", 503)
        if not re.fullmatch(r"[A-Za-z0-9_]+", call_id):
            raise GatewayError("invalid_call_id")
        path, generation = identity
        gateway = await self.gateway_path(requested)
        if path != gateway + "/" + call_id:
            return False
        owned = lambda: self.call_generations.get(path, 0) == generation
        try:
            supported = "Hangup" in await self.methods(path, CALL)
        except GatewayError:
            if not owned() or await self.call_identity(requested, call_id) != identity:
                return False
            raise
        if not supported:
            raise GatewayError("unsupported_capability", 503)
        # 最后一次读取：确认对象仍在；此前到达的增删信号已更新代次。
        objects = (await self._call(self.session, TELEPHONY, gateway, MANAGER, "GetManagedObjects"))[0]
        if CALL not in objects.get(path, {}):
            return False
        try:
            # 代次核对与发送之间没有 await。
            await self._call(self.session, TELEPHONY, path, CALL, "Hangup", guard=owned)
        except IdentityChanged:
            return False
        return True

    async def call_state(self, gateway, path):
        objects = (await self._call(self.session, TELEPHONY, gateway, MANAGER, "GetManagedObjects"))[0]
        props = objects.get(path, {}).get(CALL)
        return None if props is None else props.get("State", "unknown")

    async def answer(self, requested, call_id):
        """接听仍是同一身份（对象路径, 代次）的响铃来电；有界时间内 State 变为 active 才算成功。"""
        if not self.identity_tracking:
            raise GatewayError("call_identity_tracking_unavailable", 503)
        if not re.fullmatch(r"[A-Za-z0-9_]+", call_id):
            raise GatewayError("invalid_call_id")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ANSWER_SECONDS
        gateway = await self.gateway_path(requested)
        path = gateway + "/" + call_id
        generation = self.call_generations.get(path, 0)
        unchanged = lambda: self.call_generations.get(path, 0) == generation
        state = await self.call_state(gateway, path)
        if state is None:
            raise GatewayError("call_not_found", 404)
        if state != "incoming":
            raise GatewayError("call_not_incoming", 409)
        if "Answer" not in await self.methods(path, CALL):
            raise GatewayError("unsupported_capability", 503)
        # 最后一次读取：确认对象仍在；此前到达的增删信号已更新代次。
        if await self.call_state(gateway, path) is None:
            raise GatewayError("call_not_found", 409)
        try:
            # 代次核对与发送之间没有 await。
            await asyncio.wait_for(self._call(self.session, TELEPHONY, path, CALL, "Answer", guard=unchanged),
                                   max(deadline - loop.time(), 0.1))
        except IdentityChanged:
            raise GatewayError("call_not_found", 409) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            # 错误回复或超时都不能证明手机没有接听；以下以通话状态为准。
            pass
        while True:
            state = await self.call_state(gateway, path)
            if state is None or not unchanged():
                raise GatewayError("call_not_found", 409)
            if state == "active":
                return {"ok": True}
            if loop.time() >= deadline:
                raise GatewayError("answer_not_confirmed", 503)
            await asyncio.sleep(0.1)

    async def operate(self, requested, operation, value=None, call_id=None):
        if operation == "answer" and call_id is not None:
            return await self.answer(requested, call_id)
        path = await self.gateway_path(requested)
        interface = GATEWAY
        method = {"dial": "Dial", "tones": "SendTones", "answer": "Answer", "hangup": "Hangup"}[operation]
        if call_id is not None:
            if not re.fullmatch(r"[A-Za-z0-9_]+", call_id):
                raise GatewayError("invalid_call_id")
            objects = (await self._call(self.session, TELEPHONY, path, MANAGER, "GetManagedObjects"))[0]
            path += "/" + call_id
            if CALL not in objects.get(path, {}):
                raise GatewayError("call_not_found", 404)
            interface = CALL
        if method not in await self.methods(path, interface):
            raise GatewayError("unsupported_capability", 503)
        body = await self._call(self.session, TELEPHONY, path, interface, method, "s" if value is not None else "", [value] if value is not None else [])
        # Some PipeWire versions return no Dial result; read /calls in that case.
        return {"id": body[0].rsplit("/", 1)[-1]} if body and isinstance(body[0], str) and body[0].startswith(path + "/") else {"ok": True}


class IdentityChanged(Exception):
    """发送前最后核对时，电话对象已换代。"""


def address_if_valid(value):
    try:
        return address(value)
    except GatewayError:
        return None
