"""Full native entry regression: real SLC and raw CIEV, no physical radio."""
import asyncio
import tempfile
from pathlib import Path
import secrets
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from server.common.api import create_app
from server.common.call_state import CallState
from server.common.contracts import GatewayError
from server.common.storage import Store
from server.mac.adapter import MacPhoneAdapter
from server.mac.native.acipc_pair import run
from server.mac.service import MacService
from server.tests_mac.native_fixture import NativeFixture


class NativeEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_slc_never_starts_service_and_releases_hardware(self):
        fixture = NativeFixture()
        fixture.fail_command = 'AT+BRSF=0'
        invoked = False
        async def operation(*_):
            nonlocal invoked
            invoked = True
        with fixture.patched(), self.assertRaises(Exception):
            await asyncio.wait_for(run(fixture.PHONE, operation), 3)
        self.assertFalse(invoked)
        self.assertTrue(fixture.stopped)
        self.assertEqual(fixture.commands, ['AT+BRSF=0'])
        self.assertEqual((fixture.rfcomm_shutdown, fixture.acl_disconnect), (1, 1))

    async def test_cancelling_actual_native_entry_cleans_service_and_hardware(self):
        fixture = NativeFixture()
        started, closed = asyncio.Event(), asyncio.Event()
        async def operation(device, connection, protocol, transport):
            adapter = MacPhoneAdapter(fixture.PHONE, device, connection, protocol, transport)
            try:
                started.set()
                await asyncio.Future()
            finally:
                await adapter.close()
                closed.set()
        with fixture.patched():
            task = asyncio.create_task(run(fixture.PHONE, operation))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertTrue(closed.is_set() and fixture.stopped)
        self.assertEqual((fixture.rfcomm_shutdown, fixture.acl_disconnect), (1, 1))

    async def test_native_entry_negotiates_and_delivers_answer_and_end(self):
        fixture = NativeFixture()
        states = []
        async def operation(device, connection, protocol, transport):
            adapter = MacPhoneAdapter(fixture.PHONE, device, connection, protocol, transport)
            adapter.own(SimpleNamespace(index=1, direction=0, number='fixture', type=129))
            tracker = CallState(adapter, {'address': fixture.PHONE, 'callId': 'call1', 'identity': adapter.owned_signature}, lambda: None)
            queue = tracker.subscribe()
            try:
                states.append((await queue.get())['state'])
                tracker.start()
                states.append((await asyncio.wait_for(queue.get(), 1))['state'])
                fixture.call_state = 0
                fixture.notify(b'\r\n+CIEV: 1,1\r\n\r\n+CIEV: 2,0\r\n')
                states.append((await asyncio.wait_for(queue.get(), 1))['state'])
                fixture.call_state = None
                fixture.notify(b'\r\n+CIEV: 1,0\r\n')
                states.append((await asyncio.wait_for(queue.get(), 1))['state'])
                self.assertIsNone(adapter.owned_signature)
            finally:
                await tracker.close()
                await adapter.close()
        with fixture.patched():
            await asyncio.wait_for(run(fixture.PHONE, operation), 3)
        self.assertEqual(states, ['unknown', 'alerting', 'active', 'ended'])
        self.assertEqual(fixture.commands[:4], ['AT+BRSF=0', 'AT+CIND=?', 'AT+CIND?', 'AT+CMER=3,,,1'])
        self.assertEqual(fixture.commands.count('AT+BRSF=0'), 1)
        self.assertNotIn('AT+CHUP', fixture.commands)
        self.assertTrue(fixture.started and fixture.stopped)
        self.assertEqual((fixture.rfcomm_shutdown, fixture.acl_disconnect), (1, 1))

    async def test_real_service_and_console_report_acl_loss_then_reconnect(self):
        root = Path(tempfile.gettempdir()) / 'phone-call-gateway/native-entry-tests' / secrets.token_hex(8)
        store = Store(root)
        store.save('devices', 'mac', [NativeFixture.PHONE])
        service = MacService(store)
        client = TestClient(TestServer(create_app(service, managed=True, store=store)))
        await client.start_server()
        ws = await client.ws_connect('/v1/console/events')
        async def receive(connected):
            for _ in range(8):
                value = await ws.receive_json(timeout=1)
                device = next(d for d in value['devices'] if d['address'] == NativeFixture.PHONE)
                if device['hfp'] == connected:
                    return device
            self.fail('Expected connection event was not delivered')
        try:
            self.assertFalse((await receive(False))['connected'])
            first = NativeFixture()
            with first.patched():
                response = await client.post(f'/v1/management/{first.PHONE}/connect')
                self.assertEqual(response.status, 200)
                self.assertTrue((await receive(True))['connected'])
                task = service.connection_task
                first.lose_acl()
                await asyncio.wait_for(task, 2)
                self.assertFalse((await receive(False))['connected'])
            self.assertIsNone(service.adapter)
            self.assertFalse(service.cleanup_failed)
            self.assertTrue(first.stopped)
            self.assertEqual((first.rfcomm_shutdown, first.acl_disconnect), (0, 0))
            second = NativeFixture()
            with second.patched():
                response = await client.post(f'/v1/management/{second.PHONE}/connect')
                self.assertEqual(response.status, 200)
                self.assertTrue((await receive(True))['connected'])
                response = await client.post(f'/v1/management/{second.PHONE}/disconnect')
                self.assertEqual(response.status, 200)
                self.assertFalse((await receive(False))['connected'])
            self.assertTrue(second.stopped)
            self.assertEqual((second.rfcomm_shutdown, second.acl_disconnect), (1, 1))
        finally:
            await ws.close()
            await client.close()


class ActiveLinkLossTests(unittest.IsolatedAsyncioTestCase):
    """F2: 活动通话中 ACL 丢失，本地资源已释放，手机电话状态未知。"""

    def setUp(self):
        root = Path(tempfile.gettempdir()) / 'phone-call-gateway/native-entry-tests' / secrets.token_hex(8)
        self.store = Store(root)
        self.store.save('devices', 'mac', [NativeFixture.PHONE])
        self.service = MacService(self.store)

    async def dial(self):
        await self.service.operate(NativeFixture.PHONE, 'dial', '123')
        identity = await self.service.call_identity(NativeFixture.PHONE, 'call1')
        self.assertIsNotNone(identity)
        return identity

    async def test_capabilities_have_no_capacity_fields(self):
        capabilities = await self.service.capabilities()
        self.assertNotIn('maxAudioLines', capabilities)
        self.assertNotIn('capacityVerified', capabilities)
        self.assertTrue(capabilities['pairing'] and capabilities['discovery'])

    async def test_acl_loss_reports_unknown_then_reconnect_never_hangs_up_new_call(self):
        first = NativeFixture()
        first.call_state = None
        with first.patched():
            await self.service.connect(first.PHONE, True)
            identity = await self.dial()
            task = self.service.connection_task
            first.lose_acl()
            await asyncio.wait_for(task, 2)
        self.assertTrue(first.stopped)
        self.assertFalse(self.service.cleanup_failed)
        # 连接已丢失：挂断和核对都必须如实报告不可用，不能当成功或已结束。
        with self.assertRaises(GatewayError) as raised:
            await self.service.hangup_owned(first.PHONE, 'call1', identity)
        self.assertEqual(raised.exception.code, 'device_disconnected')
        with self.assertRaises(GatewayError):
            await self.service.call_snapshot(first.PHONE, 'call1', identity)
        self.assertEqual(first.commands.count('AT+CHUP'), 0)
        # 恢复后，手机上有一通外观相同的电话；旧身份不得挂断它。
        second = NativeFixture()
        second.call_state = 0
        with second.patched():
            await self.service.connect(second.PHONE, True)
            self.assertFalse(await self.service.hangup_owned(second.PHONE, 'call1', identity))
            self.assertEqual(await self.service.call_snapshot(second.PHONE, 'call1', identity), 'ended')
            self.assertIsNone(await self.service.call_identity(second.PHONE, 'call1'))
            self.assertNotIn('AT+CHUP', second.commands)
            calls = await self.service.calls(second.PHONE)
            self.assertEqual([c['state'] for c in calls], ['active'])
            with self.assertRaises(GatewayError) as busy:
                await self.service.operate(second.PHONE, 'dial', '123')
            self.assertEqual(busy.exception.code, 'phone_busy')
            # 手机上的电话结束后，新的显式拨号正常进行，并且新身份可以挂断。
            second.call_state = None
            fresh = await self.dial()
            self.assertNotEqual(fresh, identity)
            self.assertTrue(await self.service.hangup_owned(second.PHONE, 'call1', fresh))
            self.assertEqual(second.commands.count('AT+CHUP'), 1)
            await self.service.connect(second.PHONE, False)
        self.assertTrue(second.stopped)

    async def test_failed_link_cleanup_with_native_released_does_not_block_reconnect(self):
        first = NativeFixture()
        first.cleanup_error = RuntimeError('link cleanup failed')
        first.call_state = None
        with first.patched():
            await self.service.connect(first.PHONE, True)
            await self.dial()
            task = self.service.connection_task
            first.lose_control_channel()
            await asyncio.wait_for(task, 2)
        self.assertTrue(first.stopped)
        self.assertIsNone(self.service.adapter)
        # native transport 已释放：不能因链路清理失败而永久拒绝重连。
        self.assertFalse(self.service.cleanup_failed)
        self.assertEqual(self.service.release_error, 'RuntimeError')
        second = NativeFixture()
        with second.patched():
            await self.service.connect(second.PHONE, True)
            self.assertEqual((await self.service.devices())[0]['hfp'], True)
            await self.service.connect(second.PHONE, False)
        self.assertEqual((second.rfcomm_shutdown, second.acl_disconnect), (1, 1))

    async def test_blocked_native_release_is_reported_until_it_completes(self):
        first = NativeFixture()
        first.release_gate = asyncio.Event()
        with first.patched():
            await self.service.connect(first.PHONE, True)
            task = self.service.connection_task
            first.lose_acl()
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            self.assertIsNone(self.service.adapter)
            with self.assertRaises(GatewayError) as raised:
                await self.service.connect(first.PHONE, True)
            self.assertEqual((raised.exception.code, raised.exception.status), ('controller_releasing', 503))
            first.release_gate.set()
            await asyncio.wait_for(task, 2)
        self.assertFalse(self.service.cleanup_failed)
        second = NativeFixture()
        with second.patched():
            await self.service.connect(second.PHONE, True)
            await self.service.connect(second.PHONE, False)
        self.assertTrue(second.stopped)


class DeviceNameTests(unittest.IsolatedAsyncioTestCase):
    """连接后在后台读取手机蓝牙名称，只存本机设备配置。"""

    def setUp(self):
        root = Path(tempfile.gettempdir()) / 'phone-call-gateway/native-entry-tests' / secrets.token_hex(8)
        self.store = Store(root)
        self.store.save('devices', 'mac', [NativeFixture.PHONE])
        self.service = MacService(self.store)
        self.seen = []
        self.service.on_change = lambda: self.seen.append(self.service.names.get(NativeFixture.PHONE))

    async def wait_name(self, expected):
        for _ in range(100):
            if (await self.service.devices())[0]['name'] == expected:
                return
            await asyncio.sleep(0.01)
        self.fail('Device name was not reported')

    async def test_unknown_name_is_null(self):
        self.assertEqual(await self.service.devices(),
                         [{'address': NativeFixture.PHONE, 'paired': True, 'connected': False, 'hfp': False, 'name': None}])

    async def test_name_is_learned_stored_and_kept_after_disconnect(self):
        fixture = NativeFixture()
        fixture.remote_name = 'Pixel 9\x00'
        with fixture.patched():
            await self.service.connect(fixture.PHONE, True)
            await self.wait_name('Pixel 9')
            # 名称变化触发快照更新。
            self.assertIn('Pixel 9', self.seen)
            await self.service.connect(fixture.PHONE, False)
        self.assertEqual(fixture.name_requests, 1)
        self.assertEqual((await self.service.devices())[0]['name'], 'Pixel 9')
        self.assertEqual(self.store.load('devices', 'mac-names'), {fixture.PHONE: 'Pixel 9'})
        self.assertEqual((await MacService(self.store).devices())[0]['name'], 'Pixel 9')

    async def test_name_failure_keeps_link_and_previous_name(self):
        self.store.save('devices', 'mac-names', {NativeFixture.PHONE: 'Old Name'})
        self.service = MacService(self.store)
        fixture = NativeFixture()
        fixture.remote_name = RuntimeError('remote name failed')
        fixture.call_state = None
        with fixture.patched():
            await self.service.connect(fixture.PHONE, True)
            await asyncio.sleep(0.05)
            device = (await self.service.devices())[0]
            self.assertEqual((device['hfp'], device['name']), (True, 'Old Name'))
            await self.service.operate(fixture.PHONE, 'dial', '123')
            await self.service.connect(fixture.PHONE, False)
        self.assertTrue(fixture.stopped)
        self.assertEqual((fixture.rfcomm_shutdown, fixture.acl_disconnect), (1, 1))

    async def test_pending_name_does_not_block_connect_or_disconnect(self):
        fixture = NativeFixture()
        fixture.name_gate = asyncio.Event()
        with fixture.patched():
            await asyncio.wait_for(self.service.connect(fixture.PHONE, True), 2)
            self.assertTrue((await self.service.devices())[0]['hfp'])
            await asyncio.wait_for(self.service.connect(fixture.PHONE, False), 2)
        self.assertTrue(fixture.stopped)
        self.assertIsNone((await self.service.devices())[0]['name'])
        self.assertIsNone(self.store.load('devices', 'mac-names'))

    async def test_remove_forgets_name(self):
        self.store.save('devices', 'mac-names', {NativeFixture.PHONE: 'Pixel 9'})
        self.store.save('devices', 'mac-controllers', {NativeFixture.PHONE: '11:22:33:44:55:66'})
        service = MacService(self.store)
        class Keys:
            def __init__(self, *_): pass
            async def delete(self): pass
        with patch('server.mac.service.SelectedKeyStore', Keys):
            await service.remove(NativeFixture.PHONE)
        self.assertEqual(self.store.load('devices', 'mac-names'), {})
        self.assertEqual(await service.devices(), [])
