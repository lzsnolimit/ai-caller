"""Real HTTP contracts over substitute phones/workers; no Bluetooth operations."""
import asyncio
import tempfile
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch
import secrets

from aiohttp.test_utils import TestClient, TestServer
from server.common.api import create_app
from server.common.contracts import GatewayError
from server.common.storage import Store
from server.common.pairing import Pairings

PHONE = "AA:BB:CC:DD:EE:FF"
OTHER = "11:22:33:44:55:66"
TASK = {"purpose": "Audio test", "outcome": "Confirm both voices", "content": "Identify yourself as AI and ask if the caller hears you.", "source": "editor"}
REMOVED_VOICES = ('nuri', 'juni', 'flitz', 'brise', 'sillage', 'nira', 'harema', 'shida', 'noeul')


class Process:
    pid = 99999999
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.returncode = None
        self.commands = []
        self.done = asyncio.Event()
    def write(self, data):
        command = json.loads(data)
        self.commands.append(command)
        self.stdout.feed_data((json.dumps({"commandId": command["id"], "commandStatus": "written"})+"\n").encode())
    async def drain(self): pass
    async def wait(self):
        await self.done.wait()
        return self.returncode
    def finish(self, code=0):
        self.returncode = code
        self.stdout.feed_eof()
        self.done.set()


class Phones:
    def __init__(self):
        self.current = {PHONE: [], OTHER: []}
        self.dials = []
        self.answers = []
        self.processes = {}
        self.pairings = None
        self.connected = {PHONE, OTHER}
        self.listeners = {PHONE: set(), OTHER: set()}
        self.names = {}
        self.caller_ids = {}
    def subscribe_calls(self, selected, callback):
        self.listeners[selected].add(callback)
        return lambda: self.listeners[selected].discard(callback)
    def changed(self, selected):
        for callback in tuple(self.listeners[selected]): callback()
    async def start(self): pass
    async def close(self): pass
    async def device_path(self, selected):
        if selected not in self.current:
            raise GatewayError("device_not_found", 404)
        return selected
    async def devices(self):
        return [{"address": a, "paired": True, "connected": a in self.connected, "name": self.names.get(a)} for a in self.current]
    async def capabilities(self):
        return {"phones": [{"address": a, "methods": ["Dial"]} for a in self.connected]}
    async def calls(self, selected):
        await self.device_path(selected)
        return self.current[selected]
    async def peer_number(self, selected, call_id):
        return self.caller_ids.get((selected, call_id))
    async def call_identity(self, selected, call_id):
        return (selected,call_id,self.dials.count(selected)) if self.current[selected] and not self.current[selected][0].get("newCall") else None
    async def hangup_owned(self, selected, call_id, identity):
        if not self.current[selected] or self.current[selected][0].get("newCall"):
            return False
        await self.operate(selected,"hangup",call_id=call_id)
        return True
    async def operate(self, selected, operation, value=None, call_id=None):
        if operation == "dial":
            self.dials.append(selected)
            self.current[selected] = [{"id": "call1", "state": "active"}]
            return {"id": "call1"}
        if operation == "answer":
            # 接听以手机通话状态为准：来电变为 active 才算成功。
            self.answers.append((selected, call_id))
            for call in self.current[selected]:
                if call["id"] == call_id and call["state"] == "incoming":
                    call["state"] = "active"
                    self.changed(selected)
                    return {"ok": True}
            raise GatewayError("call_not_found", 409)
        self.current[selected] = []
        if selected in self.processes:
            self.processes[selected].finish()
        return {"ok": True}
    async def connect(self, selected, connected):
        if connected: self.connected.add(selected)
        else: self.connected.discard(selected)
    async def remove(self, selected):
        self.current.pop(selected)
    async def discover(self): return await self.devices()
    async def pair(self, selected, key):
        if not await self.pairings.ask(key, selected, "confirmation", "123456"):
            raise GatewayError("pairing_rejected", 409)


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(tempfile.gettempdir()) / "ai-caller/console-tests"/secrets.token_hex(8)
        self.store = Store(self.root)
        self.phones = Phones()
        self.env = patch.dict(os.environ, {"PHONE_VOICE_SAMPLE_DIR": str(self.root / 'samples')})
        self.env.start()
        self.app = create_app(self.phones, managed=True, store=self.store)
        self.runtime = self.app["conversations"]
        async def spawn(item, task):
            process = Process()
            self.phones.processes[item["address"]] = process
            process.stdout.feed_data(b'{"sessionStarted":true}\n{"callAnswered":true}\n')
            return process
        self.runtime.spawn = spawn
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
    async def test_saved_recording_plays_only_after_the_call(self):
        key = 'ab' * 16
        self.runtime.items[key] = {'id': key, 'state': 'ended', 'address': PHONE}
        missing = await self.client.get(f'/v1/conversations/{key}/recording')
        self.assertEqual(missing.status, 404)
        folder = self.runtime.records.root / key
        folder.mkdir(parents=True)
        (folder / 'call.wav').write_bytes(b'RIFFdemo')
        ready = await self.client.get(f'/v1/conversations/{key}/recording')
        self.assertEqual(ready.status, 200)
        self.assertEqual(ready.content_type, 'audio/wav')
        self.assertEqual(await ready.read(), b'RIFFdemo')
        self.runtime.items[key]['state'] = 'active'
        busy = await self.client.get(f'/v1/conversations/{key}/recording')
        self.assertEqual(busy.status, 409)
        outside = self.root / 'outside.wav'
        outside.write_bytes(b'nope')
        saved = folder / 'call.wav'
        saved.unlink()
        saved.symlink_to(outside)
        self.runtime.items[key]['state'] = 'ended'
        denied = await self.client.get(f'/v1/conversations/{key}/recording')
        self.assertEqual(denied.status, 404)

    async def test_device_name_reaches_snapshot_and_device_list(self):
        self.phones.names = {PHONE: "Pixel 9"}
        snapshot = await (await self.client.get("/v1/console")).json()
        self.assertEqual({d["address"]: d["name"] for d in snapshot["devices"]}, {PHONE: "Pixel 9", OTHER: None})
        listed = await (await self.client.get("/v1/devices")).json()
        self.assertEqual({d["address"]: d["name"] for d in listed["devices"]}, {PHONE: "Pixel 9", OTHER: None})
    async def asyncTearDown(self):
        for process in self.phones.processes.values():
            if process.returncode is None: process.finish()
        await asyncio.sleep(.02)
        await self.client.close()
        self.env.stop()
    async def dial(self, selected=PHONE, task=None):
        return await self.client.post("/v1/conversations", json={"address": selected, "number": "+123", "task": TASK if task is None else task})
    async def until(self, condition, seconds=3):
        async with asyncio.timeout(seconds):
            while not condition(): await asyncio.sleep(.01)
    async def end(self, item):
        with patch("os.killpg"):
            return await self.client.post("/v1/conversations/" + item["id"] + "/end")
    async def test_worker_diagnostics_are_persisted_and_exposed_without_payloads(self):
        response = await self.dial()
        item = await response.json()
        process = self.phones.processes[PHONE]
        process.stdout.feed_data((json.dumps({"diagnostic": {"event": "session_end", "backendCreated": 1,
            "backendCompleted": 1, "observedBackendModels": ["gpt-6-luna"],
            "instructions": "private-prompt", "delta": "private-transcript"}}) + "\n").encode())
        process.finish()
        await self.until(lambda: item["id"] not in self.runtime.sessions)
        detail = self.runtime.public(self.runtime.get(item["id"]))
        self.assertEqual(detail["diagnostics"]["backendCreated"], 1)
        self.assertEqual(detail["diagnostics"]["observedBackendModels"], ["gpt-6-luna"])
        self.assertNotIn("private-", json.dumps(detail))

    async def test_state_websocket_replays_and_ui_disconnect_does_not_stop_worker(self):
        item = await (await self.dial()).json()
        await asyncio.sleep(.08)
        path = '/v1/conversations/' + item['id']
        ws = await self.client.ws_connect(path + '/events')
        self.assertEqual((await ws.receive_json(timeout=1))['state'], 'active')
        await ws.close()
        self.assertIsNone(self.phones.processes[PHONE].returncode)
        self.phones.current[PHONE][0]['state'] = 'held'
        self.phones.changed(PHONE)
        await asyncio.sleep(.08)
        ws = await self.client.ws_connect(path + '/events')
        self.assertEqual((await ws.receive_json(timeout=1))['state'], 'held')
        self.phones.current[PHONE] = []
        self.phones.changed(PHONE)
        self.assertEqual((await ws.receive_json(timeout=1))['state'], 'ended')
        await ws.close()

    async def test_call_state_get_is_cached_and_timeout_does_not_end_worker(self):
        item = await (await self.dial()).json()
        await asyncio.sleep(.08)
        path = '/v1/conversations/' + item['id'] + '/call-state'
        with patch.object(self.phones, 'call_identity', side_effect=TimeoutError):
            self.phones.changed(PHONE)
            await asyncio.sleep(.08)
            for _ in range(5):
                response = await self.client.get(path)
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())['state'], 'unknown')
            self.assertIsNone(self.phones.processes[PHONE].returncode)
            self.assertEqual(self.runtime.get(item['id'])['error'], None)
    async def test_voice_catalog_and_invalid_voice_never_dials(self):
        catalog = await (await self.client.get('/v1/voices')).json()
        self.assertEqual(catalog['defaultVoice'],'gleam')
        self.assertEqual(catalog['model'],'gpt-live-1')
        self.assertEqual(len(catalog['voices']),13)
        self.assertTrue(set(REMOVED_VOICES).isdisjoint(v['id'] for v in catalog['voices']))
        self.assertTrue(all(v['providerVerification']=='not_verified' for v in catalog['voices']))
        for voice in ('unknown',None,{'id':'quartz'}):
            response = await self.client.post('/v1/conversations',json={'address':PHONE,'number':'+123','task':TASK,'voice':voice})
            self.assertEqual(response.status,400)
            self.assertEqual((await response.json())['error']['code'],'invalid_voice')
        self.assertEqual(self.phones.dials,[])
        self.assertFalse(self.runtime.items)
        self.assertEqual((await self.client.post('/v1/voices/unknown/preview')).status,400)

    async def test_removed_voices_rejected_before_calls_previews_or_preference_writes(self):
        path = f'/v1/devices/{PHONE}/voice'
        await self.client.put(path, json={'voice': 'quartz'})
        with patch('server.common.voices.VoicePreviews.synthesize', side_effect=AssertionError('no generation')) as generate:
            for voice in REMOVED_VOICES:
                with self.subTest(voice=voice):
                    responses = [
                        await self.client.post('/v1/conversations', json={'address': PHONE, 'number': '+123', 'task': TASK, 'voice': voice}),
                        await self.client.post(f'/v1/voices/{voice}/preview'),
                        await self.client.put(path, json={'voice': voice}),
                    ]
                    for response in responses:
                        self.assertEqual(response.status, 400)
                        self.assertEqual((await response.json())['error']['code'], 'invalid_voice')
            generate.assert_not_called()
        self.assertEqual(self.runtime.voice_preferences, {PHONE: 'quartz'})
        self.assertFalse(self.phones.dials)
        self.assertFalse(self.runtime.items)
        self.assertFalse(self.runtime.sessions)
        self.assertIsNone(self.app['state'].lease)
        self.assertFalse(list((self.store.root / 'tasks').glob('*.json')))
        self.assertFalse(self.app['voice_previews'].pending)

    async def test_task_is_private_durable_and_frozen_before_dial(self):
        response = await self.dial()
        self.assertEqual(response.status, 201)
        item = await response.json()
        path = self.root/"tasks"/(item["taskId"]+".json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())["content"], TASK["content"])
        self.assertEqual(self.phones.dials, [PHONE])
        body = await (await self.client.get("/v1/conversations/"+item["id"])).json()
        self.assertEqual(body["task"]["purpose"], TASK["purpose"])
    async def test_missing_task_and_save_failure_never_dial(self):
        response = await self.dial(task={})
        self.assertEqual(response.status, 400)
        with patch.object(self.store, "save", side_effect=OSError):
            response = await self.dial()
        self.assertEqual(response.status, 503)
        self.assertEqual(self.phones.dials, [])

    async def test_device_voice_is_shared_per_known_device_without_call_side_effects(self):
        first, second = f'/v1/devices/{PHONE}/voice', f'/v1/devices/{OTHER}/voice'
        self.assertEqual((await (await self.client.get(first)).json())['voice'], 'gleam')
        self.assertEqual((await self.client.put(first, json={'voice': 'quartz'})).status, 200)
        self.assertEqual((await self.client.put(second, json={'voice': 'vesper'})).status, 200)
        for path, voice in ((first, 'quartz'), (second, 'vesper')):
            response = await self.client.get(path)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual((await response.json())['voice'], voice)
        snapshot = await (await self.client.get('/v1/console')).json()
        self.assertEqual({d['address']: d['voice'] for d in snapshot['devices']}, {PHONE: 'quartz', OTHER: 'vesper'})
        self.assertFalse(self.phones.dials)
        self.assertFalse(self.runtime.items)
        self.assertFalse(self.runtime.sessions)
        self.assertIsNone(self.app['state'].lease)
        self.assertFalse(list((self.store.root / 'tasks').glob('*.json')))
        # A second app with the same durable store starts with default preferences.
        other_app = create_app(Phones(), managed=True, store=self.store)
        other = TestClient(TestServer(other_app))
        await other.start_server()
        try:
            self.assertEqual((await (await other.get(first)).json())['voice'], 'gleam')
        finally:
            await other.close()

    async def test_device_voice_validation_and_deletion(self):
        path = f'/v1/devices/{PHONE}/voice'
        for value in (None, {}, '', 'unknown'):
            self.assertEqual((await self.client.put(path, json={'voice': value})).status, 400)
        for invalid, status in (('bad', 400), ('00:00:00:00:00:00', 404)):
            url = f'/v1/devices/{invalid}/voice'
            self.assertEqual((await self.client.get(url)).status, status)
            self.assertEqual((await self.client.put(url, json={'voice': 'quartz'})).status, status)
        self.assertFalse(self.runtime.voice_preferences)
        self.assertNotIn('00:00:00:00:00:00', self.runtime.locks)
        await self.client.put(path, json={'voice': 'quartz'})
        self.assertEqual((await self.client.post(f'/v1/management/{PHONE}/remove')).status, 200)
        self.assertNotIn(PHONE, self.runtime.voice_preferences)
        self.assertEqual((await self.client.get(path)).status, 404)
        self.assertFalse(self.phones.dials)

    async def test_saved_voice_is_default_and_active_task_stays_frozen(self):
        path = f'/v1/devices/{PHONE}/voice'
        await self.client.put(path, json={'voice': 'quartz'})
        first = await (await self.dial()).json()
        self.assertEqual(first['voice'], 'quartz')
        await self.client.put(path, json={'voice': 'vesper'})
        detail = await (await self.client.get('/v1/conversations/' + first['id'])).json()
        self.assertEqual((detail['voice'], detail['task']['voice']), ('quartz', 'quartz'))
        await self.client.post('/v1/conversations/' + first['id'] + '/end')
        response = await self.client.post('/v1/conversations', json={'address': PHONE, 'number': '+123', 'task': TASK, 'voice': 'marin'})
        self.assertEqual((await response.json())['voice'], 'marin')
        self.assertEqual((await (await self.client.get(path)).json())['voice'], 'vesper')
    async def test_old_call_events_do_not_reach_the_next_call(self):
        # Both phones expose call ID "call1". The previous session must not touch the next one.
        first = await (await self.dial(PHONE)).json()
        await asyncio.sleep(.02)
        old_session = self.runtime.sessions[first["id"]]
        old_tracker = old_session.tracker
        self.assertEqual((await self.end(first)).status, 200)
        self.assertNotIn(first["id"], self.runtime.sessions)
        second = await (await self.dial(OTHER)).json()
        await asyncio.sleep(.05)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["taskId"], second["taskId"])
        self.assertEqual(self.runtime.items[second["id"]]["state"], "active")
        # Late events for the old phone and old session.
        self.phones.current[PHONE] = [{"id": "call1", "state": "held"}]
        self.phones.changed(PHONE)
        old_session.publish(0, b"\x00" * 960)
        self.runtime.publish(PHONE, 1, b"\x00" * 960)
        old_tracker.dirty.set()
        await asyncio.sleep(.08)
        self.assertEqual((await self.end(first)).status, 200)
        self.assertEqual((await self.client.post("/v1/conversations/" + first["id"] + "/commands", json={"content": "late"})).status, 409)
        with self.assertRaises(Exception):
            await self.client.ws_connect("/v1/conversations/" + first["id"] + "/monitor")
        detail = await (await self.client.get("/v1/conversations/" + second["id"])).json()
        self.assertEqual((detail["state"], detail["callStatus"]["state"], detail["inputBytes"], detail["outputBytes"]), ("active", "active", 0, 0))
        self.assertEqual(self.runtime.items[first["id"]]["state"], "ended")
        self.assertEqual(self.runtime.items[first["id"]]["callStatus"]["state"], "ended")
        self.assertEqual(self.phones.processes[OTHER].commands, [])
        self.assertIsNone(self.phones.processes[OTHER].returncode)
        self.assertTrue(self.phones.current[OTHER])
        self.assertEqual(self.runtime.line.session, self.runtime.sessions[second["id"]])
    async def test_second_phone_is_rejected_while_line_is_busy(self):
        first = await (await self.dial(PHONE)).json()
        response = await self.dial(OTHER)
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "line_busy")
        self.assertEqual(self.phones.dials, [PHONE])
        snapshot = await (await self.client.get("/v1/console")).json()
        self.assertEqual(snapshot["line"], {"state": "busy", "conversationId": first["id"], "address": PHONE, "reason": None, "audioReady": False})
        self.assertNotIn("capacity", snapshot)
        self.assertNotIn("audioLines", snapshot)
        self.assertEqual((await self.end(first)).status, 200)
        self.assertEqual((await self.client.get("/v1/console")).status, 200)
        self.assertEqual((await (await self.client.get("/v1/console")).json())["line"]["state"], "idle")
        self.assertEqual((await self.dial(OTHER)).status, 201)
    async def test_same_phone_parallel_dial_is_rejected(self):
        responses = await asyncio.gather(self.dial(), self.dial())
        self.assertEqual(sorted(r.status for r in responses), [201,409])
        self.assertEqual(len(self.phones.dials), 1)
    async def test_busy_line_blocks_low_level_lease(self):
        await self.dial()
        for selected in (PHONE, PHONE.lower()):
            response = await self.client.post("/v1/lease", headers={"X-Phone-Line": selected})
            self.assertEqual(response.status, 409)
        self.assertEqual((await self.client.post("/v1/lease")).status, 409)
    async def test_consecutive_calls_on_one_phone_and_switching_phones(self):
        ids = []
        for selected in (PHONE, PHONE, OTHER, PHONE, OTHER):
            item = await (await self.dial(selected)).json()
            await asyncio.sleep(.02)
            self.assertEqual(self.runtime.items[item["id"]]["state"], "active")
            self.assertEqual(self.runtime.line.state, "busy")
            self.assertEqual((await self.end(item)).status, 200)
            self.assertEqual(self.runtime.items[item["id"]]["state"], "ended")
            self.assertEqual(self.runtime.items[item["id"]]["cleanup"], {"local": "released", "phone": "ended"})
            self.assertEqual(self.runtime.line.state, "idle")
            self.assertFalse(self.runtime.sessions)
            self.assertFalse(self.runtime.recovering)
            self.assertEqual(self.phones.current[selected], [])
            ids.append((item["id"], item["taskId"]))
        self.assertEqual(len({i for i, _ in ids}), 5)
        self.assertEqual(len({t for _, t in ids}), 5)
        self.assertEqual(len(self.runtime.items), 5)
    async def test_start_failure_with_failed_hangup_is_not_reported_as_success(self):
        # C01: worker start fails and the hangup of our own call fails too.
        async def spawn(item, task):
            raise GatewayError("worker_spawn_failed", 503)
        self.runtime.spawn = spawn
        with patch.object(self.phones, "hangup_owned", side_effect=GatewayError("phone_failed", 503)):
            response = await self.dial()
            self.assertEqual(response.status, 503)
            item = next(iter(self.runtime.items.values()))
            self.assertEqual((item["state"], item["error"]), ("cleanup_failed", "conversation_start_failed"))
            self.assertEqual(item["cleanup"], {"local": "released", "phone": "unknown"})
            self.assertEqual(item["endReason"], "start_failed")
            self.assertEqual(self.runtime.call_status(item)["state"], "unknown")
            response = await self.client.post("/v1/conversations/" + item["id"] + "/end")
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "phone_hangup_failed")
            self.assertEqual(item["state"], "cleanup_failed")
            snapshot = await (await self.client.get("/v1/console")).json()
            self.assertEqual(snapshot["line"]["state"], "idle")
            self.assertTrue(next(d for d in snapshot["devices"] if d["address"] == PHONE)["recovering"])
            self.assertEqual((await (await self.dial(PHONE)).json())["error"]["code"], "device_recovering")
            self.assertTrue(self.phones.current[PHONE])
        # The phone ends the call by itself: isolation lifts without a redial or a blind hangup.
        self.phones.current[PHONE] = []
        self.phones.changed(PHONE)
        await self.until(lambda: not self.runtime.recovering)
        self.assertEqual((item["state"], item["cleanupPending"]), ("failed", False))
        self.assertEqual(item["cleanup"], {"local": "released", "phone": "ended"})
        self.assertEqual(self.runtime.call_status(item)["state"], "ended")
        self.assertFalse(next(d for d in (await (await self.client.get("/v1/console")).json())["devices"] if d["address"] == PHONE)["recovering"])
        self.assertEqual(self.phones.dials, [PHONE])
    async def test_hangup_sent_but_phone_still_reports_the_call_isolates_only_that_phone(self):
        item = await (await self.dial()).json()
        original = self.phones.hangup_owned
        async def sent_but_not_ended(selected, call_id, identity):
            if selected != PHONE:
                return await original(selected, call_id, identity)
            self.phones.processes[selected].finish()
            return True
        with patch.object(self.phones, "hangup_owned", sent_but_not_ended), patch("server.common.call_session.HANGUP_VERIFY_SECONDS", .1):
            response = await self.end(item)
            self.assertEqual(response.status, 503)
            self.assertEqual(self.runtime.get(item["id"])["state"], "cleanup_failed")
            self.assertEqual(self.runtime.get(item["id"])["cleanup"], {"local": "released", "phone": "unknown"})
            self.assertIn(PHONE, self.runtime.recovering)
            self.assertTrue(self.runtime.line.idle())
            other = await (await self.dial(OTHER)).json()
            self.assertEqual((await self.end(other)).status, 200)
        self.phones.current[PHONE] = []
        self.phones.changed(PHONE)
        await self.until(lambda: PHONE not in self.runtime.recovering)
        self.assertEqual(self.runtime.get(item["id"])["state"], "failed")
        self.assertEqual((await self.dial(PHONE)).status, 201)
    async def test_unreleased_local_resources_put_the_line_into_recovering(self):
        item = await (await self.dial()).json()
        process = self.phones.processes[PHONE]
        original = self.phones.operate
        async def keep_process(selected, operation, *args, **kwargs):
            self.phones.current[selected] = []
            return {"ok": True}
        self.phones.operate = keep_process
        with patch("server.common.call_session.WORKER_EXIT_SECONDS", .05):
            response = await self.end(item)
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "local_cleanup_failed")
        self.assertEqual(self.runtime.items[item["id"]]["cleanup"], {"local": "failed", "phone": "ended"})
        snapshot = await (await self.client.get("/v1/console")).json()
        self.assertEqual(snapshot["line"]["state"], "recovering")
        self.assertEqual(snapshot["line"]["conversationId"], item["id"])
        self.assertFalse(any(d["recovering"] for d in snapshot["devices"]))
        response = await self.dial(OTHER)
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "line_recovering")
        self.assertIn(item["id"], self.runtime.sessions)
        process.finish()
        await asyncio.sleep(.02)
        self.assertEqual((await self.end(item)).status, 200)
        self.assertEqual(self.runtime.items[item["id"]]["state"], "failed")
        self.assertEqual(self.runtime.items[item["id"]]["error"], "worker_exit_failed")
        self.assertEqual(self.runtime.line.state, "idle")
        self.assertFalse(self.runtime.sessions)
        self.phones.operate = original
        self.assertEqual((await self.dial(OTHER)).status, 201)
    async def test_platform_release_error_passes_through_unchanged(self):
        # Mac reports `controller_releasing` (503) while the native pump stops. It is not controller_busy.
        with patch.object(self.phones, "connect", side_effect=GatewayError("controller_releasing", 503)):
            response = await self.client.post("/v1/management/" + PHONE + "/connect")
        self.assertEqual((response.status, (await response.json())["error"]["code"]), (503, "controller_releasing"))
    async def test_isolation_lifts_only_when_the_phone_reports_no_call(self):
        item = await (await self.dial()).json()
        original = self.phones.hangup_owned
        async def failing(selected, call_id, identity):
            if selected == PHONE:
                raise GatewayError("phone_failed", 503)
            return await original(selected, call_id, identity)
        with patch.object(self.phones, "hangup_owned", failing):
            self.phones.processes[PHONE].finish()
            await self.until(lambda: self.runtime.get(item["id"])["state"] == "cleanup_failed")
        # A new call with a reused ID is not ours, but the phone is not free either.
        self.phones.current[PHONE] = [{"id": "call1", "state": "active", "newCall": True}]
        self.phones.changed(PHONE)
        await asyncio.sleep(.08)
        self.assertIn(PHONE, self.runtime.recovering)
        self.assertEqual((await (await self.dial(PHONE)).json())["error"]["code"], "device_recovering")
        self.phones.current[PHONE] = []
        self.phones.changed(PHONE)
        await self.until(lambda: PHONE not in self.runtime.recovering)
        self.assertEqual(self.runtime.get(item["id"])["state"], "failed")
    async def lose_phone_during_call(self):
        """Mac style ACL loss: the adapter is gone, hangup and subscription fail, the worker exits."""
        item = await (await self.dial()).json()
        await asyncio.sleep(.02)
        state = {"lost": True}
        original_path, original_subscribe, original_hangup = self.phones.device_path, self.phones.subscribe_calls, self.phones.hangup_owned
        async def device_path(selected):
            if state["lost"] and selected == PHONE:
                raise GatewayError("device_disconnected", 409)
            return await original_path(selected)
        async def hangup_owned(selected, call_id, identity):
            if state["lost"] and selected == PHONE:
                raise GatewayError("device_disconnected", 409)
            return await original_hangup(selected, call_id, identity)
        def subscribe_calls(selected, callback):
            if state["lost"] and selected == PHONE:
                raise GatewayError("device_disconnected", 409)
            return original_subscribe(selected, callback)
        async def connect(selected, connected):
            state["lost"] = not connected
            if connected:
                self.phones.connected.add(selected)
                self.phones.current[PHONE] = []  # The phone ended the call while it was away.
            else:
                self.phones.connected.discard(selected)
        patches = [patch.object(self.phones, name, value) for name, value in (
            ("device_path", device_path), ("hangup_owned", hangup_owned), ("subscribe_calls", subscribe_calls), ("connect", connect))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.phones.connected.discard(PHONE)
        self.phones.processes[PHONE].finish()
        await self.until(lambda: self.runtime.get(item["id"])["state"] == "cleanup_failed")
        snapshot = await (await self.client.get("/v1/console")).json()
        self.assertEqual(snapshot["line"]["state"], "idle")
        self.assertTrue(next(d for d in snapshot["devices"] if d["address"] == PHONE)["recovering"])
        self.assertEqual((await self.end(item)).status, 503)
        return item, state
    async def test_isolated_device_can_reconnect_and_isolation_lifts_without_a_dial(self):
        item, state = await self.lose_phone_during_call()
        # The UI reconnect and pairing entries stay open while the phone is isolated.
        response = await self.client.post("/v1/management/" + PHONE + "/connect")
        self.assertEqual((response.status, await response.json()), (200, {"ok": True}))
        self.assertFalse(state["lost"])
        await self.until(lambda: PHONE not in self.runtime.recovering)
        self.assertEqual((self.runtime.get(item["id"])["state"], self.runtime.get(item["id"])["cleanupPending"]), ("failed", False))
        self.assertFalse(next(d for d in (await (await self.client.get("/v1/console")).json())["devices"] if d["address"] == PHONE)["recovering"])
        self.assertEqual((await self.dial(PHONE)).status, 201)
    async def test_isolated_device_can_start_pairing_and_device_change_triggers_recheck(self):
        item, state = await self.lose_phone_during_call()
        pair = await self.client.post("/v1/pairings", json={"address": PHONE})
        self.assertEqual(pair.status, 202)
        await self.client.delete("/v1/pairings/" + (await pair.json())["id"])
        self.assertIn(PHONE, self.runtime.recovering)
        # The phone comes back through another route; only a device-state notification arrives.
        state["lost"] = False
        self.phones.connected.add(PHONE)
        self.phones.current[PHONE] = []
        self.phones.on_change()
        await self.until(lambda: PHONE not in self.runtime.recovering)
        self.assertEqual(self.runtime.get(item["id"])["state"], "failed")
        # A reused call ID on the reconnected phone is still verified before any later dial.
        other = await (await self.dial(OTHER)).json()
        self.assertEqual((await self.end(other)).status, 200)
    async def test_remote_end_with_briefly_stale_call_list_does_not_isolate(self):
        # R1: hangup_owned=False (the phone already ended it) while the call list is still stale for a moment.
        item = await (await self.dial()).json()
        await asyncio.sleep(.02)
        queries = []
        original_calls = self.phones.calls
        async def calls(selected):
            queries.append(selected)
            if selected == PHONE and len(queries) < 3:
                return [{"id": "call1", "state": "active"}]
            return await original_calls(selected)
        async def hangup_owned(selected, call_id, identity):
            return False
        self.phones.current[PHONE] = []
        with patch.object(self.phones, "calls", calls), patch.object(self.phones, "hangup_owned", hangup_owned), patch("server.common.call_session.HANGUP_VERIFY_SECONDS", 2):
            self.phones.processes[PHONE].finish()
            await self.until(lambda: item["id"] not in self.runtime.sessions)
        self.assertEqual(self.runtime.get(item["id"])["cleanup"], {"local": "released", "phone": "ended"})
        self.assertEqual(self.runtime.get(item["id"])["state"], "ended")
        self.assertFalse(self.runtime.recovering)
    async def test_remote_end_and_worker_failure_use_the_same_cleanup(self):
        item = await (await self.dial()).json()
        await asyncio.sleep(.02)
        self.phones.current[PHONE] = []
        self.phones.changed(PHONE)
        await asyncio.sleep(.08)
        self.phones.processes[PHONE].finish()
        await self.until(lambda: item["id"] not in self.runtime.sessions)
        self.assertEqual(self.runtime.items[item["id"]]["state"], "ended")
        self.assertEqual(self.runtime.items[item["id"]]["endReason"], "remote_end")
        self.assertEqual(self.runtime.line.state, "idle")
        failed = await (await self.dial()).json()
        self.phones.processes[PHONE].finish(code=3)
        await self.until(lambda: failed["id"] not in self.runtime.sessions)
        self.assertEqual((self.runtime.items[failed["id"]]["state"], self.runtime.items[failed["id"]]["error"]), ("failed", "ai_worker_failed"))
        self.assertEqual(self.runtime.items[failed["id"]]["endReason"], "ai_failed")
        self.assertEqual(self.phones.current[PHONE], [])
        self.assertEqual(self.runtime.line.state, "idle")
    async def test_monitor_is_bounded_drops_copy_only_and_has_two_directions(self):
        item = await (await self.dial()).json()
        queue = asyncio.Queue(maxsize=5)
        self.runtime.sessions[item["id"]].monitors.add(queue)
        for n in range(100): self.runtime.publish(PHONE,n%2,b'\x00'*960)
        self.assertEqual(queue.qsize(),5)
        packets = [queue.get_nowait() for _ in range(5)]
        self.assertEqual({p[0] for p in packets},{0,1})
        self.assertEqual(sum(len(p)-1 for p in packets),4800)
        self.assertIsNone(self.phones.processes[PHONE].returncode)
    async def test_monitor_ws_receives_both_voices(self):
        item = await (await self.dial()).json()
        ws = await self.client.ws_connect("/v1/conversations/"+item["id"]+"/monitor")
        self.runtime.publish(PHONE,0,b'\x01\x00')
        self.runtime.publish(PHONE,1,b'\x02\x00')
        self.assertEqual((await ws.receive(timeout=1)).data,b'\x00\x01\x00')
        self.assertEqual((await ws.receive(timeout=1)).data,b'\x01\x02\x00')
        await ws.close()
        self.assertIsNone(self.phones.processes[PHONE].returncode)
    async def test_commands_bound_to_session_not_reused_call_id(self):
        item = await (await self.dial()).json()
        await asyncio.sleep(.01)
        path = "/v1/conversations/"+item["id"]+"/commands"
        response = await self.client.post(path,json={"content":"Speak Chinese"})
        self.assertEqual(response.status,202)
        self.assertEqual(self.phones.processes[PHONE].commands[0]["conversationId"],item["id"])
        with patch("os.killpg"):
            await self.client.post("/v1/conversations/"+item["id"]+"/end")
        next_item = await (await self.dial()).json()
        self.assertNotEqual(next_item["id"],item["id"])
        self.assertEqual((await self.client.post(path,json={"content":"Wrong session"})).status,409)
        self.assertEqual(self.phones.processes[PHONE].commands,[])

    async def test_command_early_receipt_timeout_size_and_write_failure_are_traceable(self):
        item = await (await self.dial()).json()
        await asyncio.sleep(.02)
        path = '/v1/conversations/'+item['id']+'/commands'
        self.assertEqual((await self.client.post(path,json={'content':'界'*4097})).status,400)
        with patch('server.common.call_session.COMMAND_TIMEOUT', .04):
            response=await self.client.post(path,json={'content':'Ask about Florida. '*40})
            command=await response.json()
            self.assertEqual(response.status,202)
            self.assertGreater(len(command['content'].encode('utf-8')),500)
            await asyncio.sleep(.02)
            self.assertEqual(self.runtime.items[item['id']]['commands'][0]['status'],'written')
            await asyncio.sleep(.05)
            self.assertEqual(self.runtime.items[item['id']]['commands'][0]['status'],'unknown')
            self.assertFalse(self.runtime.sessions[item['id']].command_timers)
        process=self.phones.processes[PHONE]
        with patch.object(process,'write',side_effect=BrokenPipeError):
            failed=await (await self.client.post(path,json={'content':'Second instruction'})).json()
        self.assertEqual(failed['status'],'unknown')
        self.assertEqual(failed['reason'],'worker_write_failed')
        self.assertNotEqual(failed['id'],command['id'])
        self.assertFalse(self.runtime.sessions[item['id']].command_timers)
        self.assertIsNone(self.runtime.items[item['id']]['error'])
    async def test_prompt_changes_next_task_only(self):
        a = await (await self.dial(PHONE)).json()
        await self.client.put("/v1/system-prompt",json={"content":"New prompt"})
        self.assertEqual((await self.end(a)).status, 200)
        b = await (await self.dial(OTHER)).json()
        self.assertNotEqual(self.store.load("tasks",a["taskId"])["systemPrompt"],"New prompt")
        self.assertEqual(self.store.load("tasks",b["taskId"])["systemPrompt"],"New prompt")
    async def test_no_legacy_taskless_dial_in_managed_server(self):
        lease = await (await self.client.post("/v1/lease")).json()
        response = await self.client.post("/v1/devices/"+PHONE+"/calls",json={"number":"+123"},headers={"X-Controller-Lease":lease["lease"]})
        self.assertEqual(response.status,409)
        self.assertEqual(self.phones.dials,[])
    async def test_pairing_numeric_confirmation_and_wrong_target(self):
        pair = await (await self.client.post("/v1/pairings",json={"address":PHONE})).json()
        await asyncio.sleep(.01)
        path = "/v1/pairings/"+pair["id"]
        self.assertEqual((await (await self.client.get(path)).json())["kind"],"confirmation")
        response = await self.client.post(path,json={"address":OTHER,"accept":True})
        self.assertEqual(response.status,409)
        self.assertEqual((await self.client.post(path,json={"address":PHONE,"accept":True})).status,200)
        await asyncio.sleep(.01)
        self.assertEqual((await (await self.client.get(path)).json())["state"],"paired")
        self.assertEqual((await self.client.post(path,json={"address":PHONE,"accept":True})).status,409)
    async def test_cancelled_pairing_rejects_late_response(self):
        pair = await (await self.client.post("/v1/pairings",json={"address":PHONE})).json()
        await asyncio.sleep(.01)
        path = "/v1/pairings/"+pair["id"]
        self.assertEqual((await self.client.delete(path)).status,200)
        self.assertEqual((await self.client.post(path,json={"address":PHONE,"accept":True})).status,409)
    async def test_independent_client_cors_and_unknown_origin(self):
        response = await self.client.options("/v1/conversations",headers={"Origin":"http://127.0.0.1:5173"})
        self.assertEqual(response.status,204)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"],"http://127.0.0.1:5173")
        response = await self.client.post("/v1/discovery",headers={"Origin":"https://untrusted.invalid"})
        self.assertEqual(response.status,403)
    async def test_busy_device_cannot_disconnect_or_remove(self):
        await self.dial()
        for operation in ("disconnect","remove"):
            self.assertEqual((await self.client.post("/v1/management/"+PHONE+"/"+operation)).status,409)

    async def test_new_call_with_reused_id_is_not_hung_up_or_given_old_commands(self):
        item = await (await self.dial()).json()
        await asyncio.sleep(.01)
        self.phones.current[PHONE] = [{"id":"call1","state":"active","newCall":True}]
        self.phones.changed(PHONE)
        await asyncio.sleep(.08)
        path = "/v1/conversations/"+item["id"]
        self.assertEqual((await (await self.client.get(path+"/call-state")).json())["state"],"ended")
        self.assertEqual((await self.client.post(path+"/commands",json={"content":"Do not cross calls"})).status,409)
        self.phones.processes[PHONE].finish()
        await asyncio.sleep(.02)
        self.assertTrue(self.phones.current[PHONE][0]["newCall"])

    async def test_end_waits_for_inflight_dial_and_never_restarts_worker(self):
        gate = asyncio.Event()
        original = self.phones.operate
        async def slow(selected, operation, *args, **kwargs):
            if operation == "dial": await gate.wait()
            return await original(selected,operation,*args,**kwargs)
        self.phones.operate = slow
        create = asyncio.create_task(self.dial())
        for _ in range(30):
            if self.runtime.items: break
            await asyncio.sleep(.01)
        item = next(iter(self.runtime.items.values()))
        with patch("os.killpg"):
            end = asyncio.create_task(self.client.post("/v1/conversations/"+item["id"]+"/end"))
            await asyncio.sleep(.02)
            self.assertFalse(end.done())
            gate.set()
            self.assertEqual((await create).status,201)
            self.assertEqual((await end).status,200)
        self.assertEqual(item["state"],"ended")
        self.assertEqual(self.phones.current[PHONE],[])

    async def test_real_hangup_failure_is_not_reported_as_ended(self):
        item = await (await self.dial()).json()
        original = self.phones.hangup_owned
        async def failing(selected, call_id, identity):
            if selected == PHONE:
                raise GatewayError("phone_failed", 503)
            return await original(selected, call_id, identity)
        with patch.object(self.phones,"hangup_owned",failing), patch("os.killpg"):
            self.phones.processes[PHONE].finish()
            await self.until(lambda: self.runtime.get(item["id"])["state"] == "cleanup_failed")
            response = await self.client.post("/v1/conversations/"+item["id"]+"/end")
            self.assertEqual(response.status,503)
            self.assertEqual((await response.json())["error"]["code"],"phone_hangup_failed")
            self.assertEqual(self.runtime.get(item["id"])["state"],"cleanup_failed")
            # Local resources are released: the line is idle, only this phone is isolated.
            snapshot = await (await self.client.get("/v1/console")).json()
            self.assertEqual(snapshot["line"]["state"], "idle")
            self.assertEqual({d["address"]: d["recovering"] for d in snapshot["devices"]}, {PHONE: True, OTHER: False})
            self.assertFalse(self.runtime.sessions)
            self.assertEqual((await self.dial(PHONE)).status, 409)
            self.assertEqual((await (await self.dial(PHONE)).json())["error"]["code"], "device_recovering")
            other = await (await self.dial(OTHER)).json()
            self.assertEqual((await self.end(other)).status, 200)
        # An explicit retry is bound to the old identity and succeeds once the phone cooperates.
        response = await self.client.post("/v1/conversations/"+item["id"]+"/end")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.runtime.get(item["id"])["state"], "failed")
        self.assertEqual(self.phones.current[PHONE], [])
        self.assertFalse(self.runtime.recovering)

    async def test_shutdown_closes_console_and_monitor_sockets_without_60s_wait(self):
        item = await (await self.dial()).json()
        events = await self.client.ws_connect("/v1/console/events")
        monitor = await self.client.ws_connect("/v1/conversations/"+item["id"]+"/monitor")
        self.phones.processes[PHONE].finish()
        await asyncio.sleep(.02)
        await asyncio.wait_for(self.client.close(),2)
        self.assertTrue(events.closed)
        self.assertTrue(monitor.closed)


class PairingVariants(unittest.IsolatedAsyncioTestCase):
    async def test_passkey_pin_validation_and_expiry(self):
        owner = Phones()
        pairings = Pairings(owner)
        for kind,valid,invalid in (("passkey","000123","123"),("pin","1234","")):
            key = kind
            pairings.sessions[key] = {"id":key,"address":PHONE,"state":"connecting"}
            task = asyncio.create_task(pairings.ask(key,PHONE,kind))
            await asyncio.sleep(0)
            with self.assertRaises(GatewayError): pairings.respond(key,{"address":PHONE,"code":invalid})
            pairings.respond(key,{"address":PHONE,"code":valid})
            self.assertEqual(await task,valid)
        key="expired"
        pairings.sessions[key] = {"id":key,"address":PHONE,"state":"connecting"}
        task = asyncio.create_task(pairings.ask(key,PHONE,"passkey"))
        await asyncio.sleep(0)
        pairings.sessions[key]["deadline"] = 0
        with self.assertRaises(GatewayError): pairings.respond(key,{"address":PHONE,"code":"000123"})
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
