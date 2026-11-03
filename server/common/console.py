"""Shared UI APIs. No authentication; bind only to a trusted local network."""
import asyncio
import contextlib
from pathlib import Path

from aiohttp import web, WSMsgType

from .contracts import GatewayError, address
from .conversations import Conversations, ACTIVE
from .inbound import DEFAULT_INBOUND_PROMPT
from .pairing import Pairings
from .voices import VoicePreviews, validate_voice


def install(app, adapter, hub, store=None):
    runtime = Conversations(adapter, hub, store)
    pairings = Pairings(adapter)
    def changed():
        runtime.devices_changed()
        runtime.notify()
    pairings.on_change = changed
    # create_app owns adapter events; State.notify also forwards them to this runtime.
    hub.runtime = runtime
    app["conversations"], app["pairings"] = runtime, pairings
    sockets = set()
    previews = app["voice_previews"] = VoicePreviews()

    async def voices(request):
        return web.json_response(previews.catalog())

    async def preview(request):
        audio = await previews.get(request.match_info["voice"])
        return web.Response(body=audio, content_type="audio/wav", headers={"Cache-Control": "no-store"})

    async def device_voice(request):
        selected = address(request.match_info['address'])
        voice = None
        if request.method == 'PUT':
            body = await request.json()
            if not isinstance(body, dict):
                raise GatewayError('invalid_request')
            voice = validate_voice(body.get('voice'))
        # Reject arbitrary addresses before allocating a per-device lock or value.
        await runtime.known_device(selected)
        async with runtime.locks.setdefault(selected, asyncio.Lock()):
            await runtime.known_device(selected)
            if voice is not None:
                runtime.voice_preferences[selected] = voice
                runtime.notify()
            return web.json_response({'address': selected, 'voice': runtime.device_voice(selected),
                                      'scope': 'server_process'}, headers={'Cache-Control': 'no-store'})

    async def inbound_prompt(request):
        selected = address(request.match_info['address'])
        content = None
        if request.method == 'PUT':
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get('content'), str):
                raise GatewayError('invalid_inbound_prompt')
            content = body['content']
            if len(content.encode('utf-8')) > 16384:
                raise GatewayError('invalid_inbound_prompt')
        await runtime.known_device(selected)
        async with runtime.locks.setdefault(selected, asyncio.Lock()):
            await runtime.known_device(selected)
            if content is not None:
                await asyncio.to_thread(runtime.set_inbound_prompt, selected, content)
                runtime.notify()
            configured = runtime.inbound_prompt(selected)
            return web.json_response({'address': selected, 'content': configured if configured is not None else DEFAULT_INBOUND_PROMPT,
                                      'default': configured is None, 'guard': runtime.guard_prompt},
                                     headers={'Cache-Control': 'no-store'})

    async def snapshot():
        try:
            capabilities = await adapter.capabilities()
            devices = await adapter.devices()
            available = {phone["address"] for phone in capabilities.get("phones", [])}
            for device in devices:
                device["hfp"] = device.get("hfp", device["address"] in available)
                device['voice'] = runtime.device_voice(device['address'])
                device['recovering'] = device['address'] in runtime.recovering
                device['inboundPrompt'] = {'configured': device['address'] in runtime.inbound_prompts}
        except GatewayError:
            capabilities, devices = {"nativeTelephony": False}, []
        return {"devices": devices, "capabilities": capabilities,
                "line": runtime.line.snapshot(hub.audio_ready),
                "conversations": [runtime.public(item) for item in runtime.items.values()],
                "pairings": [pairings.get(key) for key in pairings.sessions]}

    async def state(request):
        return web.json_response(await snapshot())

    async def events(request):
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
        await ws.prepare(request)
        sockets.add(ws)
        queue = runtime.subscribe_updates()
        async def send():
            while True:
                await queue.get()
                await asyncio.wait_for(ws.send_json({"type": "console.state", **await snapshot()}), 2)
        async def receive():
            async for message in ws:
                if message.type == WSMsgType.ERROR:
                    break
        sender, receiver = asyncio.create_task(send()), asyncio.create_task(receive())
        try:
            await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        finally:
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            runtime.updates.discard(queue)
            sockets.discard(ws)
            await ws.close()
        return ws

    async def discover(request):
        return web.json_response({"devices": await adapter.discover()})

    async def pairing(request):
        key = request.match_info.get("key")
        if not key:
            body = await request.json()
            selected = address(body.get("address"))
            if runtime.busy(selected) or hub.lease:
                raise GatewayError("phone_busy", 409)
            return web.json_response(await pairings.start(selected), status=202)
        if request.method == "DELETE":
            await pairings.cancel(key)
        elif request.method == "POST":
            pairings.respond(key, await request.json())
        return web.json_response(pairings.get(key))

    async def device_control(request):
        selected = address(request.match_info["address"])
        async with runtime.locks.setdefault(selected, asyncio.Lock()):
            return await device_locked(selected, request.match_info["operation"])

    async def device_locked(selected, operation):
        if runtime.busy(selected) or hub.lease:
            raise GatewayError("phone_busy", 409)
        if any(s["address"] == selected and s["state"] in ("connecting", "waiting") for s in pairings.sessions.values()):
            raise GatewayError("pairing_busy", 409)
        if operation == "remove":
            await adapter.remove(selected)
            runtime.forget_device(selected)
        else:
            await adapter.connect(selected, operation == "connect")
        runtime.devices_changed()
        runtime.notify()
        return web.json_response({"ok": True})

    async def conversations(request):
        if request.method == "POST":
            body = await request.json()
            return web.json_response(await runtime.create(body), status=201)
        return web.json_response({"conversations": runtime.listing(request.query.get("direction"), request.query.get("address"))})

    async def detail(request):
        item = runtime.get(request.match_info["key"])
        return web.json_response({**runtime.public(item), "task": runtime.store.load("tasks", item["taskId"]), "commands": item["commands"]})

    async def call_state(request):
        item = runtime.get(request.match_info["key"])
        return web.json_response(runtime.call_status(item))

    async def call_events(request):
        item = runtime.get(request.match_info['key'])
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
        await ws.prepare(request)
        sockets.add(ws)
        tracker = runtime.tracker(item['id'])
        queue = tracker.subscribe() if tracker else None
        async def send():
            if queue is None:
                await ws.send_json({'type': 'call.state', **runtime.call_status(item)})
                return
            while True:
                value = await queue.get()
                await asyncio.wait_for(ws.send_json({'type': 'call.state', **value}), 2)
                if value['state'] == 'ended':
                    return
        async def receive():
            async for message in ws:
                if message.type == WSMsgType.ERROR:
                    break
        sender, receiver = asyncio.create_task(send()), asyncio.create_task(receive())
        try:
            await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        finally:
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            if tracker:
                tracker.listeners.discard(queue)
            sockets.discard(ws)
            await ws.close()
        return ws

    def transcript_cursor(request):
        try:
            after = int(request.query.get('after', '0'))
            if after < 0 or after > 2**53 - 1:
                raise ValueError()
        except ValueError:
            raise GatewayError('invalid_transcript_cursor') from None
        return after, request.query.get('cacheId')

    async def transcript_history(request):
        # 持久化的完整转写；进行中的会话返回已写入部分。实时增量用 /transcript/events。
        item = runtime.get(request.match_info['key'])
        events = await runtime.records.transcript(item['id'])
        return web.json_response({'type': 'transcript.record', 'conversationId': item['id'], 'direction': item.get('direction', 'outbound'),
                                  'state': item['state'], 'complete': item['state'] not in ACTIVE, 'events': events},
                                 headers={'Cache-Control': 'no-store'})

    async def transcript_events(request):
        item = runtime.get(request.match_info['key'])
        after, cache_id = transcript_cursor(request)
        ws = web.WebSocketResponse(heartbeat=15, compress=False, writer_limit=65536, max_msg_size=4096)
        await ws.prepare(request)
        sockets.add(ws)
        queue = asyncio.Queue(maxsize=1)
        buffer = runtime.transcripts.get(item)
        buffer.listeners.add(queue)
        queue.put_nowait(True)
        async def send():
            nonlocal after, cache_id, buffer
            while True:
                await queue.get()
                current = runtime.transcripts.buffers.get(item['id'], buffer)
                if current is not buffer:
                    buffer.listeners.discard(queue)
                    buffer = current
                    buffer.listeners.add(queue)
                value = buffer.snapshot(item['id'], item['state'], after, cache_id)
                await asyncio.wait_for(ws.send_json(value), 2)
                after, cache_id = value['revision'], value['cacheId']
                if item['state'] not in ACTIVE or item['id'] not in runtime.transcripts.buffers:
                    return
                await asyncio.sleep(.05)
        async def receive():
            async for message in ws:
                if message.type == WSMsgType.ERROR:
                    break
        sender, receiver = asyncio.create_task(send()), asyncio.create_task(receive())
        try:
            await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        finally:
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            buffer.listeners.discard(queue)
            sockets.discard(ws)
            await ws.close()
        return ws

    async def end(request):
        return web.json_response(await runtime.end(request.match_info["key"]))

    async def command(request):
        return web.json_response(await runtime.command(request.match_info["key"], await request.json()), status=202)

    async def monitor(request):
        key = request.match_info["key"]
        item = runtime.get(key)
        session = runtime.sessions.get(key)
        if item["state"] not in ACTIVE or session is None:
            raise GatewayError("conversation_not_active", 409)
        queue = asyncio.Queue(maxsize=5)
        ws = web.WebSocketResponse(heartbeat=15, compress=False, writer_limit=4800, max_msg_size=4096)
        await ws.prepare(request)
        sockets.add(ws)
        session.monitors.add(queue)
        async def send():
            while True:
                packet = await queue.get()
                if packet is None:
                    return
                start = asyncio.get_running_loop().time()
                try:
                    await asyncio.wait_for(ws.send_bytes(packet), 0.1)
                except Exception:
                    if session.audio:
                        session.audio.metrics.observe('monitor_failure')
                    raise
                if session.audio:
                    session.audio.metrics.observe('monitor_send', asyncio.get_running_loop().time() - start, len(packet))
        sender = asyncio.create_task(send())
        receiver = asyncio.create_task(ws.receive())
        try:
            await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        finally:
            sockets.discard(ws)
            session.monitors.discard(queue)
            for task in (sender, receiver):
                task.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            await ws.close()
        return ws

    async def prompt(request):
        if request.method == "PUT":
            content = (await request.json()).get("content")
            if not isinstance(content, str) or not content.strip() or len(content) > 16384:
                raise GatewayError("invalid_system_prompt")
            await asyncio.to_thread(runtime.store.save, "settings", "system-prompt", {"content": content})
            runtime.prompt = content
        return web.json_response({"content": runtime.prompt})

    async def cleanup(app):
        try:
            await previews.close()
            await pairings.close()
            await runtime.close()
        finally:
            await asyncio.gather(*(ws.close() for ws in tuple(sockets)))
            if hub.audio_ws:
                await hub.clear_lease()
    async def startup(app):
        runtime.start()
    app.on_startup.append(startup)
    app.on_shutdown.append(cleanup)
    app.router.add_get("/v1/voices", voices)
    app.router.add_post("/v1/voices/{voice}/preview", preview)
    app.router.add_get('/v1/devices/{address}/voice', device_voice)
    app.router.add_put('/v1/devices/{address}/voice', device_voice)
    app.router.add_get('/v1/devices/{address}/inbound-prompt', inbound_prompt)
    app.router.add_put('/v1/devices/{address}/inbound-prompt', inbound_prompt)
    app.router.add_get("/v1/console", state)
    app.router.add_get("/v1/console/events", events)
    app.router.add_post("/v1/discovery", discover)
    app.router.add_post("/v1/pairings", pairing)
    app.router.add_route("*", "/v1/pairings/{key}", pairing)
    app.router.add_post("/v1/management/{address}/{operation:connect|disconnect|remove}", device_control)
    app.router.add_get("/v1/conversations", conversations)
    app.router.add_post("/v1/conversations", conversations)
    app.router.add_get("/v1/conversations/{key}", detail)
    app.router.add_get("/v1/conversations/{key}/call-state", call_state)
    app.router.add_get("/v1/conversations/{key}/events", call_events)
    app.router.add_post("/v1/conversations/{key}/end", end)
    app.router.add_post("/v1/conversations/{key}/commands", command)
    app.router.add_get("/v1/conversations/{key}/monitor", monitor)
    app.router.add_get("/v1/conversations/{key}/transcript", transcript_history)
    app.router.add_get("/v1/conversations/{key}/transcript/events", transcript_events)
    app.router.add_get("/v1/system-prompt", prompt)
    app.router.add_put("/v1/system-prompt", prompt)

    # The UI is also served here for a same-origin, zero-configuration entry.
    client = Path(__file__).resolve().parents[2] / "client"
    async def index(request):
        return web.FileResponse(client / "index.html")
    app.router.add_get("/", index)
    app.router.add_static("/assets/", client / "assets")
