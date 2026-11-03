"""Server-owned tasks over one line: one CallSession per dial, history as data only."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import time

from .contracts import GatewayError, address
from .storage import Store
from .diagnostics import Diagnostics
from .call_session import ACTIVE, COMMAND_TIMEOUT, CallSession, DeviceRecovery, Line
from .ai_audio import WorkerAudio
from .inbound import InboundWatcher, load_guard_prompt
from .records import ConversationRecords
from .voices import provider_settings, validate_voice

from .transcripts import Transcripts, WorkerTranscripts

DEFAULT_PROMPT = "You are a concise telephone AI assistant. Identify yourself as AI. Speak the caller's language. Do not claim actions you have not performed."
MAX_HISTORY = 256
MAX_INBOUND_PROMPT_BYTES = 16384
DIRECTIONS = ('outbound', 'inbound')
__all__ = ['Conversations', 'ACTIVE', 'COMMAND_TIMEOUT', 'DEFAULT_PROMPT']


class Conversations:
    def __init__(self, adapter, hub, store=None, spawn=None):
        self.adapter, self.hub = adapter, hub
        self.store = store or Store()
        self.diagnostics = Diagnostics(self.store.root)
        self.prompt = self.store.load("settings", "system-prompt", {"content": DEFAULT_PROMPT})["content"]
        self.guard_prompt = load_guard_prompt()
        saved = self.store.load("devices", "inbound-prompts", {})
        self.inbound_prompts = {k: v for k, v in saved.items() if isinstance(v, str) and v.strip()} if isinstance(saved, dict) else {}
        self.items, self.locks = {}, {}
        self.sessions = {}      # conversation id -> CallSession, only while run objects exist
        self.recovering = {}    # address -> DeviceRecovery, only while the phone call state is unknown
        self.line = Line()
        self.spawn = spawn or self.spawn_worker
        self.closing = False
        self.updates = set()
        self.records = ConversationRecords(self.store.root, self.diagnostics)
        self.transcripts = Transcripts(self.records.append)
        self.voice_preferences = {}
        self.inbound = InboundWatcher(self)
        self.load_history()

    def load_history(self):
        """历史 meta 进入会话列表（不加载转写正文）。上次进程未收尾的会话标为 failed。"""
        for meta in self.records.load(MAX_HISTORY):
            item = {**meta, "callId": None, "ai": "closed", "commands": [], "persisted": True,
                    "direction": meta.get("direction") if meta.get("direction") in DIRECTIONS else "outbound",
                    "inputBytes": meta.get("inputBytes") or 0, "outputBytes": meta.get("outputBytes") or 0}
            if item.get("state") in ACTIVE or item.get("state") is None:
                item.update(state="failed", error=item.get("error") or "server_restarted",
                            endReason=item.get("endReason") or "server_restart", cleanupPending=False)
                self.records.track(item)
            self.items[item["id"]] = item
        self.records.sync(ACTIVE)

    def start(self):
        """在事件循环里启动来电监听。"""
        self.inbound.start()

    def devices_changed(self):
        self.inbound.devices_changed()

    async def known_device(self, selected):
        known = {device['address'] for device in await self.adapter.devices() if device.get('paired')}
        for stale in self.voice_preferences.keys() - known:
            self.voice_preferences.pop(stale, None)
        if selected not in known:
            raise GatewayError('device_not_found', 404)

    def device_voice(self, selected):
        return self.voice_preferences.get(selected, 'marin')

    def inbound_prompt(self, selected):
        return self.inbound_prompts.get(selected)

    def set_inbound_prompt(self, selected, content):
        """空白内容恢复默认。保护 prompt 不在这里，也没有修改 API。"""
        if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_INBOUND_PROMPT_BYTES:
            raise GatewayError('invalid_inbound_prompt')
        previous = dict(self.inbound_prompts)
        if content.strip():
            self.inbound_prompts[selected] = content
        else:
            self.inbound_prompts.pop(selected, None)
        try:
            self.store.save('devices', 'inbound-prompts', self.inbound_prompts)
        except OSError:
            self.inbound_prompts = previous
            raise GatewayError('inbound_prompt_save_failed', 503) from None

    def notify(self):
        self.records.sync(ACTIVE)
        for buffer in tuple(self.transcripts.buffers.values()):
            buffer.notify()
        for recovery in tuple(self.recovering.values()):
            recovery.dirty.set()  # Any device or phone change re-verifies an isolated phone.
        for queue in tuple(self.updates):
            if not queue.full():
                queue.put_nowait(True)

    def subscribe_updates(self):
        queue = asyncio.Queue(maxsize=1)
        self.updates.add(queue)
        queue.put_nowait(True)
        return queue

    def tracker(self, key):
        session = self.sessions.get(key)
        return session.tracker if session else None

    def call_status(self, item):
        tracker = self.tracker(item['id'])
        if tracker and item['state'] in ACTIVE:
            return tracker.snapshot()
        final = item.get('callStatus') or {'state': 'unknown', 'lastKnownState': 'unknown', 'revision': 0}
        if item['state'] not in ACTIVE:
            return {'state': 'ended', 'lastKnownState': 'ended', 'revision': final.get('revision', 0)}
        return final

    def public(self, item):
        return {**{k: item.get(k) for k in ("id", "address", "state", "taskId", "created", "callId", "ai", "error", "inputBytes", "outputBytes")},
                "direction": item.get("direction", "outbound"),
                "diagnostics": {**item.get("diagnostics", {}), "droppedLogs": self.diagnostics.dropped},
                "voice": item.get("voice") or "marin", "callStatus": self.call_status(item), "commandRevision": item.get('commandRevision', 0),
                "endReason": item.get('endReason'), "cleanup": item.get('cleanup'),
                "answeredAt": item.get('answeredAt'), "endedAt": item.get('endedAt')}

    def listing(self, direction=None, selected=None):
        """会话列表（含持久化历史），可按方向和设备过滤。"""
        if direction is not None and direction not in DIRECTIONS:
            raise GatewayError("invalid_direction")
        if selected is not None:
            selected = address(selected)
        return [self.public(item) for item in self.items.values()
                if (direction is None or item.get("direction", "outbound") == direction)
                and (selected is None or item["address"] == selected)]

    def get(self, key):
        item = self.items.get(key)
        if not item:
            raise GatewayError("conversation_not_found", 404)
        return item

    def busy(self, selected):
        """Only a call in progress. An isolated phone can still connect, pair or be removed."""
        session = self.line.session
        return session is not None and session.address == selected

    def forget_device(self, selected):
        self.voice_preferences.pop(selected, None)
        if selected in self.inbound_prompts:
            with contextlib.suppress(GatewayError):
                self.set_inbound_prompt(selected, '')
        recovery = self.recovering.pop(selected, None)
        if recovery:
            recovery.abandon()

    def preflight(self):
        runner = Path(__file__).parent / "ai/node_modules/.bin/tsx"
        if not runner.is_file():
            raise GatewayError("install_server_ai_dependencies", 503)
        # Same provider rules as the worker; raises ai_provider_not_configured.
        provider_settings()

    def isolate(self, item):
        """Only this phone is held back until its call is confirmed ended. The line stays usable."""
        previous = self.recovering.get(item['address'])
        if previous and previous.item is item:
            return
        if previous:
            previous.abandon()
        recovery = self.recovering[item['address']] = DeviceRecovery(self, item)
        recovery.start()

    def trim_history(self):
        finished = [key for key, item in self.items.items() if item['state'] not in ACTIVE]
        for key in finished[:max(0, len(self.items) - MAX_HISTORY)]:
            self.items.pop(key, None)

    async def create(self, body):
        if not isinstance(body, dict):
            raise GatewayError("invalid_request")
        if self.closing:
            raise GatewayError("server_stopping", 503)
        if 'voice' in body:
            validate_voice(body['voice'])
        body = dict(body)
        selected = address(body.get("address"))
        number = body.get("number")
        if not isinstance(number, str) or not re.fullmatch(r"\+?[0-9]{1,20}", number):
            raise GatewayError("invalid_number")
        lock = self.locks.setdefault(selected, asyncio.Lock())
        async with lock:
            body['voice'] = body.get('voice', self.device_voice(selected))
            item = self.new_item(selected, "outbound", "dialing", body['voice'])
            session = CallSession(self, item)
            # Synchronous idle -> busy. No await between the check and the claim.
            self.line.acquire(session)
            try:
                if self.hub.lease:
                    raise GatewayError("line_busy", 409)  # An external controller holds the line.
                recovery = self.recovering.get(selected)
                if recovery and not await recovery.check():
                    raise GatewayError("device_recovering", 409)
                await self.adapter.device_path(selected)
                if await self.adapter.calls(selected):
                    raise GatewayError("phone_busy", 409)
                if self.spawn == self.spawn_worker:
                    self.preflight()
                task = await asyncio.to_thread(self.store.task, body.get("task"), self.prompt, body["voice"])
            except BaseException:
                self.line.release(session)
                raise
            self.register(item, session, task)
            try:
                await session.start(number, task)
                return self.public(item)
            except BaseException:
                item["error"] = item["error"] or "conversation_start_failed"
                if not item["callId"]:
                    # The platform can own a call although dial raised. Verify before touching it.
                    owned = getattr(getattr(self.adapter, "adapter", self.adapter), "owned_call_id", None)
                    if owned:
                        item["callId"] = owned
                        with contextlib.suppress(Exception):
                            item["identity"] = await self.adapter.call_identity(selected, owned)
                await session.finish("start_failed")
                raise

    def new_item(self, selected, direction, state, voice):
        return {"id": secrets.token_hex(16), "direction": direction, "address": selected, "state": state,
                "taskId": None, "voice": voice, "created": time.time(), "callId": None,
                "ai": "not_started", "error": None, "commands": [], "inputBytes": 0, "outputBytes": 0}

    def register(self, item, session, task):
        item.update(taskId=task["id"], voice=task["voice"])
        self.trim_history()
        self.items[item["id"]] = item
        self.sessions[item["id"]] = session
        self.records.track(item)
        self.notify()
        self.diagnostics.write(item["id"], {"event": "worker_start"})

    async def answer_inbound(self, selected, call_id):
        """来电监听确认后调用。线路同步占用；接听失败走统一收尾，不拒接、不盲目挂断。"""
        if self.closing:
            raise GatewayError("server_stopping", 503)
        async with self.locks.setdefault(selected, asyncio.Lock()):
            voice = self.device_voice(selected)
            item = self.new_item(selected, "inbound", "answering", voice)
            session = CallSession(self, item)
            self.line.acquire(session)
            try:
                if self.hub.lease:
                    raise GatewayError("line_busy", 409)
                if selected in self.recovering:
                    raise GatewayError("device_recovering", 409)
                if self.spawn == self.spawn_worker:
                    self.preflight()
                task = await asyncio.to_thread(self.store.inbound_task, self.inbound_prompt(selected),
                                               self.prompt, self.guard_prompt, voice)
                # 写任务文件期间服务可能开始关闭；此时会话尚未登记，关闭流程看不到它，所以不再接听。
                if self.closing:
                    raise GatewayError("server_stopping", 503)
            except BaseException:
                self.line.release(session)
                raise
            self.register(item, session, task)
            try:
                await session.answer(call_id, task)
                return self.public(item)
            except BaseException:
                item["error"] = item["error"] or "inbound_answer_failed"
                await session.finish("answer_failed")
                raise

    async def spawn_worker(self, item, task):
        session = self.sessions[item['id']]
        root = Path(__file__).parent / "ai"
        env = {**os.environ, "PHONE_MANAGED": "1", "PHONE_CONVERSATION_ID": item["id"],
               "PHONE_TEST_CALL_ID": item["callId"]}
        args = [sys.executable, str(root / "run-gpt-live.py")]
        if env.get("OPENAI_API_KEY") and env.get("OPENAI_LIVE_URL"):
            args = [env.get("PHONE_NODE") or "node", "--import", str(root / "node_modules/tsx/dist/loader.mjs"), str(root / "src/index.ts")]
        audio = WorkerAudio(session)
        text = WorkerTranscripts(self, item)
        try:
            await audio.claim()
            env['PHONE_AUDIO_FD'] = str(audio.child.fileno())
            env['PHONE_TRANSCRIPT_FD'] = str(text.child.fileno())
            process = await asyncio.create_subprocess_exec(*args, '--address', item['address'],
                '--call-test', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True, limit=65536,
                pass_fds=(audio.child.fileno(), text.child.fileno()))
            audio.child.close()
            text.child.close()
            session.text = text
            text.task = asyncio.create_task(text.run())
            session.audio = audio
            audio.task = asyncio.create_task(audio.run())
        except BaseException:
            await text.close()
            await audio.close()
            raise
        # Task text is IPC, not arguments, environment, source files or logs.
        try:
            process.stdin.write((json.dumps({"type": "init", "task": task}) + "\n").encode())
            await asyncio.wait_for(process.stdin.drain(), 5)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 18)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            await text.close()
            await audio.close()
            session.text = session.audio = None
            raise
        return process

    async def command(self, key, body):
        self.get(key)
        content = body.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 4096:
            raise GatewayError("invalid_command")
        session = self.sessions.get(key)
        if not session:
            raise GatewayError("conversation_not_active", 409)
        return await session.command(content)

    def publish(self, selected, direction, data):
        session = self.line.session
        if session and session.address == selected and session.item["state"] in ACTIVE:
            session.publish(direction, data)

    async def end(self, key):
        item = self.get(key)
        # Dial/create already owns this lock. Ending cannot race its late result.
        async with self.locks.setdefault(item["address"], asyncio.Lock()):
            return await self.end_locked(item)

    async def end_locked(self, item):
        session = self.sessions.get(item["id"])
        if session:
            if session.done.is_set():
                await session.retry()
            else:
                await session.finish("user_end")
        elif item["state"] == "cleanup_failed":
            recovery = self.recovering.get(item["address"])
            if recovery and recovery.item is item:
                await recovery.hangup()
        if item["state"] == "cleanup_failed":
            cleanup = item.get("cleanup", {})
            if cleanup.get("local") == "failed":
                raise GatewayError("audio_cleanup_failed" if item["error"] == "audio_cleanup_failed" else "local_cleanup_failed", 503)
            raise GatewayError("phone_hangup_failed", 503)
        return self.public(item)

    async def close(self):
        self.closing = True
        try:
            await self.inbound.close()
            session = self.line.session
            if session:
                with contextlib.suppress(GatewayError):
                    await self.end(session.id)
            for session in tuple(self.sessions.values()):
                if session.cleanup_task:
                    await asyncio.gather(session.cleanup_task, return_exceptions=True)
            await asyncio.gather(*(recovery.close() for recovery in tuple(self.recovering.values())), return_exceptions=True)
        finally:
            self.records.sync(ACTIVE)
            await self.records.close()
            await self.diagnostics.close()
