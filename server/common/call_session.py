"""One line, one CallSession per dial, one idempotent cleanup with scoped recovery."""
import asyncio
import contextlib
import json
import os
import secrets
import signal
import time

from .contracts import GatewayError
from .call_state import CallState
from .diagnostics import exception_fields, sanitize

ACTIVE = {"dialing", "answering", "starting", "active", "ending", "cleanup_failed"}
COMMAND_TIMEOUT = 12
ANSWER_SECONDS = 8
HANGUP_VERIFY_SECONDS = 6
WORKER_EXIT_SECONDS = 18
COMMAND_REASONS = {'command_limit', 'not_ready', 'command_too_large', 'model_write_failed', 'provider_rejected',
                   'ack_timeout', 'conversation_closed', 'worker_write_failed', 'worker_timeout'}


class Line:
    """The only call line. idle -> busy is synchronous, so two dials cannot both win."""
    def __init__(self):
        self.state, self.session, self.reason = 'idle', None, None

    def idle(self):
        return self.state == 'idle'

    def acquire(self, session):
        if self.state == 'recovering':
            raise GatewayError('line_recovering', 503)
        if self.state == 'busy':
            raise GatewayError('line_busy', 409)
        self.state, self.session, self.reason = 'busy', session, None

    def release(self, session, reason=None):
        if self.session is not session:
            return
        if reason:
            # Local resources are still held. Keep the owner of the cleanup handles.
            self.state, self.reason = 'recovering', reason
        else:
            self.state, self.session, self.reason = 'idle', None, None

    def snapshot(self, audio_ready=False):
        session = self.session
        return {'state': self.state, 'conversationId': session.id if session else None,
                'address': session.address if session else None, 'reason': self.reason,
                'audioReady': bool(audio_ready)}


async def our_call_gone(adapter, selected, call_id, identity, seconds):
    """True once the phone neither reports our identity nor a call with our ID. Raises when a query fails.

    The identity is opaque: the platform defines it, the common layer only compares it.
    `hangup_owned` False or an identity change can also mean "this connection no longer owns
    the call" (Mac reconnect), so the call list is checked as well.
    """
    current = await asyncio.wait_for(adapter.call_identity(selected, call_id), seconds)
    if current == identity:
        return False
    calls = await asyncio.wait_for(adapter.calls(selected), seconds)
    return not any(call.get('id') == call_id for call in calls)


async def phone_call_ended(adapter, selected, call_id, identity, seconds):
    """Bounded wait until our call is gone. Raises when the query fails."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        if await our_call_gone(adapter, selected, call_id, identity, seconds):
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.1)


class DeviceRecovery:
    """One phone whose call state is unknown. Only verifies; never redials or hangs up blindly."""
    def __init__(self, runtime, item):
        self.runtime, self.item = runtime, item
        self.address, self.call_id, self.identity = item['address'], item['callId'], item.get('identity')
        self.unsubscribe = None
        self.task = None
        self.dirty = asyncio.Event()
        self.resolved = False

    def start(self):
        self.subscribe()
        self.task = asyncio.create_task(self.run())

    def subscribe(self):
        """Retried on every check: the platform can be disconnected when the isolation starts."""
        if self.unsubscribe or self.resolved:
            return
        subscribe = getattr(self.runtime.adapter, 'subscribe_calls', None)
        if subscribe:
            with contextlib.suppress(Exception):
                self.unsubscribe = subscribe(self.address, self.dirty.set)

    async def run(self):
        while not self.resolved:
            await self.dirty.wait()
            self.dirty.clear()
            await self.check()
            await asyncio.sleep(0.05)

    async def check(self):
        if self.resolved:
            return True
        self.subscribe()
        adapter = self.runtime.adapter
        try:
            # Isolation lifts only when the phone reports no call at all, not merely a new identity.
            current = await asyncio.wait_for(adapter.call_identity(self.address, self.call_id), HANGUP_VERIFY_SECONDS)
            if (self.identity is not None and current == self.identity) or await asyncio.wait_for(adapter.calls(self.address), HANGUP_VERIFY_SECONDS):
                return False
        except Exception:
            return False
        self.resolve()
        return True

    async def hangup(self):
        """Explicit retry for this identity only. Returns True when the call is confirmed ended."""
        if self.resolved:
            return True
        if self.identity is None:
            # 没有本次身份（例如未确认接通的来电）：只核对，不发任何挂断。
            return await self.check()
        try:
            sent = await self.runtime.adapter.hangup_owned(self.address, self.call_id, self.identity)
            if sent and not await phone_call_ended(self.runtime.adapter, self.address, self.call_id, self.identity, HANGUP_VERIFY_SECONDS):
                return False
        except Exception as error:
            self.runtime.diagnostics.write(self.item['id'], exception_fields(error, 'phone_recovery'))
            return False
        self.resolve()
        return True

    def resolve(self):
        self.resolved = True
        item = self.item
        item['cleanupPending'] = False
        item.setdefault('cleanup', {})['phone'] = 'ended'
        status = item.get('callStatus') or {'revision': 0}
        item['callStatus'] = {'state': 'ended', 'lastKnownState': 'ended', 'revision': status.get('revision', 0) + 1}
        if item['state'] == 'cleanup_failed' and item.get('cleanup', {}).get('local') == 'released':
            item['state'] = 'failed' if item['error'] else 'ended'
        if self.runtime.recovering.get(self.address) is self:
            self.runtime.recovering.pop(self.address)
        if self.unsubscribe:
            self.unsubscribe()
            self.unsubscribe = None
        self.runtime.diagnostics.write(item['id'], {'event': 'phone_recovered'})
        self.runtime.notify()

    def abandon(self):
        """Stop verifying without claiming the phone ended. The item leaves the active set as failed."""
        self.resolved = True
        item = self.item
        if item['state'] == 'cleanup_failed':
            item['state'] = 'failed'
        if self.runtime.recovering.get(self.address) is self:
            self.runtime.recovering.pop(self.address)
        self.runtime.diagnostics.write(item['id'], {'event': 'phone_recovery_abandoned'})
        asyncio.create_task(self.close())

    async def close(self):
        if self.unsubscribe:
            self.unsubscribe()
            self.unsubscribe = None
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class CallSession:
    """Every resource of one dial. Destroyed after cleanup; the item dict is the history projection."""
    def __init__(self, runtime, item):
        self.runtime, self.item = runtime, item
        self.id, self.address = item['id'], item['address']
        self.adapter, self.hub, self.diagnostics = runtime.adapter, runtime.hub, runtime.diagnostics
        self.process = self.watcher = self.tracker = self.audio = self.text = None
        self.monitors = set()
        self.command_timers = {}
        self.finishing = False
        self.done = asyncio.Event()
        self.cleanup_task = None

    def notify(self):
        self.runtime.notify()

    def accepting(self):
        return not self.finishing and self.item['state'] in ('starting', 'active')

    async def start(self, number, task):
        item = self.item
        result = await self.adapter.operate(self.address, "dial", number)
        call_id = result.get("id")
        if not call_id:
            calls = await self.adapter.calls(self.address)
            candidates = [c for c in calls if c["state"] in ("dialing", "alerting", "active")]
            if len(candidates) != 1:
                raise GatewayError("dial_call_not_identified", 409)
            call_id = candidates[0]["id"]
        item["callId"] = call_id
        item["identity"] = await self.adapter.call_identity(self.address, call_id)
        if item["identity"] is None:
            raise GatewayError("dial_call_not_identified", 409)
        await self.attach(task)

    async def answer(self, call_id, task):
        """来电：接听结果以手机通话状态为准（adapter 已核对）。失败时不盲目挂断仍在响铃的来电。"""
        item = self.item
        item["callId"] = call_id
        try:
            await asyncio.wait_for(self.adapter.operate(self.address, "answer", call_id=call_id), ANSWER_SECONDS)
        except Exception as error:
            # 命令失败不等于没接通。收尾时 check_phone 以手机状态为准，绝不挂断仍在响铃的来电。
            self.diagnostics.write(self.id, exception_fields(error, 'inbound_answer'))
            raise
        item["identity"] = await self.adapter.call_identity(self.address, call_id)
        if item["identity"] is None:
            raise GatewayError("answer_call_not_identified", 409)
        item["answeredAt"] = time.time()
        await self.attach(task)

    async def attach(self, task):
        """拨号或接听之后的共同部分：状态跟踪、worker 与 watcher。"""
        item = self.item
        item["state"] = "starting"
        def call_changed():
            self.diagnostics.write(item['id'], {'event': 'call_state', **self.tracker.snapshot()})
            self.notify()
        self.tracker = CallState(self.adapter, item, call_changed)
        self.tracker.start()
        self.notify()
        self.process = await self.runtime.spawn(item, task)
        self.watcher = asyncio.create_task(self.watch())

    async def watch(self):
        item, process = self.item, self.process
        try:
            while raw := await process.stdout.readline():
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                diagnostic = sanitize(event.get("diagnostic"))
                if diagnostic:
                    self.diagnostics.write(item["id"], diagnostic)
                    if diagnostic["event"] in ("model_session_ready", "audio_stats", "session_end"):
                        item.setdefault("diagnostics", {}).update(diagnostic)
                if event.get("sessionStarted") and not self.finishing:
                    item["ai"] = "ready"
                if event.get("callAnswered") and not self.finishing:
                    item["state"] = "active"
                    item["answeredAt"] = item.get("answeredAt") or time.time()
                if "bridgeError" in event and not item['error']:
                    item["error"] = "ai_audio_failed"
                if event.get("sessionClosed"):
                    item["inputBytes"] = event.get("inputBytes", 0)
                    item["outputBytes"] = event.get("outputBytes", 0)
                if event.get("commandId"):
                    for command in item["commands"]:
                        if command["id"] == event["commandId"]:
                            self.command_update(command, event.get('commandStatus'), event.get('commandReason'), event.get('commandElapsedMs'))
                self.notify()
            code = await process.wait()
            self.diagnostics.write(item["id"], {"event": "worker_exit", "exitCode": code})
            # 收尾时由我们停止 worker，或手机已报告通话结束后 worker 才退出：非零退出码是结束的结果，不算故障。
            ended = self.tracker is not None and self.tracker.state == 'ended'
            if code and not item["error"] and not self.finishing and not ended:
                item["error"] = "ai_worker_failed"
        except asyncio.CancelledError:
            raise
        except Exception:
            item["error"] = "ai_worker_failed"
        finally:
            if not self.finishing:
                remote = self.tracker is not None and self.tracker.state == 'ended'
                reason = 'remote_end' if remote else ('ai_failed' if item['error'] else 'worker_exit')
                self.cleanup_task = asyncio.create_task(self.finish(reason))

    # ---- commands ----
    def command_update(self, command, status, reason=None, worker_elapsed=None):
        item = self.item
        if not isinstance(status, str) or status not in {'accepted', 'written', 'injected', 'rejected', 'unknown'} or command['status'] not in {'accepted', 'written'}:
            return
        if status == command['status'] and status != 'accepted':
            return
        timer = self.command_timers.get(command['id'])
        command.update(status=status, updatedAtMs=round(time.time() * 1000))
        if isinstance(reason, str) and reason in COMMAND_REASONS:
            command['reason'] = reason
        if type(worker_elapsed) in (int, float) and 0 <= worker_elapsed <= 1e9:
            command['workerElapsedMs'] = worker_elapsed
        if timer:
            command['elapsedMs'] = round((time.monotonic() - timer[1]) * 1000)
            if status in {'injected', 'rejected', 'unknown'}:
                timer[0].cancel()
                self.command_timers.pop(command['id'], None)
        self.diagnostics.write(item['id'], {'event': 'operator_command', 'commandId': command['id'],
            'clientEventId': 'operator_' + command['id'], 'commandStatus': status,
            'commandReason': command.get('reason'), 'durationMs': command.get('elapsedMs', 0),
            'workerElapsedMs': command.get('workerElapsedMs'), 'clockDomain': 'python_monotonic'})
        item['commandRevision'] = item.get('commandRevision', 0) + 1
        self.notify()

    def close_commands(self):
        for command in self.item['commands']:
            self.command_update(command, 'unknown', 'conversation_closed')
        for handle, _ in self.command_timers.values():
            handle.cancel()
        self.command_timers.clear()

    async def command(self, content):
        item, process = self.item, self.process
        if not self.accepting() or item["ai"] != "ready" or not process or process.returncode is not None:
            raise GatewayError("conversation_not_active", 409)
        if await self.adapter.call_identity(self.address, item["callId"]) != item.get("identity"):
            raise GatewayError("conversation_not_active", 409)
        if not self.accepting() or process.returncode is not None:
            raise GatewayError('conversation_not_active', 409)
        if len(item['commands']) >= 128 or sum(c['status'] in {'accepted', 'written'} for c in item['commands']) >= 32:
            raise GatewayError('command_limit', 429)
        command = {"id": secrets.token_hex(16), "content": content, "status": "accepted", 'acceptedAtMs': round(time.time() * 1000)}
        item['commands'].append(command)  # Register before any worker receipt can arrive.
        handle = asyncio.get_running_loop().call_later(COMMAND_TIMEOUT, self.command_update, command, 'unknown', 'worker_timeout')
        self.command_timers[command['id']] = (handle, time.monotonic())
        self.command_update(command, 'accepted')
        try:
            process.stdin.write((json.dumps({"type": "command", "conversationId": self.id, **command}) + "\n").encode())
            await asyncio.wait_for(process.stdin.drain(), 1)
        except (OSError, asyncio.TimeoutError):
            self.command_update(command, 'unknown', 'worker_write_failed')
        return command

    # ---- monitor copies ----
    def publish(self, direction, data):
        item = self.item
        packet = bytes([direction]) + data
        item["inputBytes" if direction == 0 else "outputBytes"] += len(data)
        for queue in tuple(self.monitors):
            if queue.full():
                queue.get_nowait()  # Drop the monitor copy only. Never wait in SCO.
                if self.audio:
                    self.audio.metrics.observe("monitor_drop")
            queue.put_nowait(packet)
            if self.audio:
                self.audio.metrics.observe("monitor_enqueue", size=len(packet), depth=queue.qsize())

    # ---- unified cleanup ----
    async def finish(self, reason):
        """Idempotent. Every end reason: remote end, user end, start failure, AI/audio error, cancel, shutdown."""
        if self.finishing:
            await self.done.wait()
            return
        self.finishing = True
        item = self.item
        item['endReason'] = reason
        item['state'] = 'ending'
        item['cleanup'] = {'local': 'pending', 'phone': 'pending'}
        self.close_commands()
        self.diagnostics.write(self.id, {'event': 'session_finish', 'reason': reason})
        self.notify()
        local_ok = phone_ok = False
        try:
            if self.process and self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
            local, phone = await asyncio.gather(self.release_local(), self.check_phone(), return_exceptions=True)
            local_ok, phone_ok = local is True, phone is True
            for stage, result in (('local_release', local), ('phone_check', phone)):
                if isinstance(result, BaseException):
                    self.diagnostics.write(self.id, exception_fields(result, stage))
        finally:
            await self.settle(local_ok, phone_ok)
            self.done.set()

    async def retry(self):
        """Explicit second attempt for a session whose local resources were not released."""
        await self.done.wait()
        if self.item['cleanup'].get('local') == 'released':
            return
        try:
            local = await self.release_local(retry=True)
        except Exception as error:
            self.diagnostics.write(self.id, exception_fields(error, 'local_release'))
            local = False
        await self.settle(local, self.item['cleanup'].get('phone') == 'ended')

    async def release_local(self, retry=False):
        ok = True
        item = self.item
        if self.watcher and not self.watcher.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.watcher), WORKER_EXIT_SECONDS)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(asyncio.shield(self.watcher), 5)
                except asyncio.TimeoutError:
                    ok = False
                    item['error'] = item['error'] or 'worker_exit_failed'
        if self.text:
            try:
                await self.text.close()
            except Exception as error:
                self.diagnostics.write(self.id, exception_fields(error, 'transcript_cleanup'))
        if self.audio:
            await self.audio.close(retry=retry)
            if self.hub.audio_cleanup_failed and self.hub.audio_ws is self.audio:
                ok = False
                item['error'] = item['error'] or 'audio_cleanup_failed'
        for queue in tuple(self.monitors):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(None)
        self.monitors.clear()
        for handle, _ in self.command_timers.values():
            handle.cancel()
        self.command_timers.clear()
        return ok

    async def check_phone(self):
        item = self.item
        call_id, identity = item.get('callId'), item.get('identity')
        if call_id and item.get('direction') == 'inbound' and not item.get('answeredAt'):
            # 未确认接通的来电：仍在响铃 → 由手机处理，不拒接；已消失 → 结束；已接通 → 按身份挂断；
            # 查询失败 → 状态未知，隔离该设备，但不对它发任何挂断。
            try:
                calls = await asyncio.wait_for(self.adapter.calls(self.address), HANGUP_VERIFY_SECONDS)
            except Exception as error:
                self.diagnostics.write(self.id, exception_fields(error, 'phone_verify'))
                return False
            state = next((c.get('state') for c in calls if c.get('id') == call_id), None)
            if state != 'active':
                return True
            if identity is None:
                with contextlib.suppress(Exception):
                    identity = item['identity'] = await asyncio.wait_for(self.adapter.call_identity(self.address, call_id), HANGUP_VERIFY_SECONDS)
            if identity is None:
                return False
        if not call_id or identity is None:
            # Nothing identified as ours was dialed. A hangup without identity would be blind.
            return True
        try:
            sent = await self.adapter.hangup_owned(self.address, call_id, identity)
        except Exception as error:
            self.diagnostics.write(self.id, exception_fields(error, 'phone_hangup'))
            return False
        try:
            # `sent` False: not ours any more (ended, or this connection no longer owns it). Never touch a
            # newer call; only confirm, within the same bound, that no call with our ID remains. The
            # phone can list the row for a moment after the remote end.
            return await phone_call_ended(self.adapter, self.address, call_id, identity, HANGUP_VERIFY_SECONDS)
        except Exception as error:
            self.diagnostics.write(self.id, exception_fields(error, 'phone_verify'))
            return False

    async def settle(self, local_ok, phone_ok):
        item, runtime = self.item, self.runtime
        item['cleanup'] = {'local': 'released' if local_ok else 'failed', 'phone': 'ended' if phone_ok else 'unknown'}
        item['ai'] = 'closed'
        item['endedAt'] = item.get('endedAt') or time.time()
        if phone_ok:
            item['cleanupPending'] = False
        else:
            item['cleanupPending'] = True
            item['error'] = item['error'] or 'phone_hangup_failed'
        if self.tracker:
            await self.tracker.close('ended' if phone_ok else 'unknown')
            item['callStatus'] = self.tracker.snapshot()
        if local_ok and phone_ok:
            item['state'] = 'failed' if item['error'] else 'ended'
        else:
            item['state'] = 'cleanup_failed'
        if local_ok:
            # Destroy the run objects. The item dict stays as history.
            runtime.sessions.pop(self.id, None)
            runtime.line.release(self)
            self.process = self.watcher = self.audio = self.text = self.tracker = None
        else:
            runtime.line.release(self, item['error'] or 'local_release_failed')
        if not phone_ok and item.get('callId'):
            runtime.isolate(item)
        self.diagnostics.write(self.id, {'event': 'session_settled', 'state': item['state'], **item['cleanup']})
        self.notify()
