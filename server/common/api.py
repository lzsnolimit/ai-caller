"""Trusted-LAN control, event and PCM WebSocket API."""
import asyncio
import contextlib
import hmac
import re
import secrets
import os
import time

from aiohttp import web, WSMsgType

from .contracts import GatewayError, address
from .diagnostics import exception_fields

MAX_FRAME = 960  # 20 ms at 24 kHz mono PCM16
LEASE_SECONDS = 30


class State:
    """The one global controller lease and audio owner. There is one line."""
    def __init__(self, adapter, audio_factory):
        self.adapter, self.audio_factory = adapter, audio_factory
        self.lease = None
        self.expires = 0
        self.audio_ws = None
        self.audio_done = asyncio.Event()
        self.audio_done.set()
        self.audio_cleanup_failed = False
        self.audio_ready = False
        self.lock = asyncio.Lock()
        self.events = {}
        self.updates = set()
        self.lease_changed = asyncio.Event()
        self.runtime = None

    def require_lease(self, request):
        candidate = request.headers.get("X-Controller-Lease", "")
        if not self.lease or time.monotonic() >= self.expires or not hmac.compare_digest(candidate, self.lease):
            raise GatewayError("controller_lease_required", 409)

    async def clear_lease(self):
        if self.audio_ws:
            await self.audio_ws.close(code=1000, message=b"controller lease ended")
            await self.audio_done.wait()
        if self.audio_cleanup_failed:
            raise GatewayError("audio_cleanup_failed", 503)
        self.lease = None
        self.expires = 0

    def notify(self):
        for queue in tuple(self.updates):
            if not queue.full():
                queue.put_nowait(True)
        if self.runtime:
            self.runtime.devices_changed()
            self.runtime.notify()

    def publish(self, selected, direction, data):
        if self.runtime:
            self.runtime.publish(selected, direction, data)


def create_app(adapter, audio_factory=None, *, managed=False, store=None):
    if audio_factory is None:
        from server.pi.audio import PipeWireAudio
        audio_factory = PipeWireAudio
    hub = State(adapter, audio_factory)
    adapter.on_change = hub.notify

    @web.middleware
    async def errors(request, handler):
        started = time.monotonic()
        def record(error):
            if hub.runtime:
                fields = exception_fields(error, 'api_request')
                fields.update(clockDomain='python_monotonic', durationMs=round((time.monotonic()-started)*1000))
                hub.runtime.diagnostics.write(secrets.token_hex(16), fields)
        origin = request.headers.get("Origin")
        allowed = {f"{request.scheme}://{request.host}", "http://127.0.0.1:5173", "http://localhost:5173"}
        allowed.update(os.environ.get("PHONE_CLIENT_ORIGINS", "").split(","))
        if origin and origin not in allowed:
            return web.json_response({"error": {"code": "origin_not_allowed"}}, status=403)
        try:
            response = web.Response(status=204) if request.method == "OPTIONS" else await handler(request)
        except GatewayError as error:
            if error.status >= 500:
                record(error)
            response = web.json_response({"error": {"code": error.code}}, status=error.status)
        except (ValueError, TypeError, KeyError):
            response = web.json_response({"error": {"code": "invalid_request"}}, status=400)
        except Exception as error:
            record(error)
            # D-Bus failures can include numbers. Never return exception text.
            response = web.json_response({"error": {"code": "internal_error"}}, status=500)
        if origin:
            response.headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type"})
        response.headers["Cache-Control"] = "no-store"
        return response

    app = web.Application(middlewares=[errors], client_max_size=2 * 1024 * 1024)
    app["state"] = hub

    async def capabilities(request):
        return web.json_response(await adapter.capabilities())

    async def devices(request):
        return web.json_response({"devices": await adapter.devices()})

    async def calls(request):
        return web.json_response({"calls": await adapter.calls(address(request.match_info["address"]))})

    async def lease(request):
        state = hub
        async with hub.lock:
            if state.lease and time.monotonic() >= state.expires:
                await state.clear_lease()
            if request.method == "POST":
                if state.lease:
                    raise GatewayError("controller_busy", 409)
                if hub.runtime and not hub.runtime.line.idle():
                    raise GatewayError("controller_busy", 409)
                state.lease = secrets.token_urlsafe(32)
            elif request.method in ("PUT", "DELETE"):
                state.require_lease(request)
                if request.method == "DELETE":
                    await state.clear_lease()
                    hub.lease_changed.set()
                    return web.json_response({"ok": True})
            else:
                raise GatewayError("method_not_allowed", 405)
            state.expires = time.monotonic() + LEASE_SECONDS
            hub.lease_changed.set()
            return web.json_response({"lease": state.lease, "expiresIn": LEASE_SECONDS})

    async def control(request):
        state = hub
        async with state.lock:
            state.require_lease(request)
            addr = address(request.match_info["address"])
            operation = request.match_info["operation"]
            if operation in ("connect", "disconnect"):
                await adapter.connect(addr, operation == "connect")
                result = {"ok": True}
            elif operation in ("answer", "hangup"):
                result = await adapter.operate(addr, operation, call_id=request.match_info["call_id"])
            else:
                body = await request.json()
                if not isinstance(body, dict):
                    raise GatewayError("invalid_request")
                value = body.get("number" if operation == "dial" else "tones")
                pattern = r"\+?[0-9]{1,20}" if operation == "dial" else r"[0-9A-Da-d*#]{1,64}"
                if not isinstance(value, str) or not re.fullmatch(pattern, value):
                    raise GatewayError("invalid_number" if operation == "dial" else "invalid_tones")
                if operation == "dial" and hub.runtime:
                    raise GatewayError("use_task_conversation_api", 409)
                result = await adapter.operate(addr, operation, value)
            hub.notify()
            return web.json_response(result)

    async def audio(request):
        state = hub
        addr = address(request.query.get("address"))
        async with hub.lock:
            state.require_lease(request)
            if state.audio_ws:
                raise GatewayError("audio_busy", 409)
            await adapter.device_path(addr)
            state.require_lease(request)
            # aiohttp rejects an uncompressed message at >= max_msg_size.
            ws = web.WebSocketResponse(max_msg_size=MAX_FRAME + 1, heartbeat=15, writer_limit=9600, compress=False)
            await ws.prepare(request)
            state.audio_ws = ws
            state.audio_done.clear()
        bridge = None
        tasks = []
        try:
            bridge = audio_factory(addr)
            # Watch disconnects while SCO is pending. No startup audio is accepted.
            startup = asyncio.create_task(bridge.start())
            inbound = asyncio.create_task(ws.receive())
            tasks.extend([startup, inbound])
            done, _ = await asyncio.wait(tasks, timeout=45, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise GatewayError("audio_ready_timeout", 503)
            if inbound in done:
                message = inbound.result()
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    return ws
                raise GatewayError("audio_not_ready", 409)
            await startup
            state.audio_ready = True
            if hub.runtime:
                hub.runtime.notify()
            await ws.send_json({"type": "audio.ready"})

            pcm_queue = asyncio.Queue(maxsize=10)  # At most 200 ms; no stale voice queue.
            capture_queue = asyncio.Queue(maxsize=10)

            async def receive():
                pending = inbound
                while True:
                    message = await pending
                    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        return
                    if message.type != WSMsgType.BINARY or not message.data or len(message.data) % 2 or len(message.data) > MAX_FRAME:
                        raise GatewayError("invalid_pcm_frame")
                    try:
                        await asyncio.wait_for(pcm_queue.put(message.data), 0.2)
                    except asyncio.TimeoutError:
                        raise GatewayError("audio_backpressure", 409) from None
                    pending = ws.receive()

            async def playback():
                while True:
                    data = await pcm_queue.get()
                    await bridge.write(data)
                    hub.publish(addr, 1, data)

            async def capture():
                while True:
                    data = await bridge.read()
                    if not data or len(data) % 2 or len(data) > MAX_FRAME:
                        raise GatewayError("invalid_capture_frame", 503)
                    hub.publish(addr, 0, data)
                    try:
                        await asyncio.wait_for(capture_queue.put(data), 0.2)
                    except asyncio.TimeoutError:
                        raise GatewayError("audio_backpressure", 409) from None

            async def transmit():
                while True:
                    data = await capture_queue.get()
                    try:
                        await asyncio.wait_for(ws.send_bytes(data), 0.2)
                    except asyncio.TimeoutError:
                        raise GatewayError("audio_backpressure", 409) from None

            running = [asyncio.create_task(receive()), asyncio.create_task(playback()), asyncio.create_task(capture()), asyncio.create_task(transmit()), asyncio.create_task(bridge.monitor())]
            tasks.extend(running)
            finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                await task
        except GatewayError as error:
            if not ws.closed:
                with contextlib.suppress(ConnectionError):
                    await ws.send_json({"type": "audio.closed"} if error.code == "sco_closed" else {"type": "error", "code": error.code})
        except asyncio.CancelledError:
            raise
        except Exception:
            if not ws.closed:
                with contextlib.suppress(ConnectionError):
                    await ws.send_json({"type": "error", "code": "audio_failed"})
        finally:
            state.audio_ready = False
            if hub.runtime:
                hub.runtime.notify()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if bridge is not None:
                    await bridge.close()
            except Exception:
                state.audio_cleanup_failed = True
                if not ws.closed:
                    with contextlib.suppress(ConnectionError):
                        await ws.send_json({"type": "error", "code": "audio_cleanup_failed"})
            if not ws.closed:
                with contextlib.suppress(ConnectionError):
                    await ws.send_json({"type": "audio.closed"})
            await ws.close()
            if not state.audio_cleanup_failed:
                state.audio_ws = None
            state.audio_done.set()
        return ws

    async def events(request):
        state = hub
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
        await ws.prepare(request)
        state.events[ws] = None
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(True)
        hub.updates.add(queue)
        subscriptions = {}
        def changed():
            if not queue.full():
                queue.put_nowait(True)
        async def send():
            while True:
                await queue.get()
                current = {'devices': await adapter.devices(), 'phones': []}
                live = {d['address'] for d in current['devices'] if d['connected']}
                for selected in tuple(subscriptions):
                    if selected not in live:
                        subscriptions.pop(selected)()
                for selected in live:
                    subscribe = getattr(adapter, 'subscribe_calls', None)
                    if subscribe and selected not in subscriptions:
                        subscriptions[selected] = subscribe(selected, changed)
                    try:
                        calls = await adapter.calls(selected)
                    except Exception:
                        calls = None
                    current['phones'].append({'address': selected, 'calls': calls})
                await asyncio.wait_for(ws.send_json({'type': 'phone.state', **current}), 2)
        async def receive():
            async for _ in ws:
                pass
        sender, receiver = asyncio.create_task(send()), asyncio.create_task(receive())
        try:
            await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        finally:
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            for unsubscribe in subscriptions.values():
                unsubscribe()
            hub.updates.discard(queue)
            state.events.pop(ws, None)
            await ws.close()
        return ws

    async def background():
        while True:
            hub.lease_changed.clear()
            try:
                if hub.lease:
                    await asyncio.wait_for(hub.lease_changed.wait(), max(0.01, hub.expires-time.monotonic()))
                    continue
                await hub.lease_changed.wait()
                continue
            except asyncio.TimeoutError:
                pass
            async with hub.lock:
                if hub.lease and time.monotonic() >= hub.expires:
                    try:
                        await hub.clear_lease()
                    except GatewayError:
                        # Do not spin if resource cleanup cannot finish.
                        hub.expires = time.monotonic() + 5

    async def lifecycle(app):
        await adapter.start()
        task = asyncio.create_task(background())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        try:
            if hub.runtime:
                await hub.runtime.close()
            if hub.audio_ws:
                await hub.clear_lease()
        finally:
            await asyncio.gather(*(ws.close() for ws in tuple(hub.events)))
            await adapter.close()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/v1/capabilities", capabilities)
    app.router.add_get("/v1/devices", devices)
    app.router.add_get("/v1/devices/{address}/calls", calls)
    app.router.add_route("*", "/v1/lease", lease)
    app.router.add_post("/v1/devices/{address}/{operation:connect|disconnect}", control)
    # Supply the fixed operation through a wrapper rather than caller input.
    async def dial(request):
        request.match_info["operation"] = "dial"
        return await control(request)
    app.router.add_post("/v1/devices/{address}/calls", dial)
    app.router.add_post("/v1/devices/{address}/calls/{call_id}/{operation:answer|hangup}", control)
    async def tones(request):
        request.match_info["operation"] = "tones"
        return await control(request)
    app.router.add_post("/v1/devices/{address}/tones", tones)
    app.router.add_get("/v1/audio", audio)
    app.router.add_get("/v1/events", events)
    if managed:
        from .console import install
        install(app, adapter, hub, store)
    return app
