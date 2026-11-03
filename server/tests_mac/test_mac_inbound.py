"""来电接听：以手机 CLCC 状态为准确认，接听后的来电与去电一样由本连接拥有。"""
import asyncio
import unittest
from types import SimpleNamespace

from bumble import hfp
from pyee import EventEmitter

from server.common.contracts import GatewayError
from server.mac.adapter import MacPhoneAdapter
from server.mac.native.acipc_pair import serve_hfp
from test_mac_gateway import fixture, PHONE

NO_CALLS = (hfp.AgIndicator.CALL, hfp.AgIndicator.CALL_SETUP, hfp.AgIndicator.CALL_HELD)


def ringing(number='caller-fixture', status=4):
    return SimpleNamespace(index=1, status=status, multi_party=0, direction=1, number=number, type=129)


def answering(adapter, protocol, reply=None, active=True):
    """ATA 的替身：可让手机接通（CLCC 变 active），同时回错误状态码，复现真机现象。"""
    original = adapter.control.original
    async def execute(command, **kw):
        if command == 'ATA':
            protocol.commands.append(command)
            if active and protocol.current:
                protocol.current[0].status = 0
            if reply:
                raise reply
            return None
        return await original(command, **kw)
    return execute


def inbound_fixture(**kw):
    adapter, protocol, sent = fixture()
    protocol.current = [ringing()]
    adapter.control.original = answering(adapter, protocol, **kw)
    adapter.ANSWER_SECONDS = 0.3
    return adapter, protocol


def no_calls(protocol):
    protocol.ag_indicators = [SimpleNamespace(indicator=key, current_status=0) for key in NO_CALLS]
    protocol.emit(hfp.HfProtocol.EVENT_AG_INDICATOR, protocol.ag_indicators[0])


class MacAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unanswered_incoming_has_identity_that_survives_answer(self):
        adapter, protocol = inbound_fixture()
        self.assertEqual(await adapter.calls(PHONE), [{'id': 'call1', 'state': 'incoming', 'multiparty': False}])
        before = await adapter.call_identity(PHONE, 'call1')
        self.assertIsNotNone(before)
        self.assertEqual(await adapter.call_identity(PHONE, 'call1'), before)
        self.assertEqual(await adapter.call_snapshot(PHONE, 'call1', before), 'incoming')
        # 未接听的来电不是本连接的通话：不能挂断（不拒接）。
        self.assertFalse(await adapter.hangup_owned(PHONE, 'call1', before))
        self.assertEqual(protocol.commands, [])
        self.assertEqual(await adapter.operate(PHONE, 'answer', call_id='call1'), {'ok': True})
        self.assertEqual(await adapter.call_identity(PHONE, 'call1'), before)
        self.assertNotIn('caller-fixture', str(await adapter.calls(PHONE)))
        await adapter.close()

    async def test_ata_error_but_phone_active_is_success_and_owned_like_outbound(self):
        # 真机：ATA 回错误状态码，但 CLCC 立即为 active。
        adapter, protocol = inbound_fixture(reply=hfp.HfpProtocolError('ERROR'))
        identity = await adapter.call_identity(PHONE, 'call1')
        self.assertEqual(await adapter.operate(PHONE, 'answer', call_id='call1'), {'ok': True})
        self.assertEqual(await adapter.call_identity(PHONE, 'call1'), identity)
        self.assertEqual(await adapter.call_snapshot(PHONE, 'call1', identity), 'active')
        self.assertFalse(await adapter.hangup_owned(PHONE, 'call2', identity))
        self.assertTrue(await adapter.hangup_owned(PHONE, 'call1', identity))
        self.assertEqual(protocol.commands, ['ATA', 'AT+CHUP'])
        self.assertFalse(await adapter.hangup_owned(PHONE, 'call1', identity))
        await adapter.close()

    async def test_unconfirmed_answer_then_close_never_rejects_ringing_call(self):
        # 审查 B1-a：接听未确认、仍在响铃时关闭连接，不得发挂断（等于拒接）。
        adapter, protocol = inbound_fixture(active=False)
        identity = await adapter.call_identity(PHONE, 'call1')
        loop = asyncio.get_running_loop()
        start = loop.time()
        with self.assertRaises(GatewayError) as raised:
            await adapter.operate(PHONE, 'answer', call_id='call1')
        self.assertEqual(raised.exception.code, 'answer_not_confirmed')
        self.assertLess(loop.time() - start, 1)
        self.assertEqual((await adapter.calls(PHONE))[0]['state'], 'incoming')
        # 不再拥有：身份仍可读（未接听来电），但不能被挂断。
        self.assertEqual(await adapter.call_identity(PHONE, 'call1'), identity)
        self.assertIsNone(adapter.owned_signature)
        self.assertFalse(await adapter.hangup_owned(PHONE, 'call1', identity))
        await adapter.close()
        self.assertEqual(protocol.commands, ['ATA'])

    async def test_cancelled_answer_then_close_never_rejects_ringing_call(self):
        # 审查 B1-b：接听轮询中被取消（服务关闭），随后关闭连接，不得发挂断。
        adapter, protocol = inbound_fixture(active=False)
        adapter.ANSWER_SECONDS = 5
        task = asyncio.create_task(adapter.operate(PHONE, 'answer', call_id='call1'))
        await asyncio.sleep(0.1)
        self.assertEqual(protocol.commands, ['ATA'])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(adapter.owned_signature)
        await adapter.close()
        self.assertEqual(protocol.commands, ['ATA'])

    async def test_close_skips_owned_call_that_rings_again(self):
        # close() 自身也不对响铃中的通话发挂断，即使拥有关系仍在。
        for status in (4, 5):
            with self.subTest(status=status):
                adapter, protocol = inbound_fixture(active=False)
                call = protocol.current[0]
                adapter.call_attempted = True
                adapter.own(call, 1)
                call.status = status
                await adapter.close()
                self.assertEqual(protocol.commands, [])

    async def test_answered_call_is_still_hung_up_on_close(self):
        adapter, protocol = inbound_fixture()
        await adapter.operate(PHONE, 'answer', call_id='call1')
        await adapter.close()
        self.assertEqual(protocol.commands, ['ATA', 'AT+CHUP'])

    async def test_ata_error_and_not_active_is_failure(self):
        adapter, protocol = inbound_fixture(reply=hfp.HfpProtocolError('ERROR'), active=False)
        with self.assertRaises(GatewayError) as raised:
            await adapter.operate(PHONE, 'answer', call_id='call1')
        self.assertEqual(raised.exception.code, 'answer_not_confirmed')

    async def test_caller_hangs_up_during_answer(self):
        adapter, protocol = inbound_fixture(active=False)
        async def gone(command, **kw):
            protocol.commands.append(command)
            protocol.current = []
        adapter.control.original = gone
        with self.assertRaises(GatewayError) as raised:
            await adapter.operate(PHONE, 'answer', call_id='call1')
        self.assertEqual(raised.exception.code, 'call_not_found')
        self.assertIsNone(adapter.owned_signature)
        await adapter.close()
        self.assertEqual(protocol.commands, ['ATA'])

    async def test_only_a_single_ringing_incoming_call_is_answered(self):
        for current, code in (([ringing(status=0)], 'call_not_incoming'),
                              ([SimpleNamespace(index=1, status=3, multi_party=0, direction=0, number='x', type=129)], 'call_not_incoming'),
                              ([ringing(), SimpleNamespace(index=2, status=0, multi_party=0, direction=0, number='x', type=129)], 'ambiguous_call'),
                              ([], 'call_not_found')):
            with self.subTest(code=code, rows=len(current)):
                adapter, protocol = inbound_fixture()
                protocol.current = current
                with self.assertRaises(GatewayError) as raised:
                    await adapter.operate(PHONE, 'answer', call_id='call1')
                self.assertEqual(raised.exception.code, code)
                self.assertIsNone(adapter.owned_signature)
                await adapter.close()
                self.assertEqual(protocol.commands, [])

    async def test_identity_rechecked_right_before_ata(self):
        adapter, protocol = inbound_fixture()
        async with adapter.control.lock:
            pending = asyncio.create_task(adapter.operate(PHONE, 'answer', call_id='call1'))
            await asyncio.sleep(0.05)
            self.assertFalse(pending.done())
            # 发送 ATA 之前，手机报告已没有电话（对方挂断），随后同号码再次来电。
            no_calls(protocol)
        with self.assertRaises(GatewayError) as raised:
            await pending
        self.assertEqual(raised.exception.code, 'call_not_found')
        self.assertNotIn('ATA', protocol.commands)
        self.assertIsNone(adapter.owned_signature)
        await adapter.close()

    async def test_new_ringing_call_after_no_calls_has_new_identity(self):
        adapter, protocol = inbound_fixture()
        old = await adapter.call_identity(PHONE, 'call1')
        no_calls(protocol)
        protocol.current = [ringing()]
        new = await adapter.call_identity(PHONE, 'call1')
        self.assertIsNotNone(new)
        self.assertNotEqual(old, new)
        self.assertEqual(await adapter.call_snapshot(PHONE, 'call1', old), 'ended')
        await adapter.close()

    async def test_outbound_dial_still_rejects_incoming_row(self):
        adapter, protocol, _ = fixture()
        async def incoming_instead(command, **_):
            protocol.commands.append(command)
            protocol.current = [ringing()]
        adapter.control.original = incoming_instead
        with self.assertRaises(GatewayError) as raised:
            await adapter.operate(PHONE, 'dial', '123')
        self.assertEqual(raised.exception.code, 'dial_call_not_identified')
        self.assertIsNone(adapter.owned_signature)


class WireDLC:
    """在 RFCOMM 字节层复现真机：来电 +CIEV，ATA 回 ERROR 但 CLCC 已 active。"""
    def __init__(self):
        channel = EventEmitter()
        channel.EVENT_CLOSE = 'close'
        self.multiplexer = SimpleNamespace(l2cap_channel=channel)
        self.rows = []
        self.commands = []

    def write(self, command):
        command = command.strip()
        self.commands.append(command)
        if command == 'AT+CLCC':
            body = ''.join(f'\r\n+CLCC: {row}\r\n' for row in self.rows)
            self.sink(f'{body}\r\nOK\r\n'.encode())
        elif command == 'ATA':
            self.rows = ['1,1,0,0,0,"caller-fixture",129']
            self.sink(b'\r\nERROR\r\n')
        elif command == 'AT+CHUP':
            self.rows = []
            self.sink(b'\r\nOK\r\n')


class MacInboundWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_incoming_notification_answer_error_and_hangup_on_the_wire(self):
        dlc = WireDLC()
        protocol = hfp.HfProtocol(dlc, hfp.HfConfiguration([], [], [hfp.AudioCodec.CVSD]))
        protocol._slc_initialized = True
        protocol.ag_indicators = [hfp.AgIndicatorState(key, {0, 1, 2, 3}, 0) for key in NO_CALLS]
        for index, state in enumerate(protocol.ag_indicators):
            state.index = index
        device = SimpleNamespace(EVENT_SCO_REQUEST='request', EVENT_SCO_CONNECTION='connected', EVENT_SCO_CONNECTION_FAILURE='failed')
        connection = EventEmitter()
        async def guarded(operation, timeout):
            return await asyncio.wait_for(operation, timeout)
        adapter = MacPhoneAdapter(PHONE, device, connection, protocol, SimpleNamespace(guarded=guarded, failure=None))
        changed = asyncio.Event()
        unsubscribe = adapter.subscribe_calls(PHONE, changed.set)
        stop = asyncio.Event()
        async def operation():
            await stop.wait()
        task = asyncio.create_task(serve_hfp(protocol, operation()))
        try:
            dlc.rows = ['1,1,4,0,0,"caller-fixture",129']
            dlc.sink(b'\r\n+CIEV: 2,1\r\n')
            await asyncio.wait_for(changed.wait(), 1)
            self.assertEqual(await adapter.calls(PHONE), [{'id': 'call1', 'state': 'incoming', 'multiparty': False}])
            identity = await adapter.call_identity(PHONE, 'call1')
            self.assertIsNotNone(identity)
            self.assertEqual(await adapter.operate(PHONE, 'answer', call_id='call1'), {'ok': True})
            self.assertEqual(await adapter.call_snapshot(PHONE, 'call1', identity), 'active')
            self.assertTrue(await adapter.hangup_owned(PHONE, 'call1', identity))
            self.assertEqual([c for c in dlc.commands if c != 'AT+CLCC'], ['ATA', 'AT+CHUP'])
        finally:
            unsubscribe()
            stop.set()
            await asyncio.wait_for(task, 1)
            await adapter.close()


if __name__ == '__main__':
    unittest.main()
