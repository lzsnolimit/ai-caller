"""Server-owned, bounded reconciliation separate from the audio lifecycle."""
import asyncio
import contextlib


class CallState:
    QUERY_SECONDS = 6
    STATES = {'dialing', 'alerting', 'active', 'held', 'incoming', 'waiting', 'ended'}

    def __init__(self, adapter, item, notify):
        self.adapter, self.item, self.notify = adapter, item, notify
        self.state, self.last_known, self.revision = 'unknown', 'unknown', 0
        self.listeners = set()
        self.dirty = asyncio.Event()
        self.dirty.set()
        self.unsubscribe = None
        self.task = None

    def snapshot(self):
        return {'state': self.state, 'lastKnownState': self.last_known, 'revision': self.revision}

    def set(self, state):
        state = state if state in self.STATES else 'unknown'
        if state == self.state:
            return
        self.state = state
        if state != 'unknown':
            self.last_known = state
        self.revision += 1
        for queue in tuple(self.listeners):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(self.snapshot())
        self.notify()

    def subscribe(self):
        queue = asyncio.Queue(maxsize=1)
        self.listeners.add(queue)
        queue.put_nowait(self.snapshot())
        return queue

    def start(self):
        subscribe = getattr(self.adapter, 'subscribe_calls', None)
        if subscribe:
            self.unsubscribe = subscribe(self.item['address'], self.dirty.set)
        self.task = asyncio.create_task(self.run())

    async def query(self):
        item = self.item
        snapshot = getattr(self.adapter, 'call_snapshot', None)
        if snapshot:
            return await snapshot(item['address'], item['callId'], item['identity'])
        identity = await self.adapter.call_identity(item['address'], item['callId'])
        if identity != item['identity']:
            return 'ended'
        calls = await self.adapter.calls(item['address'])
        return next((c['state'] for c in calls if c['id'] == item['callId']), 'ended')

    async def run(self):
        while self.state != 'ended':
            await self.dirty.wait()
            self.dirty.clear()
            for attempt in range(3):
                try:
                    state = await asyncio.wait_for(self.query(), self.QUERY_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Event-scoped recovery, not periodic polling or an audio failure.
                    self.set('unknown')
                    if attempt < 2:
                        await asyncio.sleep(0.25 * (attempt + 1))
                else:
                    self.set(state)
                    break
            # Coalesce bursts and yield before handling notifications raised during a query.
            await asyncio.sleep(0.05)

    async def close(self, final='ended'):
        """Stop reconciliation. `final` is 'ended' only when the phone call is confirmed ended."""
        if self.unsubscribe:
            self.unsubscribe()
            self.unsubscribe = None
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.set(final)
