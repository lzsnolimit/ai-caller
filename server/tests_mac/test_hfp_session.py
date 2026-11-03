import asyncio
import unittest
from types import SimpleNamespace
from pyee import EventEmitter
from bumble import hfp
from server.mac.native.acipc_pair import serve_hfp
from server.mac.adapter import MacPhoneAdapter
from server.common.call_state import CallState

PHONE = 'AA:BB:CC:DD:EE:FF'


class DLC:
    def __init__(self):
        channel = EventEmitter()
        channel.EVENT_CLOSE = 'close'
        self.multiplexer = SimpleNamespace(l2cap_channel=channel)
        self.state = 3

    def write(self, command):
        self.sink(f'\r\n+CLCC: 1,0,{self.state},0,0,"fixture",129\r\n\r\nOK\r\n'.encode())


class HFPSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_notifications_reach_tracker_without_polling(self):
        dlc = DLC()
        protocol = hfp.HfProtocol(dlc, hfp.HfConfiguration([], [], [hfp.AudioCodec.CVSD]))
        protocol._slc_initialized = True
        protocol.ag_indicators = [hfp.AgIndicatorState(hfp.AgIndicator.CALL, 0, {0, 1}, 0)]
        device = SimpleNamespace(EVENT_SCO_REQUEST='request', EVENT_SCO_CONNECTION='connected', EVENT_SCO_CONNECTION_FAILURE='failed')
        connection = EventEmitter()
        async def guarded(operation, timeout):
            return await asyncio.wait_for(operation, timeout)
        adapter = MacPhoneAdapter(PHONE, device, connection, protocol, SimpleNamespace(guarded=guarded, failure=None))
        adapter.own(SimpleNamespace(index=1, direction=0, number='fixture', type=129))
        tracker = CallState(adapter, {'address': PHONE, 'callId': 'call1', 'identity': adapter.owned_signature}, lambda: None)
        queue = tracker.subscribe()
        stop = asyncio.Event()
        closed = asyncio.Event()
        async def operation():
            try:
                tracker.start()
                await stop.wait()
            finally:
                await tracker.close()
                adapter.call_attempted = False
                await adapter.close()
                closed.set()
        task = asyncio.create_task(serve_hfp(protocol, operation()))
        try:
            self.assertEqual((await queue.get())['state'], 'unknown')
            self.assertEqual((await asyncio.wait_for(queue.get(), 1))['state'], 'alerting')
            dlc.state = 0
            dlc.sink(b'\r\n+CIEV: 1,1\r\n')
            self.assertEqual((await asyncio.wait_for(queue.get(), 1))['state'], 'active')
            dlc.multiplexer.l2cap_channel.emit('close')
            await asyncio.wait_for(task, 1)
            self.assertTrue(closed.is_set())
            self.assertEqual(tracker.state, 'ended')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancellation_cleans_service_and_consumer(self):
        protocol = SimpleNamespace(run=lambda: asyncio.Event().wait())
        started, closed = asyncio.Event(), asyncio.Event()
        async def operation():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        task = asyncio.create_task(serve_hfp(protocol, operation()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())

    async def test_acl_disconnection_prevents_stale_control_queries(self):
        dlc = DLC()
        protocol = hfp.HfProtocol(dlc, hfp.HfConfiguration([], [], [hfp.AudioCodec.CVSD]))
        device = SimpleNamespace(EVENT_SCO_REQUEST='request', EVENT_SCO_CONNECTION='connected', EVENT_SCO_CONNECTION_FAILURE='failed')
        connection = EventEmitter()
        adapter = MacPhoneAdapter(PHONE, device, connection, protocol, SimpleNamespace(failure=None))
        connection.emit('disconnection', 8)
        with self.assertRaises(Exception) as result:
            await adapter.call_snapshot(PHONE, 'call1', (1, 0, 'fixture', 129, 0))
        self.assertEqual(result.exception.code, 'phone_disconnected')
        adapter.call_attempted = True
        await adapter.close()
