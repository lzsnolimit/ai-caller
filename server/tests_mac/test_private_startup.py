"""Actual private socket and Node reader, with a substitute HFP controller."""
import asyncio
import tempfile
import json
from pathlib import Path
import secrets
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server.common.ai_audio import WorkerAudio
from server.common.api import State
from server.common.call_state import CallState
from server.mac.adapter import MacSCOAudio
from test_mac_gateway import fixture, PHONE

ROOT = Path(__file__).resolve().parents[1]


class PrivateStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter, self.protocol, _ = fixture()
        self.records = []
        self.bridge = MacSCOAudio(PHONE, self.adapter)
        self.item = {'id': secrets.token_hex(16), 'address': PHONE, 'callId': 'call1', 'identity': None, 'error': None}
        self.tracker = CallState(self.adapter, self.item, lambda: None)
        # WorkerAudio 由公共层的 CallSession 持有；这里只提供它读取的属性。
        self.session = SimpleNamespace(hub=State(self.adapter, lambda _: self.bridge), item=self.item,
            diagnostics=SimpleNamespace(write=lambda _, value: self.records.append(value)),
            tracker=self.tracker, notify=lambda: None, publish=lambda *_: None, finishing=False)
        self.audio = WorkerAudio(self.session)
        await self.audio.claim()
        self.proc = self.producer = None

    async def asyncTearDown(self):
        if self.producer:
            self.producer.cancel()
            await asyncio.gather(self.producer, return_exceptions=True)
        await asyncio.wait_for(self.audio.close(), 2)
        if self.proc:
            if self.proc.returncode is None:
                self.proc.terminate()
            await asyncio.wait_for(self.proc.wait(), 2)
        self.assertIsNone(self.audio.state.lease)
        self.assertIsNone(self.adapter.audio)
        self.assertFalse(self.tracker.listeners)
        self.assertEqual(self.protocol.commands, [])

    async def node(self):
        directory = Path(tempfile.gettempdir()) / 'phone-call-gateway/startup-tests'/secrets.token_hex(8)
        directory.mkdir(parents=True)
        script = directory/'reader.mts'
        script.write_text("import {PrivateAudio} from " + json.dumps(str(ROOT/'common/ai/src/private-audio.ts')) + ";\n"
            "const audio=new PrivateAudio(Number(process.argv[2]));\n"
            "audio.on('failure',failure=>console.log(JSON.stringify({failure})));\n"
            "audio.on('ready',()=>console.log(JSON.stringify({ready:true})));\n"
            "audio.on('pcm',pcm=>{audio.send(pcm);console.log(JSON.stringify({bytes:pcm.length}));});\n"
            "process.stdin.once('data',()=>{void audio.openAudio();});\n"
            "console.log(JSON.stringify({loaded:true}));\n")
        self.proc = await asyncio.create_subprocess_exec('node', '--import',
            str(ROOT/'common/ai/node_modules/tsx/dist/loader.mjs'), str(script),
            str(self.audio.child.fileno()), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, pass_fds=(self.audio.child.fileno(),))
        self.audio.child.close()
        self.audio.task = asyncio.create_task(self.audio.run())
        self.assertEqual(await self.line(), {'loaded': True})

    async def line(self):
        data = await asyncio.wait_for(self.proc.stdout.readline(), 4)
        if not data:
            self.fail((await self.proc.stderr.read()).decode())
        return json.loads(data)

    async def open_node(self):
        self.proc.stdin.write(b'open\n')
        await self.proc.stdin.drain()

    async def test_delayed_reader_valid_first_frame_and_duplex(self):
        await self.node()
        async def produce():
            while True:
                if self.adapter.audio:
                    self.adapter.audio.receive(bytes(320))
                await asyncio.sleep(.02)
        self.producer = asyncio.create_task(produce())
        # The old path fails at this actual boundary, with its normal socket/queue limits.
        await asyncio.sleep(1.2)
        self.assertIsNone(self.adapter.audio)
        self.assertEqual(self.bridge.capture.maxsize, 10)
        self.assertIsNone(self.bridge.failure)
        await self.open_node()
        self.assertEqual(await self.line(), {'ready': True})
        frames = [await self.line() for _ in range(30)]
        self.assertEqual(frames[0], {'bytes': 956})
        self.assertTrue(all(frame == {'bytes': 960} for frame in frames[1:]))
        # Echo returns through the actual Node writer and Python playback path.
        self.assertGreater(self.bridge.output_bytes, 0)
        self.assertIsNone(self.item['error'])
        self.assertFalse(any(r['event'] == 'operation_failure' for r in self.records))

    async def test_cancel_before_reader(self):
        await self.node()
        await self.audio.close()
        self.assertIsNone(self.audio.bridge)
        self.assertIsNone(self.item['error'])

    async def test_cancel_during_sco_start(self):
        self.adapter.sco_ready.clear()
        await self.node()
        await self.open_node()
        for _ in range(100):
            if self.audio.bridge:
                break
            await asyncio.sleep(.01)
        self.assertIs(self.audio.bridge, self.bridge)
        await self.audio.close()
        self.assertIsNone(self.item['error'])

    async def test_worker_exit_before_reader_is_classified(self):
        await self.node()
        self.proc.terminate()
        await self.proc.wait()
        await asyncio.wait_for(self.audio.task, 2)
        failure = next(r for r in self.records if r['event'] == 'operation_failure')
        self.assertEqual((failure['errorStage'], failure['errorCode'], failure['errorType']),
            ('reader_ready', 'audio_peer_closed', 'IncompleteReadError'))

    async def test_reader_timeout_cleans_up_without_starting_sco(self):
        with patch('server.common.ai_audio.STARTUP_SECONDS', .03):
            self.audio.task = asyncio.create_task(self.audio.run())
            await asyncio.wait_for(self.audio.task, 1)
        self.assertIsNone(self.audio.bridge)
        self.assertEqual(self.item['error'], 'audio_reader_timeout')
        failure = next(r for r in self.records if r['event'] == 'operation_failure')
        self.assertEqual(failure['causeType'], 'TimeoutError')

    async def test_invalid_handshake_does_not_start_sco(self):
        packet = b'\x01not-json'
        self.audio.child.sendall(struct.pack('!I', len(packet)) + packet)
        self.audio.task = asyncio.create_task(self.audio.run())
        await asyncio.wait_for(self.audio.task, 1)
        self.assertIsNone(self.audio.bridge)
        self.assertEqual(self.item['error'], 'invalid_audio_handshake')
