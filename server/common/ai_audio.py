"""Private full-duplex audio for one server-owned AI worker."""
import asyncio
import contextlib
import json
import socket
import struct
import time

from .contracts import GatewayError
from .diagnostics import exception_fields

STARTUP_SECONDS = 45


class AudioMetrics:
    """Fixed-size aggregates. The diagnostic sink is bounded and asynchronous."""
    def __init__(self, emit, clock=time.monotonic):
        self.emit, self.clock = emit, clock
        self.origin = self.last = clock()
        self.values = {}

    def observe(self, stage, seconds=0, size=0, depth=0):
        if stage not in self.values and len(self.values) >= 32:
            return
        value = self.values.setdefault(stage, [0, 0., 0., 0, 0])
        value[0] += 1
        value[1] += seconds * 1000
        value[2] = max(value[2], seconds * 1000)
        value[3] += size
        value[4] = max(value[4], depth)
        if self.clock() - self.last >= 10:
            self.flush()

    def flush(self):
        self.last = self.clock()
        for stage, (count, total, maximum, size, depth) in self.values.items():
            self.emit({'event': 'audio_stage', 'stage': stage, 'clockDomain': 'python_monotonic',
                       'elapsedMs': round((self.last - self.origin) * 1000), 'frames': count,
                       'meanMs': total / count, 'maxMs': maximum, 'bytes': size, 'peakDepth': depth})
        self.values.clear()


class WorkerAudio:
    """Owned by one CallSession. The session supplies hub, item, diagnostics, tracker, notify and publish."""
    def __init__(self, session):
        self.session, self.item = session, session.item
        self.parent, self.child = socket.socketpair()
        self.parent.setblocking(False)
        self.child.set_inheritable(True)
        self.task = self.writer = self.bridge = self.state = None
        self.lock = asyncio.Lock()
        self.finishing = False
        self.failure_recorded = False
        self.metrics = AudioMetrics(lambda event: session.diagnostics.write(session.item['id'], event))

    async def claim(self):
        hub = self.session.hub
        async with hub.lock:
            if hub.lease or hub.audio_ws:
                raise GatewayError('audio_busy', 409)
            self.state = hub
            hub.audio_ws = self
            hub.audio_done.clear()
            # Internal ownership has no network lease renewal or transferable token.
            hub.lease, hub.expires = 'internal-' + self.item['id'], float('inf')

    async def send(self, kind, data):
        async with self.lock:
            start = time.monotonic()
            self.writer.write(struct.pack('!IB', len(data) + 1, kind) + data)
            await asyncio.wait_for(self.writer.drain(), .2)
            self.metrics.observe('capture_transport' if kind == 0 else 'control_transport', time.monotonic()-start, len(data), self.writer.transport.get_write_buffer_size())

    async def event(self, value):
        await self.send(1, json.dumps(value).encode())

    def record_failure(self, error, stage):
        if self.failure_recorded:
            return
        self.failure_recorded = True
        fields = exception_fields(error, stage)
        fields.update(clockDomain='python_monotonic', durationMs=round((time.monotonic()-self.metrics.origin)*1000))
        self.session.diagnostics.write(self.item['id'], fields)
        # 收尾开始后，或手机已报告通话结束后，停止 worker 和关闭 SCO 会让传输断开；
        # 这是结束的结果，只记诊断，不算本通故障。
        tracker = getattr(self.session, 'tracker', None)
        ended = tracker is not None and tracker.state == 'ended'
        if fields['errorCode'] != 'sco_closed' and not self.session.finishing and not ended:
            self.item['error'] = self.item.get('error') or fields['errorCode']

    async def run(self):
        tasks = []
        tracker = self.session.tracker
        updates = tracker.subscribe()
        stage = 'reader_ready'
        started = False
        try:
            reader, self.writer = await asyncio.open_connection(sock=self.parent, limit=2048)
            self.writer.transport.set_write_buffer_limits(high=9600, low=4800)
            deadline = asyncio.get_running_loop().time() + STARTUP_SECONDS
            try:
                async with asyncio.timeout_at(deadline):
                    size, = struct.unpack('!I', await reader.readexactly(4))
                    if not 2 <= size <= 4096:
                        raise GatewayError('invalid_audio_handshake', 503)
                    message = await reader.readexactly(size)
                    try:
                        valid = message[0] == 1 and json.loads(message[1:]) == {'type': 'audio.reader.ready'}
                    except (ValueError, UnicodeError):
                        valid = False
                    if not valid:
                        raise GatewayError('invalid_audio_handshake', 503)
            except TimeoutError as error:
                raise GatewayError('audio_reader_timeout', 503) from error
            self.metrics.observe('reader_ready', time.monotonic()-self.metrics.origin)
            stage = 'sco_start'
            self.bridge = self.state.audio_factory(self.item['address'])
            self.bridge.metrics = self.metrics
            # A worker which exits during SCO startup must release the pending bridge.
            startup = asyncio.create_task(self.bridge.start())
            inbound = asyncio.create_task(reader.readexactly(4))
            tasks.extend([startup, inbound])
            done, _ = await asyncio.wait(tasks, timeout=max(0, deadline-asyncio.get_running_loop().time()), return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise GatewayError('audio_ready_timeout', 503)
            if inbound in done:
                inbound.result()
                raise GatewayError('audio_not_ready', 409)
            await startup
            self.state.audio_ready = True
            self.session.notify()
            await self.event({'type': 'audio.ready'})
            started = True

            async def capture():
                while True:
                    data = await self.bridge.read()
                    if not data or len(data) % 2 or len(data) > 960:
                        raise GatewayError('invalid_capture_frame', 503)
                    await self.send(0, data)
                    self.session.publish(0, data)

            async def playback():
                pending = inbound
                while True:
                    header = await pending
                    size, = struct.unpack('!I', header)
                    if size < 3 or size > 961:
                        raise GatewayError('invalid_pcm_frame')
                    data = await reader.readexactly(size)
                    if data[0] != 0 or len(data[1:]) % 2:
                        raise GatewayError('invalid_pcm_frame')
                    start = time.monotonic()
                    await self.bridge.write(data[1:])
                    self.metrics.observe('playback_handoff', time.monotonic()-start, len(data)-1)
                    self.session.publish(1, data[1:])
                    pending = reader.readexactly(4)

            async def states():
                while True:
                    value = await updates.get()
                    await self.event({'type': 'call.state', **value})

            async def tracked(stage, job):
                try:
                    await job
                except asyncio.CancelledError:
                    raise
                except asyncio.IncompleteReadError:
                    raise  # A normal worker close ends the established stream.
                except Exception as error:
                    self.record_failure(error, stage)
                    raise

            tasks.extend(asyncio.create_task(tracked(label, job)) for label, job in (
                ('capture_transport', capture()), ('playback_handoff', playback()),
                ('control_transport', states()), ('audio_monitor', self.bridge.monitor())))
            done, _ = await asyncio.wait(tasks[2:], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.IncompleteReadError as error:
            if not started and not self.finishing and self.item.get('state') != 'ending':
                self.record_failure(error, stage)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.record_failure(error, stage)
            self.metrics.observe('transport_failure')
            with contextlib.suppress(Exception):
                await self.event({'type': 'audio.closed'} if isinstance(error, GatewayError) and error.code == 'sco_closed' else {'type': 'error'})
        finally:
            self.finishing = True
            tracker.listeners.discard(updates)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.bridge:
                try:
                    await self.bridge.close()
                except Exception as error:
                    self.session.diagnostics.write(self.item['id'], exception_fields(error, 'audio_cleanup'))
                    self.state.audio_cleanup_failed = True
                    self.item['error'] = self.item.get('error') or 'audio_cleanup_failed'
                    self.item['cleanupPending'] = True
            if self.writer:
                self.writer.close()
                with contextlib.suppress(Exception):
                    await self.writer.wait_closed()
            self.parent.close()
            self.child.close()
            self.metrics.flush()
            if self.state:
                self.state.audio_ready = False
                if not self.state.audio_cleanup_failed:
                    self.state.audio_ws = None
                    self.state.lease = None
                    self.state.expires = 0
                self.state.audio_done.set()
            self.session.notify()

    def release_state(self):
        if self.state and self.state.audio_ws is self:
            self.state.audio_ws = None
            self.state.lease = None
            self.state.expires = 0
            self.state.audio_done.set()

    async def close(self, retry=False, **_kwargs):
        if self.task:
            if not self.finishing and not self.task.cancelling():
                self.task.cancel()
            await asyncio.shield(asyncio.gather(self.task, return_exceptions=True))
            if retry and self.bridge and self.state and self.state.audio_ws is self and self.state.audio_cleanup_failed:
                # Explicit second attempt. The owner keeps the handle until the bridge is really closed.
                try:
                    await self.bridge.close()
                except Exception as error:
                    self.session.diagnostics.write(self.item['id'], exception_fields(error, 'audio_cleanup'))
                    return
                self.state.audio_cleanup_failed = False
                self.release_state()
        else:
            self.parent.close()
            self.child.close()
            self.release_state()
