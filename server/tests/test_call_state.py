import asyncio
import unittest
from server.common.call_state import CallState


class Phone:
    def __init__(self):
        self.callbacks = {}
        self.states = {'one': 'active', 'two': 'held'}
        self.queries = []
        self.failures = 0

    def subscribe_calls(self, selected, callback):
        self.callbacks[selected] = callback
        return lambda: self.callbacks.pop(selected, None)

    async def call_snapshot(self, selected, call_id, identity):
        self.queries.append((selected, call_id, identity))
        if self.failures:
            self.failures -= 1
            raise TimeoutError()
        return self.states[selected]


class CallStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_has_no_polling_and_events_are_per_phone(self):
        phone = Phone()
        trackers = [CallState(phone, {'address': a, 'callId': 'call1', 'identity': a, 'id': a}, lambda: None) for a in ('one', 'two')]
        try:
            for tracker in trackers: tracker.start()
            await asyncio.sleep(.08)
            self.assertEqual([t.state for t in trackers], ['active', 'held'])
            count = len(phone.queries)
            await asyncio.sleep(.6)
            self.assertEqual(len(phone.queries), count)
            phone.states['one'] = 'ended'
            phone.callbacks['one']()
            await asyncio.sleep(.08)
            self.assertEqual([t.state for t in trackers], ['ended', 'held'])
            self.assertEqual(phone.queries[-1], ('one', 'call1', 'one'))
        finally:
            for tracker in trackers: await tracker.close()
        self.assertFalse(phone.callbacks)

    async def test_timeout_is_unknown_then_recovers_without_fake_end(self):
        phone = Phone()
        tracker = CallState(phone, {'address': 'one', 'callId': 'call1', 'identity': 'one', 'id': 'one'}, lambda: None)
        try:
            tracker.start()
            await asyncio.sleep(.08)
            queue = tracker.subscribe()
            self.assertEqual((await queue.get())['state'], 'active')
            phone.failures = 1
            phone.callbacks['one']()
            self.assertEqual((await asyncio.wait_for(queue.get(), 1))['state'], 'unknown')
            self.assertEqual(tracker.last_known, 'active')
            self.assertEqual((await asyncio.wait_for(queue.get(), 1))['state'], 'active')
            tracker.listeners.discard(queue)
            replay = tracker.subscribe()
            self.assertEqual((await replay.get())['state'], 'active')
        finally:
            await tracker.close()

    async def test_persistent_query_failure_stops_retrying_until_new_event(self):
        phone = Phone()
        phone.failures = 10
        tracker = CallState(phone, {'address': 'one', 'callId': 'call1', 'identity': 'one', 'id': 'one'}, lambda: None)
        try:
            tracker.start()
            await asyncio.sleep(.9)
            self.assertEqual(tracker.state, 'unknown')
            self.assertEqual(len(phone.queries), 3)
            await asyncio.sleep(.6)
            self.assertEqual(len(phone.queries), 3)
        finally:
            await tracker.close()
