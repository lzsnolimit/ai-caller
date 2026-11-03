"""来电自动接听、来电 prompt、会话方向与持久化。替身手机，不做蓝牙操作。"""
import asyncio
import tempfile
import json
import os
from pathlib import Path
import secrets
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from server.common.api import create_app
from server.common.contracts import GatewayError
from server.common.inbound import DEFAULT_INBOUND_PROMPT, INBOUND_OUTCOME, INBOUND_PURPOSE
from server.common.storage import Store
from server.tests.test_console import Phones, Process, PHONE, OTHER, TASK

DELAY = .12


class InboundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(tempfile.gettempdir()) / "phone-call-gateway/inbound/I1/tests" / secrets.token_hex(8)
        self.store = Store(self.root)
        self.phones = Phones()
        self.env = patch.dict(os.environ, {"PHONE_VOICE_SAMPLE_DIR": str(self.root / 'samples')})
        self.env.start()
        self.delay = patch("server.common.inbound.INBOUND_ANSWER_DELAY", DELAY)
        self.delay.start()
        self.app, self.client = await self.boot(self.phones)
        self.runtime = self.app["conversations"]

    async def boot(self, phones):
        app = create_app(phones, managed=True, store=self.store)
        async def spawn(item, task):
            process = Process()
            phones.processes[item["address"]] = process
            process.stdout.feed_data(b'{"sessionStarted":true}\n{"callAnswered":true}\n')
            return process
        app["conversations"].spawn = spawn
        client = TestClient(TestServer(app))
        await client.start_server()
        return app, client

    async def asyncTearDown(self):
        for process in self.phones.processes.values():
            if process.returncode is None:
                process.finish()
        await asyncio.sleep(.02)
        await self.client.close()
        self.delay.stop()
        self.env.stop()

    async def until(self, condition, seconds=3):
        async with asyncio.timeout(seconds):
            while not condition():
                await asyncio.sleep(.01)

    def ring(self, selected=PHONE, call_id="call1"):
        self.phones.current[selected] = [{"id": call_id, "state": "incoming"}]
        self.phones.changed(selected)

    async def dial(self, selected=PHONE):
        return await (await self.client.post("/v1/conversations", json={"address": selected, "number": "+123", "task": TASK})).json()

    async def end(self, item):
        with patch("os.killpg"):
            return await self.client.post("/v1/conversations/" + item["id"] + "/end")

    async def accept(self, selected=PHONE):
        self.ring(selected)
        await self.until(lambda: any(i["direction"] == "inbound" and i["state"] == "active" for i in self.runtime.items.values()))
        return next(i for i in self.runtime.items.values() if i["direction"] == "inbound" and i["address"] == selected)

    def transcript(self, item, text, seq, role="caller"):
        self.runtime.transcripts.get(item).accept({'type': 'transcript.delta', 'sequence': seq, 'role': role,
                                                   'eventId': f'e{seq}', 'text': text, 'startMs': seq, 'endMs': seq + 1})

    # ---- 来电接听 ----
    async def test_incoming_call_is_answered_after_the_delay_with_a_guarded_inbound_task(self):
        self.ring()
        await asyncio.sleep(DELAY / 2)
        self.assertEqual(self.phones.answers, [])
        self.assertTrue(self.runtime.line.idle())
        item = await self.accept()
        self.assertEqual(self.phones.answers, [(PHONE, "call1")])
        self.assertEqual(self.phones.dials, [])
        self.assertEqual(self.runtime.line.session, self.runtime.sessions[item["id"]])
        self.assertIsNotNone(item.get("answeredAt"))
        task = self.store.load("tasks", item["taskId"])
        self.assertEqual((task["direction"], task["purpose"], task["outcome"], task["content"]), ("inbound", INBOUND_PURPOSE, INBOUND_OUTCOME, DEFAULT_INBOUND_PROMPT))
        self.assertTrue(task["systemPrompt"].startswith(self.runtime.prompt + "\n"))
        self.assertIn("不回答", task["systemPrompt"])
        self.assertEqual(task["systemPrompt"], self.runtime.prompt + "\n" + self.runtime.guard_prompt)
        self.assertEqual(self.phones.processes[PHONE].commands, [])
        snapshot = await (await self.client.get("/v1/console")).json()
        self.assertEqual(snapshot["line"]["conversationId"], item["id"])
        listed = next(c for c in snapshot["conversations"] if c["id"] == item["id"])
        self.assertEqual((listed["direction"], listed["state"], listed["callStatus"]["state"]), ("inbound", "active", "active"))
        detail = await (await self.client.get("/v1/conversations/" + item["id"])).json()
        self.assertEqual(detail["task"]["direction"], "inbound")
        # 来电与去电一样可以发指令、监听和结束；统一收尾挂断本通。
        self.assertEqual((await self.client.post("/v1/conversations/" + item["id"] + "/commands", json={"content": "问清楚来意"})).status, 202)
        self.assertEqual((await self.end(item)).status, 200)
        self.assertEqual((item["state"], item["endReason"], item["cleanup"]), ("ended", "user_end", {"local": "released", "phone": "ended"}))
        self.assertEqual(self.phones.current[PHONE], [])
        self.assertTrue(self.runtime.line.idle())
        self.assertFalse(self.runtime.recovering)

    async def test_busy_line_leaves_the_incoming_call_to_the_phone(self):
        outbound = await self.dial(OTHER)
        self.ring(PHONE)
        await asyncio.sleep(DELAY * 2)
        self.assertEqual(self.phones.answers, [])
        self.assertEqual(self.phones.current[PHONE], [{"id": "call1", "state": "incoming"}])
        self.assertEqual([i["direction"] for i in self.runtime.items.values()], ["outbound"])
        self.assertEqual((await self.end(self.runtime.items[outbound["id"]])).status, 200)
        # 仍在响铃且线路已空闲：下一次通话变化通知后再按正常流程接听。
        self.phones.changed(PHONE)
        item = await self.accept()
        self.assertEqual(self.phones.answers, [(PHONE, "call1")])
        self.assertEqual((await self.end(item)).status, 200)

    async def test_incoming_call_that_disappears_during_the_delay_is_not_answered(self):
        self.ring()
        await asyncio.sleep(DELAY / 2)
        self.phones.current[PHONE] = []
        self.phones.changed(PHONE)
        await asyncio.sleep(DELAY * 2)
        self.assertEqual(self.phones.answers, [])
        self.assertFalse(self.runtime.items)
        self.assertTrue(self.runtime.line.idle())
        # 本人在手机上接了：状态不再是 incoming，同样不动。
        self.phones.current[PHONE] = [{"id": "call1", "state": "incoming"}]
        self.phones.changed(PHONE)
        await asyncio.sleep(DELAY / 2)
        self.phones.current[PHONE][0]["state"] = "active"
        self.phones.changed(PHONE)
        await asyncio.sleep(DELAY * 2)
        self.assertEqual(self.phones.answers, [])
        self.assertFalse(self.runtime.items)
        self.assertEqual(self.phones.current[PHONE], [{"id": "call1", "state": "active"}])

    async def test_isolated_device_lease_and_second_call_are_not_answered(self):
        self.runtime.recovering[PHONE] = object()
        self.ring(PHONE)
        await asyncio.sleep(DELAY * 2)
        self.assertEqual(self.phones.answers, [])
        self.runtime.recovering.pop(PHONE)
        self.phones.current[PHONE] = []
        lease = await (await self.client.post("/v1/lease")).json()
        self.ring(PHONE)
        await asyncio.sleep(DELAY * 2)
        self.assertEqual(self.phones.answers, [])
        await self.client.delete("/v1/lease", headers={"X-Controller-Lease": lease["lease"]})
        # 手机上已有别的通话时也不接。
        self.phones.current[PHONE] = [{"id": "call1", "state": "active"}, {"id": "call2", "state": "incoming"}]
        self.phones.changed(PHONE)
        await asyncio.sleep(DELAY * 2)
        self.assertEqual(self.phones.answers, [])
        self.assertFalse(self.runtime.items)

    async def test_identity_unavailable_while_ringing_falls_back_to_call_id_and_state(self):
        original = self.phones.call_identity
        async def identity(selected, call_id):
            if self.phones.current[selected] and self.phones.current[selected][0]["state"] == "incoming":
                return None
            return await original(selected, call_id)
        with patch.object(self.phones, "call_identity", identity):
            item = await self.accept()
            self.assertEqual(self.phones.answers, [(PHONE, "call1")])
            self.assertIsNotNone(item["identity"])
            self.assertEqual((await self.end(item)).status, 200)

    async def test_answer_failure_finishes_without_rejecting_the_ringing_call(self):
        original = self.phones.operate
        async def failing(selected, operation, *args, **kwargs):
            if operation == "answer":
                self.phones.answers.append((selected, kwargs.get("call_id")))
                raise GatewayError("hfp_query_failed", 503)
            return await original(selected, operation, *args, **kwargs)
        with patch.object(self.phones, "operate", failing):
            self.ring()
            await self.until(lambda: any(i["state"] not in ("answering",) for i in self.runtime.items.values()))
            item = next(iter(self.runtime.items.values()))
            await self.until(lambda: item["id"] not in self.runtime.sessions)
        self.assertEqual(self.phones.answers, [(PHONE, "call1")])
        self.assertEqual((item["direction"], item["state"], item["endReason"], item["error"]), ("inbound", "failed", "answer_failed", "inbound_answer_failed"))
        self.assertEqual(item["cleanup"], {"local": "released", "phone": "ended"})
        self.assertEqual(self.phones.current[PHONE], [{"id": "call1", "state": "incoming"}])
        self.assertTrue(self.runtime.line.idle())
        self.assertFalse(self.runtime.recovering)
        self.assertNotIn(PHONE, self.phones.processes)

    async def test_unknown_phone_state_after_answer_failure_isolates_without_hangup(self):
        original = self.phones.operate
        hangups = []
        gate = asyncio.Event()
        async def failing(selected, operation, *args, **kwargs):
            if operation == "answer":
                await gate.wait()
                raise GatewayError("answer_not_confirmed", 503)
            hangups.append(operation)
            return await original(selected, operation, *args, **kwargs)
        async def broken_calls(selected):
            raise GatewayError("hfp_query_timeout", 503)
        with patch.object(self.phones, "operate", failing):
            self.ring()
            # 线路先占用，会话随后登记；等会话出现再取。
            await self.until(lambda: self.runtime.line.state == "busy" and self.runtime.items)
            item = next(iter(self.runtime.items.values()))
            with patch.object(self.phones, "calls", broken_calls), patch("server.common.call_session.HANGUP_VERIFY_SECONDS", .2):
                gate.set()
                await self.until(lambda: item["state"] == "cleanup_failed")
                self.assertEqual(item["cleanup"], {"local": "released", "phone": "unknown"})
                self.assertIn(PHONE, self.runtime.recovering)
                self.assertTrue(self.runtime.line.idle())
                # 用户重试结束：没有身份，只核对，不挂断。
                self.assertEqual((await self.client.post("/v1/conversations/" + item["id"] + "/end")).status, 503)
            self.assertEqual(hangups, [])
            self.assertEqual(self.phones.current[PHONE], [{"id": "call1", "state": "incoming"}])
            # 手机自己结束来电后隔离解除。
            self.phones.current[PHONE] = []
            self.phones.changed(PHONE)
            await self.until(lambda: PHONE not in self.runtime.recovering)
            self.assertEqual((item["state"], item["endReason"]), ("failed", "answer_failed"))
        self.assertEqual(hangups, [])

    async def test_shutdown_never_hangs_up_an_unanswered_incoming_call(self):
        gate = asyncio.Event()
        original = self.phones.operate
        hangups = []
        async def slow(selected, operation, *args, **kwargs):
            if operation == "answer":
                await gate.wait()
                raise GatewayError("answer_not_confirmed", 503)
            hangups.append((selected, operation))
            return await original(selected, operation, *args, **kwargs)
        with patch.object(self.phones, "operate", slow):
            self.ring()
            # 会话已登记、正在接听时关闭服务。
            await self.until(lambda: self.runtime.line.state == "busy" and self.runtime.items)
            closing = asyncio.create_task(self.runtime.close())
            await asyncio.sleep(.02)
            gate.set()
            await asyncio.wait_for(closing, 3)
        self.assertEqual(hangups, [])
        self.assertEqual(self.phones.current[PHONE], [{"id": "call1", "state": "incoming"}])
        item = next(iter(self.runtime.items.values()))
        self.assertEqual((item["state"], item["endReason"]), ("failed", "answer_failed"))

    async def test_shutdown_while_writing_the_task_does_not_answer(self):
        original_task, original_operate = self.runtime.store.inbound_task, self.phones.operate
        operations = []
        def task(*args, **kwargs):
            # 线路已占用、会话尚未登记时服务开始关闭。
            self.runtime.closing = True
            return original_task(*args, **kwargs)
        async def operate(selected, operation, *args, **kwargs):
            operations.append(operation)
            return await original_operate(selected, operation, *args, **kwargs)
        with patch.object(self.runtime.store, "inbound_task", task), patch.object(self.phones, "operate", operate):
            self.ring()
            await self.until(lambda: self.runtime.closing and self.runtime.line.state == "idle")
            await asyncio.sleep(.1)
        self.assertNotIn("answer", operations)
        self.assertNotIn("hangup", operations)
        self.assertEqual(self.runtime.line.state, "idle")
        self.assertEqual(self.phones.current[PHONE], [{"id": "call1", "state": "incoming"}])

    async def test_identity_never_reaches_persisted_files_or_logs(self):
        item = await self.accept()
        self.assertIsNotNone(item["identity"])
        self.transcript(item, "身份测试", 1)
        self.assertEqual((await self.end(item)).status, 200)
        await self.runtime.diagnostics.close()
        await asyncio.sleep(.05)
        folder = self.root / "conversations" / item["id"]
        for path in (folder / "meta.json", folder / "transcript.jsonl", self.root / "tasks" / (item["taskId"] + ".json"), self.root / "logs" / "calls.jsonl"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("identity", text, path.name)
            self.assertNotIn("call1", text, path.name)

    async def test_late_answer_after_command_error_is_hung_up_by_the_cleanup(self):
        # Mac 真机：ATA 回了错误码但电话已接通。adapter 应返回 ok；若仍抛错，本通已是 active 的来电由统一收尾挂断。
        original = self.phones.operate
        async def answered_but_error(selected, operation, *args, **kwargs):
            if operation == "answer":
                await original(selected, operation, *args, **kwargs)
                raise GatewayError("hfp_query_failed", 503)
            return await original(selected, operation, *args, **kwargs)
        with patch.object(self.phones, "operate", answered_but_error):
            self.ring()
            await self.until(lambda: bool(self.runtime.items))
            item = next(iter(self.runtime.items.values()))
            await self.until(lambda: item["id"] not in self.runtime.sessions)
        self.assertEqual((item["state"], item["endReason"]), ("failed", "answer_failed"))
        self.assertEqual(item["cleanup"], {"local": "released", "phone": "ended"})
        self.assertEqual(self.phones.current[PHONE], [])

    async def test_inbound_answer_and_outbound_dial_cannot_both_take_the_line(self):
        gate = asyncio.Event()
        original = self.phones.operate
        async def slow(selected, operation, *args, **kwargs):
            if operation == "answer":
                await gate.wait()
            return await original(selected, operation, *args, **kwargs)
        with patch.object(self.phones, "operate", slow):
            self.ring(PHONE)
            await self.until(lambda: self.runtime.line.state == "busy")
            response = await self.client.post("/v1/conversations", json={"address": OTHER, "number": "+123", "task": TASK})
            self.assertEqual((response.status, (await response.json())["error"]["code"]), (409, "line_busy"))
            gate.set()
            await self.until(lambda: any(i["state"] == "active" for i in self.runtime.items.values()))
        self.assertEqual(self.phones.dials, [])
        item = next(iter(self.runtime.items.values()))
        self.assertEqual((await self.end(item)).status, 200)

    async def test_disconnected_device_drops_its_subscription_and_pending_answer(self):
        self.ring(PHONE)
        await asyncio.sleep(DELAY / 3)
        self.phones.connected.discard(PHONE)
        self.phones.on_change()
        await asyncio.sleep(DELAY * 2)
        self.assertEqual(self.phones.answers, [])
        self.assertNotIn(PHONE, self.runtime.inbound.subscriptions)
        self.assertFalse(self.phones.listeners[PHONE])
        self.phones.connected.add(PHONE)
        self.phones.on_change()
        await self.until(lambda: PHONE in self.runtime.inbound.subscriptions)
        # 重新连接后已有的来电也会被看到。
        item = await self.accept()
        self.assertEqual((await self.end(item)).status, 200)

    # ---- 每设备来电 prompt ----
    async def test_device_inbound_prompt_api_default_guard_validation_and_use(self):
        path = f"/v1/devices/{PHONE}/inbound-prompt"
        body = await (await self.client.get(path)).json()
        self.assertEqual((body["address"], body["content"], body["default"]), (PHONE, DEFAULT_INBOUND_PROMPT, True))
        self.assertEqual(body["guard"], self.runtime.guard_prompt)
        self.assertIn("优先", body["guard"])
        for invalid in ([], 1, {"content": 5}, {"content": "x" * 16385}):
            response = await self.client.put(path, json=invalid)
            self.assertEqual((response.status, (await response.json())["error"]["code"]), (400, "invalid_inbound_prompt"))
        for bad, status in (("bad", 400), ("00:00:00:00:00:00", 404)):
            self.assertEqual((await self.client.get(f"/v1/devices/{bad}/inbound-prompt")).status, status)
            self.assertEqual((await self.client.put(f"/v1/devices/{bad}/inbound-prompt", json={"content": "x"})).status, status)
        self.assertFalse(self.runtime.inbound_prompts)
        response = await self.client.put(path, json={"content": "这里是张先生的助理，请问您找谁？"})
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["default"])
        snapshot = await (await self.client.get("/v1/console")).json()
        self.assertEqual({d["address"]: d["inboundPrompt"] for d in snapshot["devices"]}, {PHONE: {"configured": True}, OTHER: {"configured": False}})
        self.assertEqual(self.store.load("devices", "inbound-prompts"), {PHONE: "这里是张先生的助理，请问您找谁？"})
        self.assertEqual(self.runtime.guard_prompt, self.runtime.guard_prompt.strip())
        self.assertFalse(any(route.resource and "guard" in str(route.resource.canonical) for route in self.app.router.routes()))
        item = await self.accept()
        task = self.store.load("tasks", item["taskId"])
        self.assertEqual(task["content"], "这里是张先生的助理，请问您找谁？")
        self.assertTrue(task["systemPrompt"].endswith(self.runtime.guard_prompt))
        self.assertEqual((await self.end(item)).status, 200)
        # 空内容恢复默认；另一个进程读取同一存储得到同样的配置。
        self.assertTrue((await (await self.client.put(path, json={"content": "  "})).json())["default"])
        await self.client.put(path, json={"content": "再次配置"})
        other_app, other = await self.boot(Phones())
        try:
            self.assertEqual((await (await other.get(path)).json())["content"], "再次配置")
        finally:
            await other.close()

    async def test_removed_device_forgets_its_inbound_prompt(self):
        path = f"/v1/devices/{PHONE}/inbound-prompt"
        await self.client.put(path, json={"content": "配置"})
        self.assertEqual((await self.client.post(f"/v1/management/{PHONE}/remove")).status, 200)
        self.assertNotIn(PHONE, self.runtime.inbound_prompts)
        self.assertEqual(self.store.load("devices", "inbound-prompts"), {})

    # ---- 方向与持久化 ----
    async def test_both_directions_are_persisted_with_meta_and_full_transcript(self):
        outbound = self.runtime.items[(await self.dial(PHONE))["id"]]
        await self.until(lambda: outbound["state"] == "active")
        for seq in range(1, 601):
            self.transcript(outbound, f"第 {seq} 句", seq, "caller" if seq % 2 else "ai")
        full = await (await self.client.get("/v1/conversations/" + outbound["id"] + "/transcript")).json()
        self.assertEqual((full["conversationId"], full["direction"], full["complete"], len(full["events"])), (outbound["id"], "outbound", False, 600))
        self.assertEqual(full["events"][0], {"seq": 1, "role": "caller", "eventId": "e1", "text": "第 1 句", "startMs": 1, "endMs": 2})
        self.assertLess(len(self.runtime.transcripts.get(outbound).events), 600)
        self.assertEqual((await self.end(outbound)).status, 200)
        inbound = await self.accept(OTHER)
        self.transcript(inbound, "你好，我找李先生", 1)
        self.assertEqual((await self.end(inbound)).status, 200)
        await asyncio.sleep(.05)
        for item in (outbound, inbound):
            folder = self.root / "conversations" / item["id"]
            lines = (folder / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 600 if item is outbound else 1)
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual((meta["id"], meta["direction"], meta["address"], meta["state"], meta["endReason"], meta["taskId"]),
                             (item["id"], item["direction"], item["address"], "ended", "user_end", item["taskId"]))
            self.assertEqual(meta["cleanup"], {"local": "released", "phone": "ended"})
            self.assertTrue(meta["answeredAt"] and meta["endedAt"] >= meta["created"])
            self.assertNotIn("number", json.dumps(meta))
            self.assertNotIn("+123", json.dumps(meta))
        full = await (await self.client.get("/v1/conversations/" + inbound["id"] + "/transcript")).json()
        self.assertEqual((full["complete"], full["state"], [e["text"] for e in full["events"]]), (True, "ended", ["你好，我找李先生"]))
        listing = await (await self.client.get("/v1/conversations")).json()
        self.assertEqual([c["direction"] for c in listing["conversations"]], ["outbound", "inbound"])
        for query, expected in (("?direction=inbound", [inbound["id"]]), ("?direction=outbound", [outbound["id"]]),
                                (f"?address={OTHER.lower()}", [inbound["id"]]), (f"?direction=inbound&address={PHONE}", [])):
            self.assertEqual([c["id"] for c in (await (await self.client.get("/v1/conversations" + query)).json())["conversations"]], expected)
        self.assertEqual((await self.client.get("/v1/conversations?direction=sideways")).status, 400)
        self.assertEqual((await self.client.get("/v1/conversations?address=bad")).status, 400)
        # 新进程加载历史 meta，不加载转写正文；完整转写按需读取。
        other_app, other = await self.boot(Phones())
        try:
            runtime = other_app["conversations"]
            self.assertEqual([i["direction"] for i in runtime.items.values()], ["outbound", "inbound"])
            self.assertFalse(runtime.transcripts.buffers)
            history = await (await other.get("/v1/conversations?direction=inbound")).json()
            self.assertEqual([(c["id"], c["state"], c["callStatus"]["state"]) for c in history["conversations"]], [(inbound["id"], "ended", "ended")])
            detail = await (await other.get("/v1/conversations/" + inbound["id"])).json()
            self.assertEqual(detail["task"]["direction"], "inbound")
            full = await (await other.get("/v1/conversations/" + outbound["id"] + "/transcript")).json()
            self.assertEqual(len(full["events"]), 600)
            self.assertEqual((await other.post("/v1/conversations/" + outbound["id"] + "/end")).status, 200)
        finally:
            await other.close()

    async def test_record_write_failure_does_not_affect_the_call(self):
        with patch.object(self.runtime.records, "write_event", side_effect=OSError), patch.object(self.runtime.records, "write_meta", side_effect=OSError):
            item = await self.accept()
            self.transcript(item, "写盘失败", 1)
            await asyncio.sleep(.05)
            self.assertEqual(item["state"], "active")
            self.assertEqual(item["error"], None)
            self.assertEqual((await self.end(item)).status, 200)
        self.assertEqual(item["state"], "ended")

    async def test_unfinished_history_from_a_previous_process_is_loaded_as_failed(self):
        key = secrets.token_hex(16)
        folder = self.root / "conversations" / key
        folder.mkdir(parents=True)
        (folder / "meta.json").write_text(json.dumps({"id": key, "direction": "inbound", "address": PHONE, "created": 1.0,
                                                      "state": "active", "taskId": None, "voice": "marin"}), encoding="utf-8")
        (self.root / "conversations" / "not-a-conversation").mkdir()
        other_app, other = await self.boot(Phones())
        try:
            item = other_app["conversations"].items[key]
            self.assertEqual((item["state"], item["error"], item["endReason"], item["direction"]), ("failed", "server_restarted", "server_restart", "inbound"))
            listing = await (await other.get("/v1/conversations")).json()
            self.assertEqual(listing["conversations"][0]["id"], key)
            await asyncio.sleep(.05)
            self.assertEqual(json.loads((folder / "meta.json").read_text(encoding="utf-8"))["state"], "failed")
        finally:
            await other.close()
