"""Bounded runtime-only transcript cache and a separate worker text channel."""
import asyncio
from collections import OrderedDict, deque
import json
import math
import secrets
import socket


def integer(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


class TranscriptBuffer:
    MAX_EVENTS = 512
    MAX_CHARS = 131072

    def __init__(self, expired=False, sink=None):
        self.sink = sink    # 每条接受的转写事件先交给持久化，再受缓存上限影响
        self.cache_id = secrets.token_hex(16)
        self.events = deque()
        self.chars = self.revision = self.worker_sequence = 0
        self.dropped = self.invalid = 0
        self.truncated = expired
        self.listeners = set()

    def notify(self):
        for queue in tuple(self.listeners):
            if not queue.full():
                queue.put_nowait(True)

    def accept(self, value):
        if not isinstance(value, dict) or value.get('type') not in ('transcript.delta', 'transcript.status'):
            self.invalid += 1
            self.notify()
            return
        sequence = value.get('sequence')
        if not integer(sequence):
            self.invalid += 1
            self.notify()
            return
        previous = (self.dropped, self.invalid)
        for field in ('dropped', 'invalid'):
            if integer(value.get(field)):
                setattr(self, field, max(getattr(self, field), value[field]))
        if value['type'] == 'transcript.status':
            # Missing records include the last dropped delta, even if no later text arrives.
            if sequence > self.worker_sequence:
                self.truncated = True
                self.worker_sequence = sequence
            if previous != (self.dropped, self.invalid):
                self.notify()
            return
        text, role = value.get('text'), value.get('role')
        times = (value.get('startMs'), value.get('endMs'))
        if (not isinstance(text, str) or not 0 < len(text) <= 8192 or role not in ('caller', 'ai') or
            not isinstance(value.get('eventId'), str) or not 0 < len(value['eventId']) <= 256 or
            any(type(t) not in (int, float) or not math.isfinite(t) or t < 0 for t in times) or times[1] < times[0]):
            self.invalid += 1
            self.notify()
            return
        if sequence <= self.worker_sequence:
            return
        gap = sequence != self.worker_sequence + 1
        self.worker_sequence = sequence
        self.truncated |= gap
        self.revision += 1
        event = {'seq': self.revision, 'role': role, 'eventId': value['eventId'], 'text': text, 'startMs': times[0], 'endMs': times[1], 'gapBefore': gap}
        self.events.append(event)
        if self.sink:
            self.sink(event)
        self.chars += len(text)
        while len(self.events) > self.MAX_EVENTS or self.chars > self.MAX_CHARS:
            self.chars -= len(self.events.popleft()['text'])
            self.truncated = True
        self.notify()

    def snapshot(self, key, state, after=0, cache_id=None):
        first = self.events[0]['seq'] if self.events else self.revision + 1
        reset = cache_id != self.cache_id or after > self.revision
        gap = self.truncated or (not reset and after < first - 1)
        return {'type': 'transcript.update', 'conversationId': key, 'cacheId': self.cache_id,
                'revision': self.revision, 'firstSeq': first, 'reset': reset, 'gap': gap,
                'state': state, 'dropped': self.dropped, 'invalid': self.invalid,
                'events': [e for e in self.events if reset or e['seq'] > after]}


class Transcripts:
    MAX_CONVERSATIONS = 64

    def __init__(self, sink=None):
        self.buffers = OrderedDict()
        self.sink = sink    # sink(conversation id, event)

    def get(self, item):
        key = item['id']
        if key not in self.buffers:
            sink = (lambda event: self.sink(key, event)) if self.sink else None
            self.buffers[key] = TranscriptBuffer(expired=item.get('transcriptSeen', False), sink=sink)
            item['transcriptSeen'] = True
            while len(self.buffers) > self.MAX_CONVERSATIONS:
                _, old = self.buffers.popitem(last=False)
                old.events.clear()
                old.chars = 0
                old.truncated = True
                old.notify()
        self.buffers.move_to_end(key)
        return self.buffers[key]


class WorkerTranscripts:
    def __init__(self, owner, item):
        self.owner, self.item = owner, item
        self.parent, self.child = socket.socketpair()
        self.parent.setblocking(False)
        self.task = None
        self.writer = None

    async def run(self):
        try:
            reader, self.writer = await asyncio.open_connection(sock=self.parent, limit=65536)
            count = 0
            while raw := await reader.readline():
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    value = None
                self.owner.transcripts.get(self.item).accept(value)
                count += 1
                if count % 32 == 0:
                    await asyncio.sleep(0)
        except (ValueError, OSError):
            buffer = self.owner.transcripts.get(self.item)
            buffer.truncated = True
            buffer.notify()
        finally:
            if self.writer:
                self.writer.close()

    async def close(self):
        self.child.close()
        if self.task:
            # Drain already sent text, but never wait for a hung worker.
            try:
                await asyncio.wait_for(asyncio.shield(self.task), .15)
            except asyncio.TimeoutError:
                buffer = self.owner.transcripts.get(self.item)
                buffer.truncated = True
                buffer.notify()
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.writer:
            self.writer.close()
        self.parent.close()
