"""每个会话一个目录的持久化：meta.json 原子写，transcript.jsonl 逐条追加。写盘失败只记诊断。"""
import asyncio
import json
import os
import queue
import re
import secrets
import threading
import time

from .diagnostics import exception_fields

META_KEYS = ('id', 'direction', 'address', 'number', 'created', 'answeredAt', 'endedAt', 'state', 'endReason',
             'cleanup', 'error', 'taskId', 'voice', 'inputBytes', 'outputBytes')
EVENT_KEYS = ('seq', 'role', 'eventId', 'text', 'startMs', 'endMs')
KEY = re.compile(r'[a-f0-9]{32}')


def meta_of(item):
    return {key: item.get(key) for key in META_KEYS}


class ConversationRecords:
    """一个写线程按顺序处理所有写入和读取；读取排在已提交的追加之后，所以返回已写入的全部内容。"""
    def __init__(self, root, diagnostics):
        self.root = root / 'conversations'
        self.diagnostics = diagnostics
        self.pending = queue.Queue(maxsize=4096)
        self.written = {}       # conversation id -> last meta handed to the writer
        self.tracked = {}       # conversation id -> item still changing in memory
        self.closed = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='phone-conversation-records', daemon=True)
        self.thread.start()

    # ---- startup ----
    def load(self, limit):
        """最近 limit 条历史 meta；不读转写正文。损坏或不完整的目录跳过。"""
        items = []
        try:
            folders = [entry for entry in self.root.iterdir() if entry.is_dir() and KEY.fullmatch(entry.name)]
        except OSError:
            return items
        for folder in folders:
            try:
                meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(meta, dict) or meta.get('id') != folder.name or not isinstance(meta.get('created'), (int, float)):
                continue
            items.append({key: meta.get(key) for key in META_KEYS})
        items.sort(key=lambda meta: meta['created'])
        return items[-limit:] if limit else items

    # ---- writes ----
    def track(self, item):
        self.tracked[item['id']] = item

    def sync(self, active=()):
        """内存条目变化时调用。只把变化过的 meta 交给写线程；已结束且不再变化的条目停止跟踪。"""
        for key, item in tuple(self.tracked.items()):
            meta = meta_of(item)
            if meta != self.written.get(key):
                self.written[key] = meta
                self.submit(('meta', key, meta))
            if item.get('state') not in active:
                self.tracked.pop(key, None)
                self.written.pop(key, None)

    def append(self, key, event):
        if not isinstance(key, str) or not KEY.fullmatch(key):
            return
        self.submit(('event', key, {name: event.get(name) for name in EVENT_KEYS}))

    def submit(self, job):
        if self.closed:
            return
        try:
            self.pending.put_nowait(job)
        except queue.Full:
            self.failure(job[1], OSError('record_queue_full'))

    def failure(self, key, error):
        if isinstance(key, str) and KEY.fullmatch(key):
            self.diagnostics.write(key, exception_fields(error, 'conversation_record'))

    # ---- reads ----
    async def transcript(self, key):
        """已写入的全部转写事件。排在同一队列里，所以先于它提交的追加都已落盘。"""
        if not isinstance(key, str) or not KEY.fullmatch(key):
            return []
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def deliver(value):
            if not future.done():
                future.set_result(value)
        if self.closed:
            return self.read_transcript(key)
        try:
            self.pending.put_nowait(('read', key, (loop, deliver)))
        except queue.Full:
            return await asyncio.to_thread(self.read_transcript, key)
        return await future

    def read_transcript(self, key):
        events = []
        try:
            with open(self.root / key / 'transcript.jsonl', 'r', encoding='utf-8') as source:
                for line in source:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict) and isinstance(value.get('text'), str):
                        events.append({name: value.get(name) for name in EVENT_KEYS})
        except (OSError, UnicodeError):
            pass
        return events

    # ---- writer thread ----
    def folder(self, key):
        directory = self.root / key
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        return directory

    def write_meta(self, key, meta):
        directory = self.folder(key)
        temporary = directory / ('.' + secrets.token_hex(8))
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump({**meta, 'savedAt': time.time()}, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, directory / 'meta.json')
        finally:
            if temporary.exists():
                temporary.unlink()

    def write_event(self, key, event):
        directory = self.folder(key)
        descriptor = os.open(directory / 'transcript.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a', encoding='utf-8') as output:
            output.write(json.dumps(event, ensure_ascii=False) + '\n')

    def run(self):
        while not self.stop.is_set() or not self.pending.empty():
            try:
                kind, key, value = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if kind == 'meta':
                    self.write_meta(key, value)
                elif kind == 'event':
                    self.write_event(key, value)
                else:
                    loop, deliver = value
                    events = self.read_transcript(key)
                    try:
                        loop.call_soon_threadsafe(deliver, events)
                    except RuntimeError:
                        pass
            except OSError as error:
                self.failure(key, error)

    async def close(self):
        self.closed = True
        self.stop.set()
        await asyncio.to_thread(self.thread.join, 5)
