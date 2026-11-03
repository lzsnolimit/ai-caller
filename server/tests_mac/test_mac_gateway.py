import asyncio
import unittest
from bumble import hfp
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
from bumble import hci
from pyee import EventEmitter

from phone_gateway.adapter import GatewayError
from phone_gateway.api import create_app
from phone_gateway.mac_adapter import MacPhoneAdapter, MacSCOAudio

PHONE = 'AA:BB:CC:DD:EE:FF'


class Protocol(EventEmitter):
    def __init__(self):
        super().__init__()
        self.current = []
        self.commands = []
        self.ag_indicators = []

    async def query_current_calls(self):
        return self.current

    async def execute_command(self, command, **_):
        self.commands.append(command)
        if command.startswith('ATD'):
            self.current = [SimpleNamespace(index=1, status=2, multi_party=0, number='private-fixture', direction=0, type=129)]
        if command == 'AT+CHUP':
            self.current = []

    async def terminate_call(self):
        self.commands.append('AT+CHUP')
        self.current = []


def fixture():
    connection = EventEmitter()
    connection.peer_address = hci.Address(PHONE)
    protocol = Protocol()
    sent = []
    async def guarded(operation, timeout):
        return await operation
    device = SimpleNamespace(EVENT_SCO_REQUEST='request', EVENT_SCO_CONNECTION='connected',
        EVENT_SCO_CONNECTION_FAILURE='failed', host=SimpleNamespace(send_sco_sdu=lambda handle,data: sent.append(data)))
    transport = SimpleNamespace(guarded=guarded, failure=None)
    adapter = MacPhoneAdapter(PHONE, device, connection, protocol, transport)
    adapter.connected(sco_link(42))
    return adapter, protocol, sent


def sco_link(handle):
    link = EventEmitter()
    link.handle = handle
    link.EVENT_DISCONNECTION = 'disconnected'
    async def disconnect():
        link.emit(link.EVENT_DISCONNECTION)
    link.disconnect = disconnect
    return link


PACKET = SimpleNamespace(data=b'\x32\x00' * 30, packet_status=hci.HCI_SynchronousDataPacket.Status.CORRECTLY_RECEIVED_DATA)


class MacTests(unittest.IsolatedAsyncioTestCase):
    async def test_hfp_indicators_trigger_reconciliation_without_forwarding_private_fields(self):
        adapter, protocol, _ = fixture()
        events = []
        unsubscribe = adapter.subscribe_calls(PHONE, lambda: events.append(True))
        protocol.emit(hfp.HfProtocol.EVENT_AG_INDICATOR, SimpleNamespace(indicator=hfp.AgIndicator.SIGNAL))
        self.assertEqual(events, [])
        protocol.emit(hfp.HfProtocol.EVENT_AG_INDICATOR, SimpleNamespace(indicator=hfp.AgIndicator.CALL))
        self.assertEqual(events, [True])
        unsubscribe()
        protocol.emit(hfp.HfProtocol.EVENT_AG_INDICATOR, SimpleNamespace(indicator=hfp.AgIndicator.CALL_SETUP))
        self.assertEqual(events, [True])
        await adapter.close()

    async def test_snapshot_validates_identity_with_one_query(self):
        adapter, protocol, _ = fixture()
        await adapter.operate(PHONE, 'dial', '1234567890')
        identity = adapter.owned_signature
        protocol.current[0].status = 0
        self.assertEqual(await adapter.call_snapshot(PHONE, 'call1', identity), 'active')
        protocol.current[0].number = 'different-private-call'
        self.assertEqual(await adapter.call_snapshot(PHONE, 'call1', identity), 'ended')
        await adapter.close()

    async def test_no_calls_notification_invalidates_identical_reused_row(self):
        adapter, protocol, _ = fixture()
        await adapter.operate(PHONE, 'dial', '1234567890')
        identity = adapter.owned_signature
        protocol.ag_indicators = [SimpleNamespace(indicator=key, current_status=0) for key in (hfp.AgIndicator.CALL, hfp.AgIndicator.CALL_SETUP, hfp.AgIndicator.CALL_HELD)]
        protocol.emit(hfp.HfProtocol.EVENT_AG_INDICATOR, protocol.ag_indicators[0])
        self.assertEqual(await adapter.call_snapshot(PHONE, 'call1', identity), 'ended')
        self.assertFalse(await adapter.hangup_owned(PHONE, 'call1', identity))
        await adapter.close()
        self.assertNotIn('AT+CHUP', protocol.commands)

    async def test_call_state_and_scoped_control_without_number_leak(self):
        adapter, protocol, _ = fixture()
        result = await adapter.operate(PHONE, 'dial', '1234567890')
        self.assertEqual(result['call']['id'], 'call1')
        protocol.current[0].status = 0
        calls = await adapter.calls(PHONE)
        self.assertEqual(calls, [{'id':'call1','state':'active','multiparty':False}])
        self.assertNotIn('private-fixture', str(calls))
        with self.assertRaises(GatewayError):
            await adapter.operate('11:22:33:44:55:66', 'hangup', call_id='call1')
        await adapter.operate(PHONE, 'hangup', call_id='call1')
        self.assertEqual(protocol.commands, ['ATD1234567890;', 'AT+CHUP'])
        await adapter.close()

    async def test_existing_call_is_not_dialed_over_or_hung_up_on_shutdown(self):
        adapter, protocol, _ = fixture()
        protocol.current = [SimpleNamespace(index=1,status=0,multi_party=0,direction=1,number='other-fixture',type=129)]
        with self.assertRaises(GatewayError) as error:
            await adapter.operate(PHONE, 'dial', '1234567890')
        self.assertEqual(error.exception.code, 'phone_busy')
        await adapter.close()
        self.assertEqual(protocol.commands, [])

    async def test_remote_end_and_reused_id_do_not_hang_up_new_call(self):
        adapter, protocol, _ = fixture()
        await adapter.operate(PHONE, 'dial', '1234567890')
        protocol.current = []
        self.assertEqual(await adapter.calls(PHONE), [])
        protocol.current = [SimpleNamespace(index=1,status=4,multi_party=0,direction=1,number='other-fixture',type=129)]
        await adapter.close()
        self.assertNotIn('AT+CHUP', protocol.commands)

    async def test_incoming_reused_id_does_not_need_an_empty_poll(self):
        adapter, protocol, _ = fixture()
        await adapter.operate(PHONE, 'dial', '1234567890')
        protocol.current = [SimpleNamespace(index=1,status=4,multi_party=0,direction=1,number='other-fixture',type=129)]
        await adapter.close()
        self.assertNotIn('AT+CHUP', protocol.commands)

    async def test_streaming_resampler_and_no_phone_loopback(self):
        adapter, _, sent = fixture()
        audio = MacSCOAudio(PHONE, adapter)
        await audio.start()
        await audio.write(b'\xe8\x03' * 480)
        for _ in range(12):
            adapter.link.sink(PACKET)
        first, second = await audio.read(), await audio.read()
        self.assertEqual((len(first), len(second)), (956, 960))
        self.assertEqual(sent[0], b'\xe8\x03' * 30)
        self.assertNotEqual(sent[0], b'\x32\x00' * 30)
        await audio.close()
        self.assertFalse(audio.output)
        self.assertFalse(audio.input)
        self.assertIsNone(adapter.audio)
        await adapter.close()

    async def test_backpressure_fails_without_dropping_active_speech(self):
        adapter, _, _ = fixture()
        audio = MacSCOAudio(PHONE, adapter)
        await audio.start()
        for _ in range(10):
            await audio.write(bytes(960))
        self.assertEqual(len(audio.output), audio.MAX_PLAYBACK_BYTES)
        with self.assertRaises(GatewayError) as error:
            await audio.write(bytes(960))
        self.assertEqual(error.exception.code, 'audio_backpressure')
        self.assertEqual(len(audio.output), audio.MAX_PLAYBACK_BYTES)
        await audio.close()
        await adapter.close()

    async def test_existing_api_binary_pcm_contract(self):
        adapter, _, _ = fixture()
        client = TestClient(TestServer(create_app(adapter, lambda selected: MacSCOAudio(selected,adapter))))
        await client.start_server()
        try:
            response = await client.post('/v1/lease')
            headers = {'X-Controller-Lease':(await response.json())['lease']}
            ws = await client.ws_connect('/v1/audio?address='+PHONE, headers=headers)
            self.assertEqual(await ws.receive_json(timeout=2), {'type':'audio.ready'})
            for _ in range(6):
                adapter.link.sink(SimpleNamespace(data=bytes(60), packet_status=0))
            pcm = await ws.receive(timeout=2)
            self.assertEqual(len(pcm.data), 956)
            await ws.send_bytes(bytes(960))
            await ws.close()
            await asyncio.wait_for(client.server.app['state'].audio_done.wait(), 2)
            self.assertIsNone(adapter.audio)
        finally:
            await client.close()


class AudioEpochTests(unittest.IsolatedAsyncioTestCase):
    """F3: 旧 SCO 链路迟到的失败/断开不得污染新 link。"""

    async def test_late_old_sco_callbacks_do_not_poison_new_link(self):
        adapter, protocol, sent = fixture()
        old = adapter.link
        await adapter.operate(PHONE, 'dial', '123')
        first = adapter.owned_signature
        adapter.sco_failed(1)
        self.assertTrue(await adapter.hangup_owned(PHONE, 'call1', first))
        # 新链路建立后，上一通的 SCO 错误对本通无效。
        new = sco_link(43)
        adapter.connected(new)
        await adapter.operate(PHONE, 'dial', '123')
        audio = MacSCOAudio(PHONE, adapter)
        await audio.start()
        self.assertIsNone(adapter.audio_failure)
        self.assertEqual(adapter.audio_epoch, 2)
        # 迟到的旧链路断开和旧包不能清空新 link、不能送入新通话。
        old.emit(old.EVENT_DISCONNECTION)
        self.assertIs(adapter.link, new)
        audio.check()
        if old.sink:
            old.sink(PACKET)
        self.assertEqual(sent, [])
        self.assertTrue(audio.capture.empty())
        new.sink(PACKET)
        self.assertEqual(len(sent), 1)
        # 已有 link 时的失败事件不属于它；真正的新 link 断开仍被识别。
        adapter.sco_failed(1)
        audio.check()
        new.emit(new.EVENT_DISCONNECTION)
        self.assertIsNone(adapter.link)
        with self.assertRaises(GatewayError) as raised:
            audio.check()
        self.assertEqual(raised.exception.code, 'sco_closed')
        adapter.sco_failed(1)
        with self.assertRaises(GatewayError) as raised:
            audio.check()
        self.assertEqual(raised.exception.code, 'sco_failed')
        await audio.close()
        await adapter.close()

    async def test_new_sco_request_clears_only_audio_errors(self):
        adapter, _, _ = fixture()
        adapter.link.sink(SimpleNamespace(data=b'\x01', packet_status=0))
        self.assertEqual(adapter.audio_failure.code, 'invalid_sco_pcm')
        adapter.link.emit(adapter.link.EVENT_DISCONNECTION)
        adapter.requested(0)
        self.assertIsNone(adapter.audio_failure)
        await asyncio.gather(*adapter.accept_tasks, return_exceptions=True)
        self.assertEqual(adapter.audio_failure.code, 'sco_accept_failed')
        adapter.connected(sco_link(44))
        self.assertIsNone(adapter.audio_failure)
        adapter.connection.emit('disconnection', 8)
        adapter.connected(sco_link(45))
        audio = MacSCOAudio(PHONE, adapter)
        with self.assertRaises(GatewayError) as raised:
            audio.check()
        self.assertEqual(raised.exception.code, 'phone_disconnected')
        await adapter.close()


class HangupOwnedTests(unittest.IsolatedAsyncioTestCase):
    async def test_true_false_and_unknown(self):
        adapter, protocol, _ = fixture()
        self.assertFalse(await adapter.hangup_owned(PHONE, 'call1', None))
        await adapter.operate(PHONE, 'dial', '123')
        identity = adapter.owned_signature
        self.assertFalse(await adapter.hangup_owned(PHONE, 'call2', identity))
        self.assertFalse(await adapter.hangup_owned(PHONE, 'call1', identity[:-1] + (identity[-1] + 1,)))
        self.assertNotIn('AT+CHUP', protocol.commands)
        async def unknown(command, **_):
            raise ConnectionError('private-value')
        adapter.control.original = unknown
        with self.assertRaises(ConnectionError):
            await adapter.hangup_owned(PHONE, 'call1', identity)
        self.assertEqual(adapter.owned_signature, identity)
        adapter.control.original = Protocol.execute_command.__get__(protocol)
        self.assertTrue(await adapter.hangup_owned(PHONE, 'call1', identity))
        self.assertEqual(protocol.commands.count('AT+CHUP'), 1)
        self.assertIsNone(adapter.owned_signature)
        self.assertFalse(await adapter.hangup_owned(PHONE, 'call1', identity))
        await adapter.close()

    async def test_identity_is_rechecked_right_before_sending(self):
        adapter, protocol, _ = fixture()
        await adapter.operate(PHONE, 'dial', '123')
        identity = adapter.owned_signature
        async with adapter.control.lock:
            pending = asyncio.create_task(adapter.hangup_owned(PHONE, 'call1', identity))
            await asyncio.sleep(0.05)
            self.assertFalse(pending.done())
            # 发送前的最后一刻，手机报告没有任何电话：本次身份已失效。
            protocol.ag_indicators = [SimpleNamespace(indicator=key, current_status=0) for key in (hfp.AgIndicator.CALL, hfp.AgIndicator.CALL_SETUP, hfp.AgIndicator.CALL_HELD)]
            protocol.emit(hfp.HfProtocol.EVENT_AG_INDICATOR, protocol.ag_indicators[0])
        self.assertFalse(await pending)
        self.assertNotIn('AT+CHUP', protocol.commands)
        await adapter.close()


if __name__ == '__main__':
    unittest.main()
