import asyncio
import unittest
import time

from aiohttp import WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from phone_gateway.adapter import DBusPhoneAdapter, GatewayError, GATEWAY, DEVICE, CALL, ROOT
from phone_gateway.api import create_app
from phone_gateway.audio import PipeWireAudio

PHONE = "AA:BB:CC:DD:EE:FF"
OTHER = "11:22:33:44:55:66"


class FakeAdapter:
    async def start(self): pass
    async def close(self): pass
    async def device_path(self, addr):
        if addr != PHONE:
            raise GatewayError("device_not_found", 404)
        return "/device"
    async def capabilities(self): return {"nativeTelephony": False}
    async def devices(self): return [{"address": PHONE, "connected": True}]
    async def calls(self, addr):
        await self.device_path(addr)
        return []
    async def connect(self, addr, connected): await self.device_path(addr)
    async def operate(self, *args, **kwargs): raise GatewayError("unsupported_capability", 503)


class FakeAudio:
    instances = []
    startup_gate = None
    cleanup_gate = None
    write_gate = None
    capture_data = None
    write_delay = 0
    cleanup_error = False
    def __init__(self, addr):
        self.addr, self.closed = addr, False
        self.writes = []
        self.instances.append(self)
    async def start(self):
        if self.startup_gate:
            await self.startup_gate.wait()
    async def read(self):
        if self.capture_data:
            return self.capture_data
        await asyncio.Future()
    async def monitor(self):
        await asyncio.Future()
    async def write(self, data):
        if self.write_gate:
            await self.write_gate.wait()
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        self.writes.append(data)
    async def close(self):
        if self.cleanup_error:
            raise RuntimeError("simulated cleanup failure")
        if self.cleanup_gate:
            await self.cleanup_gate.wait()
        self.closed = True


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeAudio.instances = []
        FakeAudio.startup_gate = FakeAudio.cleanup_gate = None
        FakeAudio.write_gate = FakeAudio.capture_data = None
        FakeAudio.write_delay = 0
        FakeAudio.cleanup_error = False
        self.app = create_app(FakeAdapter(), FakeAudio)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {}

    async def asyncTearDown(self):
        FakeAudio.startup_gate = FakeAudio.cleanup_gate = None
        FakeAudio.write_gate = FakeAudio.capture_data = None
        FakeAudio.write_delay = 0
        FakeAudio.cleanup_error = False
        # The deliberate failure test repairs its fake host before shutdown.
        self.app["state"].audio_cleanup_failed = False
        await self.client.close()

    async def acquire(self):
        response = await self.client.post("/v1/lease", headers=self.headers)
        self.assertEqual(response.status, 200)
        value = await response.json()
        return {**self.headers, "X-Controller-Lease": value["lease"]}

    async def open_audio(self, headers=None):
        headers = headers or await self.acquire()
        ws = await self.client.ws_connect("/v1/audio?address=" + PHONE, headers=headers)
        self.assertEqual(await ws.receive_json(timeout=2), {"type": "audio.ready"})
        return ws, headers

    async def test_no_auth_required_for_reads(self):
        for path in ("/v1/capabilities", "/v1/devices"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
        response = await self.client.get("/v1/capabilities", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["nativeTelephony"])

    async def test_control_lease_and_capability_errors(self):
        response = await self.client.post("/v1/devices/" + PHONE + "/calls", json={"number": "+123"}, headers=self.headers)
        self.assertEqual(response.status, 409)
        headers = await self.acquire()
        response = await self.client.post("/v1/lease", headers=self.headers)
        self.assertEqual(response.status, 409)
        response = await self.client.put("/v1/lease", headers=headers)
        self.assertEqual(response.status, 200)
        response = await self.client.post("/v1/devices/" + PHONE + "/calls", json={"number": "+123"}, headers=headers)
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "unsupported_capability")
        response = await self.client.post("/v1/devices/" + PHONE + "/tones", json={"tones": "$(bad)"}, headers=headers)
        self.assertEqual(response.status, 400)
        response = await self.client.delete("/v1/lease", headers=headers)
        self.assertEqual(response.status, 200)

    async def test_wrong_address_and_audio_needs_lease(self):
        with self.assertRaises(WSServerHandshakeError) as error:
            await self.client.ws_connect("/v1/audio?address=" + PHONE, headers=self.headers)
        self.assertEqual(error.exception.status, 409)
        headers = await self.acquire()
        with self.assertRaises(WSServerHandshakeError) as error:
            await self.client.ws_connect("/v1/audio?address=" + OTHER, headers=headers)
        self.assertEqual(error.exception.status, 404)
        self.assertFalse(FakeAudio.instances)

    async def test_single_audio_owner_and_cleanup(self):
        ws, headers = await self.open_audio()
        with self.assertRaises(WSServerHandshakeError) as error:
            await self.client.ws_connect("/v1/audio?address=" + PHONE, headers=headers)
        self.assertEqual(error.exception.status, 409)
        await ws.send_bytes(b"\x00\x00" * 50)
        for _ in range(20):
            if FakeAudio.instances[0].writes:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(FakeAudio.instances[0].writes, [b"\x00\x00" * 50])
        await ws.close()
        await asyncio.wait_for(self.app["state"].audio_done.wait(), 2)
        self.assertTrue(FakeAudio.instances[0].closed)

    async def test_malformed_pcm_frames(self):
        headers = await self.acquire()
        for data in (b"", b"x", "text"):
            ws, _ = await self.open_audio(headers)
            if isinstance(data, str):
                await ws.send_str(data)
            else:
                await ws.send_bytes(data)
            event = await ws.receive_json(timeout=2)
            self.assertEqual(event, {"type": "error", "code": "invalid_pcm_frame"})
            await ws.close()
            await asyncio.wait_for(self.app["state"].audio_done.wait(), 2)
            self.assertTrue(FakeAudio.instances[-1].closed)

    async def test_oversize_frame_cleanup(self):
        ws, _ = await self.open_audio()
        await ws.send_bytes(b"\x00" * 962)
        for _ in range(3):
            msg = await ws.receive(timeout=2)
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
        await ws.close()
        await asyncio.wait_for(self.app["state"].audio_done.wait(), 2)
        self.assertTrue(FakeAudio.instances[0].closed)

    async def test_startup_binary_rejected_and_disconnect_cleanup(self):
        FakeAudio.startup_gate = asyncio.Event()
        headers = await self.acquire()
        ws = await self.client.ws_connect("/v1/audio?address=" + PHONE, headers=headers)
        await ws.send_bytes(b"\x00\x00")
        self.assertEqual(await ws.receive_json(timeout=2), {"type": "error", "code": "audio_not_ready"})
        await ws.close()
        await asyncio.wait_for(self.app["state"].audio_done.wait(), 2)
        self.assertTrue(FakeAudio.instances[0].closed)
        ws = await self.client.ws_connect("/v1/audio?address=" + PHONE, headers=headers)
        await ws.close()
        await asyncio.wait_for(self.app["state"].audio_done.wait(), 2)
        self.assertTrue(FakeAudio.instances[1].closed)

    async def test_release_waits_for_full_audio_cleanup(self):
        ws, headers = await self.open_audio()
        FakeAudio.cleanup_gate = asyncio.Event()
        release = asyncio.create_task(self.client.delete("/v1/lease", headers=headers))
        await ws.receive(timeout=2)
        await asyncio.sleep(0.02)
        self.assertFalse(release.done())
        self.assertIsNotNone(self.app["state"].lease)
        FakeAudio.cleanup_gate.set()
        self.assertEqual((await release).status, 200)
        self.assertTrue(FakeAudio.instances[0].closed)
        await self.acquire()

    async def test_expiry_closes_audio(self):
        ws, _ = await self.open_audio()
        self.app["state"].expires = time.monotonic() - 1
        self.app["state"].lease_changed.set()
        await ws.receive(timeout=2)
        await asyncio.wait_for(self.app["state"].audio_done.wait(), 2)
        self.assertTrue(FakeAudio.instances[0].closed)
        # Expired renewal is not permitted.
        await self.acquire()

    async def test_cleanup_failure_blocks_takeover(self):
        ws, headers = await self.open_audio()
        FakeAudio.cleanup_error = True
        await ws.close()
        await asyncio.wait_for(self.app["state"].audio_done.wait(), 2)
        response = await self.client.delete("/v1/lease", headers=headers)
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "audio_cleanup_failed")
        self.app["state"].expires = time.monotonic() - 1
        response = await self.client.post("/v1/lease")
        self.assertEqual(response.status, 503)
        self.assertIsNotNone(self.app["state"].audio_ws)

    async def test_capture_queue_tolerates_scheduling_burst(self):
        FakeAudio.capture_data = b"\x00" * 960
        ws, _ = await self.open_audio()
        for _ in range(30):
            message = await ws.receive(timeout=2)
            self.assertEqual(message.type, WSMsgType.BINARY)
            self.assertEqual(len(message.data), 960)
        await ws.close()
        await asyncio.wait_for(self.app["state"].audio_done.wait(), 2)

    async def test_event_snapshot_is_sanitized(self):
        ws = await self.client.ws_connect("/v1/events", headers=self.headers)
        data = await ws.receive_json(timeout=2)
        self.assertEqual(data["type"], "phone.state")
        self.assertNotIn("number", str(data))
        await ws.close()
        ws = await self.client.ws_connect("/v1/events", headers=self.headers)
        self.assertEqual((await ws.receive_json(timeout=2))["type"], "phone.state")
        await ws.close()

    async def test_playback_overload_closes_instead_of_queueing_stale_audio(self):
        FakeAudio.write_gate = asyncio.Event()
        ws, _ = await self.open_audio()
        for _ in range(14):
            await ws.send_bytes(b"\x00" * 960)
        self.assertEqual(await ws.receive_json(timeout=2), {"type": "error", "code": "audio_backpressure"})
        await ws.close()
        await asyncio.wait_for(self.app["state"].audio_done.wait(), 2)
        self.assertTrue(FakeAudio.instances[0].closed)

    async def test_normal_cloud_burst_does_not_false_trip_backpressure(self):
        headers = await self.acquire()
        for delay in (0, 0.02):
            FakeAudio.write_delay = delay
            ws, _ = await self.open_audio(headers)
            bridge = FakeAudio.instances[-1]
            for _ in range(30):
                await ws.send_bytes(b"\x00" * 960)
            deadline = time.monotonic() + 2
            while len(bridge.writes) < 30 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(len(bridge.writes), 30)
            self.assertFalse(ws.closed)
            await ws.close()
            await asyncio.wait_for(self.app["state"].audio_done.wait(), 2)


class ObjectAdapter(DBusPhoneAdapter):
    def __init__(self, native=True):
        super().__init__()
        self.native = native
        self.identity_tracking = native
        self.invocations = []
        self.answered = False
    async def _call(self, bus, service, path, interface, method, signature="", body=None, guard=None):
        if guard is not None and not guard():
            raise GatewayError("call_not_found", 409)
        self.invocations.append((path, interface, method, signature, body))
        if method == "GetManagedObjects":
            if path == "/":
                return [{"/bluez/first": {DEVICE: {"Address": OTHER}}, "/bluez/second": {DEVICE: {"Address": PHONE}}}]
            if not self.native:
                raise GatewayError("unsupported_capability", 503)
            if path == ROOT:
                return [{ROOT + "/ag1": {GATEWAY: {"Address": OTHER}}, ROOT + "/ag9": {GATEWAY: {"Address": PHONE}}}]
            state = "active" if self.answered else "incoming"
            return [{path + "/call3": {CALL: {"State": state, "LineIdentification": "private", "Name": "private"}}}]
        if method == "Introspect":
            return ['<node><interface name="' + interface.replace("org.freedesktop.DBus.Introspectable", GATEWAY if "/call" not in path else CALL) + '"><method name="Dial"/><method name="SendTones"/><method name="Answer"/><method name="Hangup"/></interface></node>']
        if method == "Dial":
            return [path + "/call4"]
        if method == "Answer":
            self.answered = True  # 接听以通话状态为准：Answer 之后 State 变为 active。
        return []


class MappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_equal_call_ids_are_scoped_to_each_phone_address(self):
        adapter = ObjectAdapter()
        # Both phones expose call3. The address, not the call ID alone, owns it.
        for requested, gateway in ((PHONE, "/ag9"), (OTHER, "/ag1")):
            calls = await adapter.calls(requested)
            self.assertEqual([call["id"] for call in calls], ["call3"])
            await adapter.operate(requested, "hangup", call_id="call3")
            self.assertEqual(adapter.invocations[-1][0], ROOT + gateway + "/call3")

    async def test_actual_address_mapping_and_call_ownership(self):
        adapter = ObjectAdapter()
        self.assertEqual(await adapter.gateway_path(PHONE.lower()), ROOT + "/ag9")
        self.assertEqual(await adapter.operate(PHONE, "dial", "+123"), {"id": "call4"})
        self.assertEqual(adapter.invocations[-1][0], ROOT + "/ag9")
        self.assertEqual(await adapter.calls(PHONE), [{"id": "call3", "state": "incoming", "multiparty": False}])
        self.assertEqual(await adapter.operate(PHONE, "answer", call_id="call3"), {"ok": True})
        self.assertIn((ROOT + "/ag9/call3", CALL, "Answer"), [i[:3] for i in adapter.invocations])
        self.assertEqual(await adapter.calls(PHONE), [{"id": "call3", "state": "active", "multiparty": False}])
        with self.assertRaises(GatewayError):
            await adapter.operate(PHONE, "hangup", call_id="../ag1/call3")
        with self.assertRaises(GatewayError) as error:
            await adapter.gateway_path("FF:FF:FF:FF:FF:FF")
        self.assertEqual(error.exception.code, "device_not_found")

    async def test_native_absent_is_explicit(self):
        adapter = ObjectAdapter(False)
        data = await adapter.capabilities()
        self.assertFalse(data["nativeTelephony"])
        self.assertEqual(data["phones"], [])
        with self.assertRaises(GatewayError) as error:
            await adapter.operate(PHONE, "dial", "+123")
        self.assertEqual(error.exception.code, "unsupported_capability")

    async def test_low_level_message_matches_native_contract(self):
        try:
            from dbus_next import Message, MessageType
        except ImportError:
            self.skipTest("dbus-next is tested in the dependency environment")
        class Bus:
            async def call(self, msg):
                self.message = msg
                return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1,
                    signature="o", body=[ROOT + "/ag9/call4"])
        bus = Bus()
        adapter = DBusPhoneAdapter()
        value = await adapter._call(bus, "org.pipewire.Telephony", ROOT + "/ag9", GATEWAY, "Dial", "s", ["+123"])
        self.assertEqual(value, [ROOT + "/ag9/call4"])
        self.assertEqual(bus.message.path, ROOT + "/ag9")
        self.assertEqual(bus.message.signature, "s")
        self.assertEqual(bus.message.member, "Dial")


class FakeProcess:
    def __init__(self, output=b"", hang=False):
        self.returncode = None
        self.output, self.hang = output, hang
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.terminated = self.killed = False
    async def communicate(self):
        self.returncode = 0
        return self.output, b""
    def terminate(self):
        self.terminated = True
        if not self.hang:
            self.returncode = -15
    def kill(self):
        self.killed = True
        self.returncode = -9
    async def wait(self):
        if self.returncode is None:
            await asyncio.Future()
        return self.returncode
    def write(self, data): pass
    async def drain(self):
        if self.hang:
            await asyncio.Future()


class AudioProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_requested_ports_are_linked_and_children_exit(self):
        processes, commands = [], []
        audio = None
        async def spawn(*args, **kwargs):
            commands.append(args)
            output = b""
            if args == ("pw-link", "-o"):
                output = ("bluez_input.AA_BB_CC_DD_EE_FF.7:output_MONO\n" + audio.tx + ":output_MONO\nbluez_input.11_22_33_44_55_66.0:output_MONO\n").encode()
            if args == ("pw-link", "-i"):
                output = ("bluez_output.AA_BB_CC_DD_EE_FF.8:input_MONO\n" + audio.rx + ":input_MONO\n").encode()
            proc = FakeProcess(output)
            processes.append(proc)
            return proc
        audio = PipeWireAudio(PHONE, spawn)
        await audio.start()
        self.assertIn(("pw-link", "bluez_input.AA_BB_CC_DD_EE_FF.7:output_MONO", audio.rx + ":input_MONO"), commands)
        self.assertIn(("pw-link", audio.tx + ":output_MONO", "bluez_output.AA_BB_CC_DD_EE_FF.8:input_MONO"), commands)
        self.assertEqual(len(audio.children), 2)
        await audio.close()
        self.assertTrue(processes[2].terminated)
        self.assertTrue(processes[3].terminated)
        self.assertFalse(audio.children)

    async def test_cleanup_after_second_child_spawn_fails(self):
        processes = []
        async def spawn(*args, **kwargs):
            if args[-1] == "--help":
                return FakeProcess()
            if processes:
                raise FileNotFoundError()
            proc = FakeProcess()
            processes.append(proc)
            return proc
        audio = PipeWireAudio(PHONE, spawn)
        with self.assertRaises(GatewayError):
            await audio.start()
        await audio.close()
        self.assertTrue(processes[0].terminated)

    async def test_pcm_capture_keeps_sample_alignment(self):
        audio = PipeWireAudio(PHONE)
        audio.capture = FakeProcess()
        audio.capture.stdout.feed_data(b"x")
        reader = asyncio.create_task(audio.read())
        await asyncio.sleep(0)
        self.assertFalse(reader.done())
        audio.capture.stdout.feed_data(b"x" * 959)
        self.assertEqual(len(await reader), 960)

    async def test_cancel_during_spawn_tracks_and_cleans_child(self):
        gate = asyncio.Event()
        entered_spawn = asyncio.Event()
        process = FakeProcess()
        async def spawn(*args, **kwargs):
            entered_spawn.set()
            await gate.wait()
            return process
        audio = PipeWireAudio(PHONE, spawn)
        startup = asyncio.create_task(audio.start())
        await entered_spawn.wait()
        startup.cancel()
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await startup
        await audio.close()
        self.assertTrue(process.terminated)

    async def test_raw_mode_is_selected_per_installed_tool_once(self):
        for record_supported, play_supported in ((True, True), (False, False), (True, False)):
            with self.subTest(record=record_supported, play=play_supported):
                commands = []
                audio = None
                async def spawn(*args, **kwargs):
                    commands.append(args)
                    output = b""
                    if args[-1] == "--help":
                        supported = record_supported if args[0] == "pw-record" else play_supported
                        output = b"  -a, --raw   Read or write raw data\n" if supported else b"  --rate RATE\n  --format FORMAT\n"
                    elif args == ("pw-link", "-o"):
                        output = ("bluez_input.AA_BB_CC_DD_EE_FF.0:output_MONO\n" + audio.tx + ":output_MONO\n").encode()
                    elif args == ("pw-link", "-i"):
                        output = ("bluez_output.AA_BB_CC_DD_EE_FF.1:input_MONO\n" + audio.rx + ":input_MONO\n").encode()
                    return FakeProcess(output)
                audio = PipeWireAudio(PHONE, spawn)
                await audio.start()
                for tool, supported in (("pw-record", record_supported), ("pw-play", play_supported)):
                    self.assertEqual(commands.count((tool, "--help")), 1)
                    stream = next(args for args in commands if args[0] == tool and args[-1] == "-")
                    self.assertEqual("--raw" in stream, supported)
                    self.assertEqual(stream[stream.index("--format") + 1], "s16")
                    self.assertEqual(stream[stream.index("--rate") + 1], "24000")
                    self.assertEqual(stream[stream.index("--channels") + 1], "1")
                await audio.close()


if __name__ == "__main__":
    unittest.main()
