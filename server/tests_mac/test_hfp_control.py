import asyncio
import unittest
from types import SimpleNamespace
from pyee import EventEmitter
from bumble import hfp
from server.mac.hfp_control import HFPControl
from server.mac.adapter import MacPhoneAdapter
from server.common.call_state import CallState
from server.common.contracts import GatewayError
from server.common.api import create_app
from aiohttp.test_utils import TestClient, TestServer

PHONE = 'AA:BB:CC:DD:EE:FF'
ROW = b'\r\n+CLCC: 1,0,0,0,0,"fixture",129\r\n'
OK = b'\r\nOK\r\n'


class DLC:
    def __init__(self):
        channel = EventEmitter()
        channel.EVENT_CLOSE = 'close'
        self.multiplexer = SimpleNamespace(l2cap_channel=channel)
        self.count = 0
        self.silent = False

    def write(self, command):
        self.count += 1
        if self.silent:
            return
        if self.count == 2:
            self.sink(ROW)
            asyncio.get_running_loop().call_later(1.35, self.sink, OK)
        elif self.count == 3:
            asyncio.get_running_loop().call_later(.3, self.sink, ROW + OK)
        else:
            self.sink(ROW + OK)


def protocol(dlc):
    return hfp.HfProtocol(dlc, hfp.HfConfiguration([], [], [hfp.AudioCodec.CVSD]))


class HFPControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_terminal_after_single_and_repeated_cancel_is_fail_closed(self):
        async def check(repeated):
            dlc = DLC()
            dlc.silent = True
            hf = protocol(dlc)
            control = HFPControl(hf)
            pending = asyncio.create_task(control.execute('AT+CLCC'))
            await asyncio.sleep(.01)
            pending.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(control.synchronized)
            self.assertTrue(control.lock.locked())
            if repeated: pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 3.5)
            self.assertFalse(control.lock.locked())
            self.assertIsNone(hf.pending_command)
            with self.assertRaises(GatewayError):
                await control.execute('AT+CHUP')
            self.assertEqual(dlc.count, 1)
        await asyncio.gather(check(False), check(True))

    async def test_actual_observer_websocket_cancellation_cannot_create_false_ended(self):
        class ObserverDLC(DLC):
            def __init__(self):
                super().__init__()
                self.second_started = asyncio.Event()
            def write(self, command):
                self.count += 1
                if self.count == 1:
                    self.sink(ROW + OK)
                elif self.count == 2:
                    self.sink(ROW)
                    self.second_started.set()
                    asyncio.get_running_loop().call_later(.4, self.sink, OK)
                else:
                    asyncio.get_running_loop().call_later(.6, self.sink, ROW + OK)
        dlc = ObserverDLC()
        hf = protocol(dlc)
        hf.ag_indicators = [hfp.AgIndicatorState(hfp.AgIndicator.CALL, 0, {0, 1}, 1)]
        async def guarded(operation, timeout):
            return await asyncio.wait_for(operation, timeout)
        device = SimpleNamespace(EVENT_SCO_REQUEST='request', EVENT_SCO_CONNECTION='connected', EVENT_SCO_CONNECTION_FAILURE='failed')
        adapter = MacPhoneAdapter(PHONE, device, EventEmitter(), hf, SimpleNamespace(guarded=guarded, failure=None))
        adapter.own(SimpleNamespace(index=1, direction=0, number='fixture', type=129))
        tracker = CallState(adapter, {'address': PHONE, 'callId': 'call1', 'identity': adapter.owned_signature}, lambda: None)
        queue = tracker.subscribe()
        client = TestClient(TestServer(create_app(adapter, lambda _: None)))
        await client.start_server()
        try:
            await queue.get()
            tracker.start()
            self.assertEqual((await asyncio.wait_for(queue.get(), 1))['state'], 'active')
            ws = await client.ws_connect('/v1/events')
            await asyncio.wait_for(dlc.second_started.wait(), 1)
            await ws.close()
            await asyncio.sleep(.05)
            self.assertFalse(adapter.control.synchronized)
            await hf.update_ag_indicator(1, 1)
            try:
                await asyncio.wait_for(queue.get(), 1.2)
            except asyncio.TimeoutError:
                pass
            self.assertEqual(tracker.state, 'active')
            self.assertTrue(adapter.control.synchronized)
            self.assertFalse(adapter.control.lock.locked())
            self.assertIsNone(hf.pending_command)
            self.assertIsNotNone(adapter.owned_signature)
            self.assertEqual(dlc.count, 3)
        finally:
            await tracker.close()
            adapter.call_attempted = False
            await client.close()

    async def test_cancelled_command_drains_its_terminal_before_next_command(self):
        dlc = DLC()
        hf = protocol(dlc)
        control = HFPControl(hf)
        await control.execute('AT+CLCC')
        pending = asyncio.create_task(control.execute('AT+CLCC'))
        await asyncio.sleep(.01)
        pending.cancel()
        following = asyncio.create_task(control.execute('AT+CLCC', response_type=hfp.AtResponseType.MULTIPLE))
        await asyncio.sleep(.02)
        self.assertFalse(control.synchronized)
        self.assertEqual(dlc.count, 2)
        with self.assertRaises(asyncio.CancelledError):
            await pending
        rows = await following
        self.assertEqual(len(rows), 1)
        self.assertTrue(control.synchronized)
        self.assertEqual(dlc.count, 3)

    async def test_late_ok_does_not_become_a_false_empty_snapshot(self):
        dlc = DLC()
        hf = protocol(dlc)
        hf.ag_indicators = [hfp.AgIndicatorState(hfp.AgIndicator.CALL, 0, {0, 1}, 1)]
        async def guarded(operation, timeout):
            return await asyncio.wait_for(operation, timeout)
        device = SimpleNamespace(EVENT_SCO_REQUEST='request', EVENT_SCO_CONNECTION='connected', EVENT_SCO_CONNECTION_FAILURE='failed')
        adapter = MacPhoneAdapter(PHONE, device, EventEmitter(), hf, SimpleNamespace(guarded=guarded, failure=None))
        adapter.own(SimpleNamespace(index=1, direction=0, number='fixture', type=129))
        tracker = CallState(adapter, {'address': PHONE, 'callId': 'call1', 'identity': adapter.owned_signature}, lambda: None)
        queue = tracker.subscribe()
        try:
            self.assertEqual((await queue.get())['state'], 'unknown')
            tracker.start()
            self.assertEqual((await asyncio.wait_for(queue.get(), 1))['state'], 'active')
            await hf.update_ag_indicator(1, 1)
            self.assertEqual((await asyncio.wait_for(queue.get(), 2))['state'], 'unknown')
            self.assertEqual((await asyncio.wait_for(queue.get(), 1))['state'], 'active')
            self.assertEqual(tracker.state, 'active')
            self.assertEqual(dlc.count, 3)
        finally:
            await tracker.close()
            adapter.call_attempted = False
            await adapter.close()

    async def test_no_terminal_response_blocks_new_commands_instead_of_guessing(self):
        dlc = DLC()
        dlc.silent = True
        hf = protocol(dlc)
        control = HFPControl(hf)
        control.RECOVERY_SECONDS = .02
        with self.assertRaises(TimeoutError):
            await control.execute('AT+CLCC', timeout=.01)
        self.assertFalse(control.synchronized)
        with self.assertRaises(GatewayError) as failure:
            await control.execute('AT+CHUP')
        self.assertEqual(failure.exception.code, 'hfp_control_unsynchronized')
        self.assertEqual(dlc.count, 1)
