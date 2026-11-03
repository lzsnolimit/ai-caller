import asyncio
import unittest
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer
from server.common.api import create_app
from server.common.contracts import GatewayError
from server.pi.adapter import DBusPhoneAdapter, DEVICE, CALL, ROOT, GATEWAY

PHONE = 'AA:BB:CC:DD:EE:FF'
GW = ROOT + '/ag9'


class Bus:
    def __init__(self): self.handlers = []
    def add_message_handler(self, handler): self.handlers.append(handler)
    def disconnect(self): pass
    def emit(self, message):
        for handler in self.handlers: handler(message)


class Phone(DBusPhoneAdapter):
    def __init__(self):
        super().__init__(Bus(), Bus())
        self.state = 'dialing'
    async def _call(self, bus, service, path, interface, method, signature='', body=None):
        if method == 'AddMatch': return []
        if method == 'GetManagedObjects':
            if path == '/': return [{'/dev': {DEVICE: {'Address': PHONE, 'Connected': True}}}]
            if path == ROOT: return [{GW: {GATEWAY: {'Address': PHONE}}}]
            if path == GW: return [{GW + '/call1': {CALL: {'State': self.state}}}]
        raise AssertionError(method)


class PiStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_websocket_receives_property_events_and_cleans_route(self):
        phone = Phone()
        client = TestClient(TestServer(create_app(phone, lambda _: None)))
        await client.start_server()
        try:
            ws = await client.ws_connect('/v1/events')
            self.assertEqual((await ws.receive_json(timeout=1))['phones'][0]['calls'][0]['state'], 'dialing')
            phone.state = 'active'
            phone.session.emit(SimpleNamespace(interface='org.freedesktop.DBus.Properties', member='PropertiesChanged', path=GW+'/call1', body=[CALL, {'State': 'active'}, []]))
            self.assertEqual((await ws.receive_json(timeout=1))['phones'][0]['calls'][0]['state'], 'active')
            await ws.close()
            await asyncio.sleep(.05)
            self.assertFalse(phone.call_listeners)
            self.assertFalse(phone.watch_gateways)
        finally:
            await client.close()


class NamedPhone(DBusPhoneAdapter):
    def __init__(self):
        super().__init__(Bus(), Bus())
    async def _call(self, bus, service, path, interface, method, signature='', body=None):
        if method == 'GetManagedObjects' and path == '/':
            return [{
                '/alias': {DEVICE: {'Address': PHONE, 'Alias': 'Kitchen Pixel', 'Name': 'Pixel 9', 'Paired': True}},
                '/name': {DEVICE: {'Address': '11:22:33:44:55:66', 'Name': 'iPhone', 'Paired': True}},
                '/none': {DEVICE: {'Address': '22:33:44:55:66:77', 'Alias': '22-33-44-55-66-77', 'Paired': True}},
            }]
        raise AssertionError(method)


class PiDeviceNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_devices_report_alias_then_name_then_null(self):
        devices = await NamedPhone().devices()
        self.assertEqual({d['address']: d['name'] for d in devices},
                         {PHONE: 'Kitchen Pixel', '11:22:33:44:55:66': 'iPhone', '22:33:44:55:66:77': None})


MANAGER = 'org.freedesktop.DBus.ObjectManager'


class SendBus(Bus):
    """记录真正交给 D-Bus 的消息；只有 Hangup 走真实的 _call 发送路径。"""
    def __init__(self):
        super().__init__()
        self.sent = []
        self.fail = False
    async def call(self, msg):
        from dbus_next import Message, MessageType
        self.sent.append((msg.path, msg.member))
        if self.fail:
            return Message.new_error(msg, 'org.freedesktop.DBus.Error.Failed', 'x')
        return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1)


class RacingPhone(DBusPhoneAdapter):
    """在第 swap_at 次准备调用期间，旧 call1 被删除；replace=True 时同路径的新 call1 随即建立。"""
    def __init__(self, swap_at=None, replace=True):
        super().__init__(SendBus(), SendBus())
        self.swap_at, self.replace = swap_at, replace
        self.prep_calls = 0
        self.call_exists = True
    def swap(self):
        path = GW + '/call1'
        self.session.emit(SimpleNamespace(interface=MANAGER, member='InterfacesRemoved', path=GW, body=[path, [CALL]]))
        self.call_exists = False
        if self.replace:
            self.session.emit(SimpleNamespace(interface=MANAGER, member='InterfacesAdded', path=GW, body=[path, {CALL: {'State': 'dialing'}}]))
            self.call_exists = True
    async def _call(self, bus, service, path, interface, method, signature='', body=None, **kw):
        if method == 'AddMatch': return []
        if method == 'Hangup':
            return await super()._call(bus, service, path, interface, method, signature, body, **kw)
        if self.swap_at is not None and self.prep_calls == self.swap_at:
            self.swap()  # 信号先于本次回复到达
        self.prep_calls += 1
        if method == 'GetManagedObjects':
            if path == '/': return [{'/dev': {DEVICE: {'Address': PHONE, 'Connected': True}}}]
            if path == ROOT: return [{GW: {GATEWAY: {'Address': PHONE}}}]
            if path == GW: return [{GW + '/call1': {CALL: {'State': 'active'}}} if self.call_exists else {}]
        if method == 'Introspect':
            return ['<node><interface name="' + CALL + '"><method name="Hangup"/></interface></node>']
        raise AssertionError(method)


class PiHangupIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def phone(self, **kw):
        try:
            import dbus_next  # noqa: F401
        except ImportError:
            self.skipTest('dbus-next is tested in the dependency environment')
        phone = RacingPhone(**kw)
        await phone.start()
        return phone

    async def prep_count(self):
        phone = await self.phone()
        identity = await phone.call_identity(PHONE, 'call1')
        phone.prep_calls = 0
        self.assertTrue(await phone.hangup_owned(PHONE, 'call1', identity))
        self.assertEqual(phone.session.sent, [(GW + '/call1', 'Hangup')])
        return phone.prep_calls

    async def test_old_hangup_never_reaches_new_call_with_same_path(self):
        # F4：挂断准备的任一异步步骤期间旧 call1 删除、新 call1 建立，旧请求被拒绝且不发送 Hangup。
        for replace in (True, False):
            for k in range(await self.prep_count()):
                with self.subTest(replace=replace, swap_at=k):
                    phone = await self.phone()
                    identity = await phone.call_identity(PHONE, 'call1')
                    phone.prep_calls, phone.swap_at, phone.replace = 0, k, replace
                    self.assertFalse(await phone.hangup_owned(PHONE, 'call1', identity))
                    self.assertEqual(phone.session.sent, [])

    async def test_new_call_own_hangup_still_ends_new_call(self):
        phone = await self.phone()
        old = await phone.call_identity(PHONE, 'call1')
        phone.swap()
        new = await phone.call_identity(PHONE, 'call1')
        self.assertNotEqual(old, new)
        self.assertFalse(await phone.hangup_owned(PHONE, 'call1', old))
        self.assertTrue(await phone.hangup_owned(PHONE, 'call1', new))
        self.assertEqual(phone.session.sent, [(GW + '/call1', 'Hangup')])

    async def test_missing_identity_or_failed_send_is_not_success(self):
        phone = await self.phone()
        self.assertFalse(await phone.hangup_owned(PHONE, 'call1', None))
        identity = await phone.call_identity(PHONE, 'call1')
        phone.session.fail = True
        with self.assertRaises(Exception):
            await phone.hangup_owned(PHONE, 'call1', identity)
        phone.identity_tracking = False
        with self.assertRaises(Exception):
            await phone.hangup_owned(PHONE, 'call1', identity)


class AnswerBus(SendBus):
    """Answer 走真实发送路径；可让手机接通，同时回 D-Bus 错误（与 Mac 真机现象同类）。"""
    def __init__(self, phone):
        super().__init__()
        self.phone = phone
    async def call(self, msg):
        if msg.member == 'Answer' and self.phone.answers:
            self.phone.state = 'active'
        return await super().call(msg)


class AnsweringPhone(DBusPhoneAdapter):
    ANSWER_SECONDS = 0.3

    def __init__(self):
        super().__init__(SendBus(), None)
        self.session = AnswerBus(self)
        self.state, self.answers, self.exists = 'incoming', True, True
        self.before_answer = None
    def remove(self, replace=False):
        path = GW + '/call1'
        self.session.emit(SimpleNamespace(interface=MANAGER, member='InterfacesRemoved', path=GW, body=[path, [CALL]]))
        self.exists = replace
        if replace:
            self.session.emit(SimpleNamespace(interface=MANAGER, member='InterfacesAdded', path=GW, body=[path, {CALL: {'State': 'incoming'}}]))
    async def _call(self, bus, service, path, interface, method, signature='', body=None, **kw):
        if method == 'AddMatch': return []
        if method in ('Answer', 'Hangup'):
            if method == 'Answer' and self.before_answer:
                self.before_answer()
            return await super()._call(bus, service, path, interface, method, signature, body, **kw)
        if method == 'GetManagedObjects':
            if path == '/': return [{'/dev': {DEVICE: {'Address': PHONE, 'Connected': True}}}]
            if path == ROOT: return [{GW: {GATEWAY: {'Address': PHONE}}}]
            if path == GW: return [{GW + '/call1': {CALL: {'State': self.state}}} if self.exists else {}]
        if method == 'Introspect':
            return ['<node><interface name="' + CALL + '"><method name="Answer"/><method name="Hangup"/></interface></node>']
        raise AssertionError(method)


class PiAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def phone(self):
        try:
            import dbus_next  # noqa: F401
        except ImportError:
            self.skipTest('dbus-next is tested in the dependency environment')
        phone = AnsweringPhone()
        await phone.start()
        return phone

    def answers(self, phone):
        return [sent for sent in phone.session.sent if sent[1] == 'Answer']

    async def test_incoming_call_notifies_subscriber_without_prior_query(self):
        phone = await self.phone()
        events = []
        unsubscribe = phone.subscribe_calls(PHONE, lambda: events.append(True))
        phone.session.emit(SimpleNamespace(interface=MANAGER, member='InterfacesAdded', path=GW, body=[GW + '/call1', {CALL: {'State': 'incoming'}}]))
        self.assertTrue(events)
        unsubscribe()
        self.assertFalse(phone.call_listeners)

    async def test_answer_confirms_active_and_identity_is_unchanged(self):
        phone = await self.phone()
        identity = await phone.call_identity(PHONE, 'call1')
        self.assertIsNotNone(identity)
        self.assertEqual(await phone.operate(PHONE, 'answer', call_id='call1'), {'ok': True})
        self.assertEqual(await phone.call_identity(PHONE, 'call1'), identity)
        self.assertEqual(await phone.call_snapshot(PHONE, 'call1', identity), 'active')
        self.assertTrue(await phone.hangup_owned(PHONE, 'call1', identity))
        self.assertEqual(phone.session.sent, [(GW + '/call1', 'Answer'), (GW + '/call1', 'Hangup')])

    async def test_dbus_error_but_active_is_success(self):
        phone = await self.phone()
        phone.session.fail = True
        self.assertEqual(await phone.operate(PHONE, 'answer', call_id='call1'), {'ok': True})
        self.assertEqual(self.answers(phone), [(GW + '/call1', 'Answer')])

    async def test_not_active_within_bound_is_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                phone = await self.phone()
                phone.answers, phone.session.fail = False, fail
                loop = asyncio.get_running_loop()
                start = loop.time()
                with self.assertRaises(GatewayError) as raised:
                    await phone.operate(PHONE, 'answer', call_id='call1')
                self.assertEqual(raised.exception.code, 'answer_not_confirmed')
                self.assertLess(loop.time() - start, 1)

    async def test_caller_hangs_up_or_new_call_during_answer(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                phone = await self.phone()
                phone.answers = False
                phone.before_answer = None
                async def gone_after_send(msg, call=phone.session.call, phone=phone):
                    reply = await call(msg)
                    phone.remove(replace)
                    return reply
                phone.session.call = gone_after_send
                with self.assertRaises(GatewayError) as raised:
                    await phone.operate(PHONE, 'answer', call_id='call1')
                self.assertEqual(raised.exception.code, 'call_not_found')

    async def test_generation_rechecked_right_before_answer(self):
        # 准备期间旧来电结束、同路径的新来电出现：旧的接听请求不得接听新来电。
        for replace in (False, True):
            with self.subTest(replace=replace):
                phone = await self.phone()
                phone.before_answer = lambda phone=phone: phone.remove(replace)
                with self.assertRaises(GatewayError) as raised:
                    await phone.operate(PHONE, 'answer', call_id='call1')
                self.assertEqual(raised.exception.code, 'call_not_found')
                self.assertEqual(self.answers(phone), [])

    async def test_only_ringing_incoming_is_answered(self):
        for state, code in (('active', 'call_not_incoming'), ('dialing', 'call_not_incoming')):
            with self.subTest(state=state):
                phone = await self.phone()
                phone.state = state
                with self.assertRaises(GatewayError) as raised:
                    await phone.operate(PHONE, 'answer', call_id='call1')
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.answers(phone), [])
        phone = await self.phone()
        phone.identity_tracking = False
        with self.assertRaises(GatewayError) as raised:
            await phone.operate(PHONE, 'answer', call_id='call1')
        self.assertEqual(raised.exception.code, 'call_identity_tracking_unavailable')
        self.assertEqual(self.answers(phone), [])


class PiCallTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_bluez_call_without_reply_is_a_timeout_not_bus_unavailable(self):
        from unittest import mock
        class SlowBus:
            async def call(self, message):
                await asyncio.sleep(3600)
        async def instant_timeout(awaitable, timeout):
            awaitable.close()
            raise asyncio.TimeoutError()
        with mock.patch("server.pi.adapter.asyncio.wait_for", instant_timeout):
            with self.assertRaises(GatewayError) as caught:
                await DBusPhoneAdapter._call(DBusPhoneAdapter.__new__(DBusPhoneAdapter), SlowBus(),
                    "org.bluez", "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01", DEVICE, "Connect")
        self.assertEqual((caught.exception.code, caught.exception.status), ("phone_operation_timeout", 504))
