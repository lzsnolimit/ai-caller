"""Actual app -> worker -> local TLS model, with substitute phones and PCM only."""
import asyncio
import tempfile
import base64
import json
import hashlib
import io
import threading
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import unittest
import wave
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from server.common.api import create_app
from server.common.ai_audio import AudioMetrics
from server.common.storage import Store
from server.common.diagnostics import PrivateHandler
from test_console import Phones, PHONE, OTHER, TASK


class Audio:
    def __init__(self, address):
        self.address = address
        self.input = asyncio.Queue()
        self.output = asyncio.Queue()
        self.closed = False
        self.write_delay = 0
        self.write_gate = None
        self.write_started = asyncio.Event()
    async def start(self): pass
    async def read(self): return await self.input.get()
    async def write(self, data):
        self.write_started.set()
        if self.write_gate is not None:
            await self.write_gate.wait()
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        await self.output.put(data)
    async def monitor(self): await asyncio.Future()
    async def close(self): self.closed = True


class ManagedAudioTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.gettempdir()) / 'phone-call-gateway/managed-audio-tests'/secrets.token_hex(8)
        cls.root.mkdir(parents=True)
        cls.cert, cls.key = cls.root/'cert.pem', cls.root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-keyout',str(cls.key),'-out',str(cls.cert),'-subj','/CN=localhost',
            '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cls.key.chmod(0o600)

    async def asyncSetUp(self):
        self.models = []
        self.preview_mode = 'audio'
        self.command_ack = True
        async def model(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            record = {'ws': ws, 'input': asyncio.Queue(), 'comments': [], 'instructions': [], 'audio_times': [], 'sample_frames': [],
                      'close_received': False, 'input_after_close': 0, 'output_bytes': 0}
            self.models.append(record)
            async for message in ws:
                event = message.json()
                if event['type'] == 'session.start':
                    record['session'] = event['session']
                    await ws.send_json({'type':'session.started'})
                elif event['type'] == 'session.input_audio.append':
                    record['input_after_close'] += int(record['close_received'])
                    await record['input'].put(base64.b64decode(event['audio']))
                    record['audio_times'].append(asyncio.get_running_loop().time())
                    if record['comments'] and record['comments'][0].startswith('Say exactly once: '):
                        count = len(record['audio_times'])
                        if self.preview_mode == 'error' and count == 3:
                            await ws.send_json({'type': 'error', 'error': {'message': 'synthetic refusal'}})
                        elif self.preview_mode == 'disconnect' and count == 3:
                            await ws.close()
                        elif self.preview_mode == 'audio' and count in (3, 6):
                            record['sample_frames'].append(count)
                            await ws.send_json({'type':'session.output_audio.delta','delta':base64.b64encode(bytes([3,0])*480).decode()})
                        elif self.preview_mode == 'continuous':
                            # One normal 20 ms output frame per input frame. The last
                            # packet crosses the cap to exercise sample-aligned trim.
                            pcm = bytes([3, 0]) * (960 if count == 400 else 480)
                            record['output_bytes'] += len(pcm)
                            await ws.send_json({'type': 'session.output_audio.delta', 'delta': base64.b64encode(pcm).decode()})
                        elif self.preview_mode.startswith('malformed_') and count == 3:
                            delta = {'malformed_empty': '', 'malformed_odd': base64.b64encode(b'x').decode(),
                                     'malformed_base64': '%%%', 'malformed_large': 'A' * (512000 + 4)}[self.preview_mode]
                            await ws.send_json({'type': 'session.output_audio.delta', 'delta': delta})
                elif event['type'] == 'session.commentary.append':
                    record['comments'].append(event['content'])
                elif event['type'] == 'session.instructions.append':
                    record['instructions'].append(event)
                    if self.command_ack:
                        await ws.send_json({'type': 'session.instructions.appended', 'client_event_id': event['event_id'], 'event_id': 'server-receipt', 'start_ms': 1, 'end_ms': 2})
                elif event['type'] == 'session.close':
                    record['close_received'] = True
                    if self.preview_mode == 'continuous':
                        # Model output can already be in flight when close arrives.
                        record['output_bytes'] += 960
                        await ws.send_json({'type': 'session.output_audio.delta', 'delta': base64.b64encode(bytes(960)).decode()})
                    await ws.send_json({'type':'session.closed','reason':'completed'})
            return ws
        model_app = web.Application()
        model_app.router.add_get('/live', model)
        self.model_server = TestServer(model_app)
        # Host trust injection is for clients; this fixture is a TLS server.
        context_type = ssl.SSLContext
        if context_type.__module__.startswith('truststore'):
            from truststore._api import _original_SSLContext
            context_type = _original_SSLContext
        context = context_type(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        await self.model_server.start_server(ssl=context)
        self.env = patch.dict(os.environ, {'OPENAI_API_KEY':'offline-fixture',
            'OPENAI_LIVE_URL':str(self.model_server.make_url('/live')).replace('https:', 'wss:'),
            'NODE_EXTRA_CA_CERTS':str(self.cert), 'OPENAI_BACKEND_MODEL':'gpt-6-luna',
            'PHONE_VOICE_SAMPLE_DIR': str(self.root / 'samples')})
        self.env.start()
        self.phones = Phones()
        self.audio = {}
        def factory(address):
            value = self.audio[address] = Audio(address)
            return value
        self.app = create_app(self.phones, factory, managed=True, store=Store(self.root/secrets.token_hex(4)))
        self.runtime = self.app['conversations']
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.model_server.close()
        self.env.stop()

    async def until(self, condition):
        async with asyncio.timeout(6):
            while not condition(): await asyncio.sleep(.01)

    async def dial(self, address, voice="marin"):
        response = await self.client.post('/v1/conversations', json={'address':address,'number':'+123','task':TASK,'voice':voice})
        self.assertEqual(response.status, 201, await response.text())
        item = await response.json()
        await self.until(lambda: self.runtime.items[item['id']]['ai'] == 'ready' and self.runtime.items[item['id']]['state'] == 'active')
        return item

    async def test_actual_managed_entry_duplex_commands_monitor_and_isolation(self):
        self.assertEqual((await self.client.put(f'/v1/devices/{PHONE}/voice', json={'voice': 'quartz'})).status, 200)
        response = await self.client.post('/v1/conversations', json={'address': PHONE, 'number': '+123', 'task': TASK})
        self.assertEqual(response.status, 201)
        first = await response.json()
        await self.until(lambda: self.runtime.items[first['id']]['ai'] == 'ready' and self.runtime.items[first['id']]['state'] == 'active')
        self.assertEqual((await self.client.post('/v1/lease')).status,409)
        busy = await self.client.post('/v1/conversations', json={'address': OTHER, 'number': '+123', 'task': TASK})
        self.assertEqual((busy.status, (await busy.json())['error']['code']), (409, 'line_busy'))
        async def duplex(index, address, record):
            self.assertEqual(record['session']['model'],'gpt-live-1')
            self.assertEqual(record['session']['audio']['output']['voice'],('quartz','vesper')[index])
            self.assertEqual(record['session']['delegation']['responses']['model'],'gpt-6-luna')
            self.assertIn(TASK['content'],record['session']['instructions'])
            pcm = bytes([index+1,0])*480
            for _ in range(12):
                await self.audio[address].input.put(pcm)
                await record['ws'].send_json({'type':'session.output_audio.delta','delta':base64.b64encode(pcm).decode()})
                self.assertEqual(await asyncio.wait_for(record['input'].get(),2),pcm)
                self.assertEqual(await asyncio.wait_for(self.audio[address].output.get(),2),pcm)
        # A listener which never drains must not block either PCM direction.
        stalled = asyncio.Queue(maxsize=5)
        self.runtime.sessions[first['id']].monitors.add(stalled)
        monitor = await self.client.ws_connect('/v1/conversations/'+first['id']+'/monitor')
        await duplex(0, PHONE, self.models[0])
        self.assertEqual(stalled.qsize(),5)
        await monitor.close()
        path = '/v1/conversations/'+first['id']
        command = await (await self.client.post(path+'/commands',json={'content':'fixture command'})).json()
        await self.until(lambda: self.runtime.items[first['id']]['commands'][0]['status']=='injected')
        self.assertIn(command['status'], ('accepted', 'written', 'injected'))
        self.assertEqual(self.models[0]['instructions'][0]['content'], 'fixture command')
        self.assertEqual(self.models[0]['instructions'][0]['event_id'], 'operator_' + command['id'])
        self.assertNotIn('fixture command',self.models[0]['comments'])
        self.assertEqual((await self.client.post(path+'/end')).status,200)
        self.assertTrue(self.audio[PHONE].closed)
        self.assertIsNone(self.app['state'].lease)
        self.assertFalse(self.runtime.sessions)
        self.assertEqual(self.runtime.line.state, 'idle')
        # The next dial on another phone starts from new objects; the old session is gone.
        second = await self.dial(OTHER, "vesper")
        for item, voice in ((first,"quartz"),(second,"vesper")):
            self.assertEqual(item["voice"],voice)
            self.assertEqual(self.runtime.store.load("tasks",item["taskId"])["voice"],voice)
        self.assertNotEqual(first['id'],second['id'])
        await duplex(1, OTHER, self.models[1])
        self.assertEqual(self.models[1]['instructions'], [])
        self.assertFalse(self.audio[OTHER].closed)
        self.assertEqual((await self.client.post('/v1/conversations/'+second['id']+'/end')).status,200)
        self.assertTrue(self.audio[OTHER].closed)
        self.assertFalse(self.runtime.sessions)
        await self.runtime.diagnostics.close()
        records = [json.loads(line) for line in (self.runtime.store.root/'logs/calls.jsonl').read_text().splitlines()]
        stages = [record for record in records if record['event']=='audio_stage']
        for stage in ('capture_transport','playback_handoff','model_input_send','model_output_handoff'):
            matching = [record for record in stages if record.get('stage')==stage]
            self.assertTrue(matching, stage)
            self.assertEqual({record['conversationId'] for record in matching},{first['id'],second['id']})
            self.assertTrue(all(record['frames']>0 and record['bytes']>0 and record['meanMs']>=0 for record in matching))
        self.assertEqual({record['clockDomain'] for record in stages},{'python_monotonic','node_performance'})
        self.assertNotIn(TASK['content'],str(records))
        self.assertNotIn('fixture command',str(records))
        self.assertEqual({r['commandStatus'] for r in records if r['event']=='operator_command'}, {'accepted','written','injected'})

    async def test_instruction_receipts_stall_without_blocking_duplex_and_rejection_is_local(self):
        self.command_ack = False
        first = await self.dial(PHONE)
        path = '/v1/conversations/' + first['id']
        content = 'Ask about the Florida plan.'
        commands = [await (await self.client.post(path+'/commands', json={'content':content})).json() for _ in range(4)]
        await self.until(lambda: len(self.models[0]['instructions']) == 4)
        await self.until(lambda: all(c['status']=='written' for c in self.runtime.items[first['id']]['commands']))
        self.assertEqual(len({c['id'] for c in commands}), 4)
        ws = self.models[0]['ws']
        await ws.send_json({'type':'session.instructions.appended','client_event_id':'operator_'+'f'*32})
        for address, record in zip((PHONE,), self.models):
            pcm = bytes([4,0])*480
            await self.audio[address].input.put(pcm)
            await record['ws'].send_json({'type':'session.output_audio.delta','delta':base64.b64encode(pcm).decode()})
            self.assertEqual(await asyncio.wait_for(record['input'].get(),2),pcm)
            self.assertEqual(await asyncio.wait_for(self.audio[address].output.get(),2),pcm)
        self.assertTrue(all(c['status']=='written' for c in self.runtime.items[first['id']]['commands']))
        await ws.send_json({'type':'error','error':{'client_event_id':'operator_'+commands[0]['id'],'message':content,'type':'invalid_request_error','code':'synthetic_rejection'}})
        ack={'type':'session.instructions.appended','client_event_id':'operator_'+commands[1]['id']}
        await ws.send_json(ack); await ws.send_json(ack)
        await self.until(lambda: self.runtime.items[first['id']]['commands'][1]['status']=='injected')
        detail=await (await self.client.get(path)).json()
        self.assertEqual([c['status'] for c in detail['commands']], ['rejected','injected','written','written'])
        self.assertIsNone(detail['error'])
        self.assertEqual((await self.client.post(path+'/end')).status,200)
        self.assertEqual([c['status'] for c in self.runtime.items[first['id']]['commands']], ['rejected','injected','unknown','unknown'])
        self.assertFalse(self.runtime.sessions)
        second = await self.dial(OTHER)
        self.assertFalse(self.models[1]['instructions'])
        self.assertEqual(self.runtime.items[second['id']]['state'],'active')
        await self.client.post('/v1/conversations/'+second['id']+'/end')
        await self.runtime.diagnostics.close()
        raw=(self.runtime.store.root/'logs/calls.jsonl').read_text()
        self.assertNotIn(content,raw)
        rows=[json.loads(line) for line in raw.splitlines() if 'operator_command' in line]
        self.assertEqual({r['commandId'] for r in rows},{c['id'] for c in commands})
        self.assertTrue(all('durationMs' in r and 'timestampMs' in r for r in rows))

    async def test_long_instruction_and_provider_token_limit_rejection_preserve_audio(self):
        item = await self.dial(PHONE)
        path = '/v1/conversations/' + item['id']
        content = 'hello ' * 100
        self.assertGreater(len(content.encode('utf-8')),500)
        accepted = await (await self.client.post(path+'/commands',json={'content':content})).json()
        await self.until(lambda: self.runtime.items[item['id']]['commands'][0]['status']=='injected')
        self.assertEqual(self.models[0]['instructions'][0]['content'],content)
        self.assertEqual(self.models[0]['instructions'][0]['event_id'],'operator_'+accepted['id'])
        self.command_ack = False
        limited = 'word ' * 600
        response = await self.client.post(path+'/commands',json={'content':limited})
        self.assertEqual(response.status,202)
        command = await response.json()
        await self.until(lambda: len(self.models[0]['instructions'])==2)
        self.assertEqual(self.models[0]['instructions'][1]['content'],limited)
        ws = self.models[0]['ws']
        await ws.send_json({'type':'error','error':{'client_event_id':'operator_'+command['id'],
            'type':'invalid_request_error','code':'synthetic_token_limit','message':'Instructions exceed the 500 token limit.'}})
        await self.until(lambda: self.runtime.items[item['id']]['commands'][1]['status']=='rejected')
        self.assertEqual(self.runtime.items[item['id']]['commands'][1]['reason'],'provider_rejected')
        self.assertIsNone(self.runtime.items[item['id']]['error'])
        pcm=bytes([5,0])*480
        await self.audio[PHONE].input.put(pcm)
        await ws.send_json({'type':'session.output_audio.delta','delta':base64.b64encode(pcm).decode()})
        self.assertEqual(await asyncio.wait_for(self.models[0]['input'].get(),2),pcm)
        self.assertEqual(await asyncio.wait_for(self.audio[PHONE].output.get(),2),pcm)
        self.assertEqual(self.runtime.items[item['id']]['state'],'active')
        self.assertFalse(self.runtime.sessions[item['id']].command_timers)
        self.assertEqual((await self.client.post(path+'/end')).status,200)

    async def test_voice_preview_tls_cache_and_active_call_isolation(self):
        item = await self.dial(PHONE)
        preview = self.app['voice_previews']
        client_session = aiohttp.ClientSession
        certificate = ssl.PEM_cert_to_DER_cert(self.cert.read_text())
        context = aiohttp.Fingerprint(hashlib.sha256(certificate).digest())
        def session(**kwargs):
            return client_session(connector=aiohttp.TCPConnector(ssl=context), **kwargs)
        with patch('server.common.voices.aiohttp.ClientSession', side_effect=session):
            first = asyncio.create_task(self.client.post('/v1/voices/quartz/preview'))
            second = asyncio.create_task(self.client.post('/v1/voices/quartz/preview'))
            await self.until(lambda: len(self.models) == 2 and bool(self.models[1]['comments']))
            await self.models[0]['ws'].send_json({'type': 'session.input_transcript.delta',
                'event_id': 'during-preview', 'delta': 'Synthetic caller during preview', 'start_ms': 1, 'end_ms': 20})
            await self.until(lambda: self.runtime.transcripts.get(self.runtime.items[item['id']]).revision == 1)
            history = await (await self.client.get('/v1/conversations/' + item['id'] + '/transcript')).json()
            self.assertEqual([event['text'] for event in history['events']], ['Synthetic caller during preview'])
            pcm = bytes([9,0])*480
            await self.audio[PHONE].input.put(pcm)
            await self.models[0]['ws'].send_json({'type':'session.output_audio.delta','delta':base64.b64encode(pcm).decode()})
            self.assertEqual(await asyncio.wait_for(self.models[0]['input'].get(),2),pcm)
            self.assertEqual(await asyncio.wait_for(self.audio[PHONE].output.get(),2),pcm)
            busy = await self.client.post('/v1/voices/vesper/preview')
            self.assertEqual(busy.status,429)
            a,b = await asyncio.gather(first,second)
        self.assertEqual((a.status,b.status),(200,200))
        data = await a.read()
        self.assertEqual(data,await b.read())
        self.assertTrue(data.startswith(b'RIFF'))
        self.assertLessEqual(len(data),8*48000+44)
        self.assertEqual(self.models[1]['session']['audio']['output']['voice'],'quartz')
        self.assertNotIn(TASK['content'],self.models[1]['session']['instructions'])
        self.assertEqual(self.phones.dials,[PHONE])
        self.assertEqual(set(self.audio),{PHONE})
        self.assertEqual(len(self.runtime.sessions),1)
        self.assertEqual(self.runtime.items[item['id']]['state'],'active')
        cached = await self.client.post('/v1/voices/quartz/preview')
        self.assertEqual(await cached.read(),data)
        self.assertEqual(len(self.models),2)
        catalog = await (await self.client.get('/v1/voices')).json()
        self.assertEqual(next(v for v in catalog['voices'] if v['id']=='quartz')['providerVerification'],'preview_succeeded')
        self.assertFalse(preview.pending)
        sample = self.models[1]
        self.assertEqual(sample['session']['audio']['format'], {'type': 'audio/pcm', 'rate': 24000})
        self.assertEqual(sample['sample_frames'], [3, 6])
        times = sample['audio_times']
        self.assertGreaterEqual(len(times), 6)
        self.assertGreaterEqual(times[5] - times[0], .075)
        self.assertGreaterEqual(times[-1] - times[0], (len(times) - 1) * .015)
        while not sample['input'].empty():
            self.assertEqual(sample['input'].get_nowait(), bytes(960))
        await self.until(lambda: sample['ws'].closed)
        self.assertFalse([task for task in asyncio.all_tasks() if task.get_name() == 'voice-preview-silence'])

    async def test_preview_silence_stops_on_provider_error_disconnect_timeout_and_shutdown(self):
        preview = self.app['voice_previews']
        client_session = aiohttp.ClientSession
        certificate = ssl.PEM_cert_to_DER_cert(self.cert.read_text())
        fingerprint = aiohttp.Fingerprint(hashlib.sha256(certificate).digest())
        def session(**kwargs):
            return client_session(connector=aiohttp.TCPConnector(ssl=fingerprint), **kwargs)
        with patch('server.common.voices.aiohttp.ClientSession', side_effect=session):
            for mode, voice in (('error', 'quartz'), ('disconnect', 'vesper'), ('timeout', 'marin'), ('shutdown', 'willow')):
                with self.subTest(mode=mode):
                    self.preview_mode = mode
                    if mode == 'shutdown':
                        request = asyncio.create_task(preview.get(voice))
                        await self.until(lambda: self.models[-1].get('session', {}).get('audio', {}).get('output', {}).get('voice') == voice
                                         and len(self.models[-1]['audio_times']) >= 3)
                        await asyncio.wait_for(preview.close(), 1)
                        result = await asyncio.gather(request, return_exceptions=True)
                        self.assertIsInstance(result[0], asyncio.CancelledError)
                    else:
                        with patch('server.common.voices.PREVIEW_TIMEOUT', .3 if mode == 'timeout' else 20):
                            response = await self.client.post('/v1/voices/' + voice + '/preview')
                        self.assertEqual(response.status, 502)
                    record = self.models[-1]
                    self.assertGreaterEqual(len(record['audio_times']), 3)
                    await self.until(lambda: record['ws'].closed)
                    self.assertFalse(preview.pending)
                    self.assertFalse(preview.cache)
                    self.assertFalse([task for task in asyncio.all_tasks() if task.get_name() == 'voice-preview-silence'])
        self.assertEqual(self.phones.dials, [])

    async def test_continuous_preview_returns_eight_second_clip_and_closes_without_hfp(self):
        self.preview_mode = 'continuous'
        client_session = aiohttp.ClientSession
        fingerprint = aiohttp.Fingerprint(hashlib.sha256(ssl.PEM_cert_to_DER_cert(self.cert.read_text())).digest())
        def session(**kwargs):
            return client_session(connector=aiohttp.TCPConnector(ssl=fingerprint), **kwargs)
        with patch('server.common.voices.aiohttp.ClientSession', side_effect=session):
            response = await self.client.post('/v1/voices/quartz/preview')
            self.assertEqual(response.status, 200, await response.text() if response.status != 200 else '')
            data = await response.read()
            cached = await self.client.post('/v1/voices/quartz/preview')
            self.assertEqual(await cached.read(), data)
        with wave.open(io.BytesIO(data), 'rb') as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()),
                             (24000, 1, 2, 8 * 24000))
            self.assertEqual(wav.readframes(wav.getnframes()), bytes([3, 0]) * (8 * 24000))
        self.assertEqual(len(data), 384044)
        self.assertEqual(len(self.models), 1)
        record = self.models[0]
        await self.until(lambda: record['ws'].closed)
        self.assertTrue(record['close_received'])
        self.assertGreater(record['output_bytes'], 384000)
        self.assertEqual(record['input_after_close'], 0)
        self.assertEqual(len(record['audio_times']), 400)
        self.assertGreaterEqual(record['audio_times'][-1] - record['audio_times'][0], 7.5)
        while not record['input'].empty():
            self.assertEqual(record['input'].get_nowait(), bytes(960))
        self.assertFalse([task for task in asyncio.all_tasks() if task.get_name() == 'voice-preview-silence'])
        self.assertFalse(self.app['voice_previews'].pending)
        self.assertEqual(self.app['voice_previews'].cache['quartz'], data)
        self.assertEqual(self.phones.dials, [])
        self.assertFalse(self.audio)
        self.assertFalse(self.runtime.items)
        self.assertIsNone(self.app['state'].lease)

    async def test_malformed_preview_pcm_still_fails_without_cache_or_sender(self):
        client_session = aiohttp.ClientSession
        fingerprint = aiohttp.Fingerprint(hashlib.sha256(ssl.PEM_cert_to_DER_cert(self.cert.read_text())).digest())
        def session(**kwargs):
            return client_session(connector=aiohttp.TCPConnector(ssl=fingerprint), **kwargs)
        with patch('server.common.voices.aiohttp.ClientSession', side_effect=session):
            for mode, voice in (('empty', 'marin'), ('odd', 'quartz'), ('base64', 'vesper'), ('large', 'willow')):
                with self.subTest(mode=mode):
                    self.preview_mode = 'malformed_' + mode
                    response = await self.client.post('/v1/voices/' + voice + '/preview')
                    self.assertEqual(response.status, 502)
                    await self.until(lambda: self.models[-1]['ws'].closed)
                    self.assertFalse(self.app['voice_previews'].cache)
                    self.assertFalse(self.app['voice_previews'].pending)
                    self.assertFalse([task for task in asyncio.all_tasks() if task.get_name() == 'voice-preview-silence'])
        self.assertEqual(self.phones.dials, [])
        self.assertFalse(self.audio)

    async def test_real_worker_transcripts_replay_isolation_and_ended_cache(self):
        first = await self.dial(PHONE, 'quartz')
        self.assertEqual(first['voice'], 'quartz')
        self.assertEqual(self.models[0]['session']['audio']['output']['voice'], 'quartz')
        path = '/v1/conversations/' + first['id'] + '/transcript'
        socket = await self.client.ws_connect(path + '/events')
        initial = await socket.receive_json(timeout=2)
        self.assertEqual(initial['events'], [])
        fragments = [('input', 'e1', ' 你好 '), ('input', 'e2', '你好 '), ('output', 'e1', '<img src=x onerror=alert(1)>')]
        for role, event_id, delta in fragments:
            await self.models[0]['ws'].send_json({'type': f'session.{role}_transcript.delta', 'event_id': event_id,
                                                'delta': delta, 'start_ms': 12, 'end_ms': 98})
        # Same event is dropped; repeated words in a new event remain.
        await self.models[0]['ws'].send_json({'type': 'session.input_transcript.delta', 'event_id': 'e2', 'delta': '你好 ', 'start_ms': 12, 'end_ms': 98})
        await self.models[0]['ws'].send_json({'type': 'session.input_transcript.delta', 'delta': 42})
        await self.until(lambda: self.runtime.transcripts.get(self.runtime.items[first['id']]).revision == 3)
        events = []
        while len(events) < 3:
            packet = await socket.receive_json(timeout=2)
            events.extend(packet['events'])
        self.assertEqual([e['text'] for e in events], [v[2] for v in fragments])
        self.assertEqual([e['role'] for e in events], ['caller', 'caller', 'ai'])
        self.assertTrue(all(e['startMs'] == 12 and e['endMs'] == 98 for e in events))
        await socket.close()
        replay = await self.client.ws_connect(path + '/events?after=1&cacheId=' + initial['cacheId'])
        packet = await replay.receive_json(timeout=2)
        self.assertFalse(packet['reset'])
        self.assertEqual([e['seq'] for e in packet['events']], [2, 3])
        await replay.close()
        pcm = b'\x01\x00' * 480
        await self.audio[PHONE].input.put(pcm)
        await self.models[0]['ws'].send_json({'type': 'session.output_audio.delta', 'delta': base64.b64encode(pcm).decode()})
        self.assertEqual(await asyncio.wait_for(self.models[0]['input'].get(), 2), pcm)
        self.assertEqual(await asyncio.wait_for(self.audio[PHONE].output.get(), 2), pcm)
        await self.client.post('/v1/conversations/' + first['id'] + '/end')
        ended = await (await self.client.get(path)).json()
        self.assertEqual(ended['state'], 'ended')
        self.assertEqual(len(ended['events']), 3)
        second = await self.dial(OTHER, 'vesper')
        self.assertEqual(self.models[1]['session']['audio']['output']['voice'], 'vesper')
        self.assertEqual(self.runtime.store.load('tasks', second['taskId'])['voice'], 'vesper')
        other = await (await self.client.get('/v1/conversations/' + second['id'] + '/transcript')).json()
        self.assertEqual(other['events'], [])
        await self.client.post('/v1/conversations/' + second['id'] + '/end')
        self.assertFalse(self.runtime.sessions)
        await self.runtime.diagnostics.close()
        log = (self.runtime.store.root / 'logs/calls.jsonl').read_text()
        self.assertNotIn('你好', log)
        self.assertNotIn('<img', log)

    async def test_hung_transcript_websocket_and_reader_do_not_block_audio_or_end(self):
        item = await self.dial(PHONE)
        text = self.runtime.sessions[item['id']].text
        # Pause the private reader; kernel and Node buffers must stay bounded.
        text.writer.transport.pause_reading()
        original_send = web.WebSocketResponse.send_json
        entered = asyncio.Event()
        async def hung(ws, value, *args, **kwargs):
            if value.get('type') == 'transcript.update':
                entered.set()
                await asyncio.Future()
            return await original_send(ws, value, *args, **kwargs)
        with patch.object(web.WebSocketResponse, 'send_json', hung):
            socket = await self.client.ws_connect('/v1/conversations/' + item['id'] + '/transcript/events')
            await asyncio.wait_for(entered.wait(), 1)
            for index in range(80):
                await self.models[0]['ws'].send_json({'type': 'session.input_transcript.delta', 'event_id': str(index),
                    'delta': 'x' * 8192, 'start_ms': index, 'end_ms': index + 1})
            pcm = b'\x01\x00' * 480
            await self.audio[PHONE].input.put(pcm)
            await self.models[0]['ws'].send_json({'type': 'session.output_audio.delta', 'delta': base64.b64encode(pcm).decode()})
            self.assertEqual(await asyncio.wait_for(self.models[0]['input'].get(), 2), pcm)
            self.assertEqual(await asyncio.wait_for(self.audio[PHONE].output.get(), 2), pcm)
            text.writer.transport.resume_reading()
            await self.until(lambda: self.runtime.transcripts.get(self.runtime.items[item['id']]).dropped > 0)
            response = await asyncio.wait_for(self.client.post('/v1/conversations/' + item['id'] + '/end'), 3)
            self.assertEqual(response.status, 200)
            self.assertFalse(self.runtime.sessions)
            await socket.close()

    async def test_hung_monitor_and_disk_do_not_block_duplex(self):
        item = await self.dial(PHONE)
        gate = threading.Event()
        entered = threading.Event()
        original_log = PrivateHandler.emit
        def hung_log(handler, record):
            entered.set()
            gate.wait(5)
            original_log(handler, record)
        original_send = web.WebSocketResponse.send_bytes
        hung_send = asyncio.Event()
        async def send(ws, data, *args, **kwargs):
            if len(data) == 961:
                hung_send.set()
                await asyncio.Future()
            return await original_send(ws, data, *args, **kwargs)
        try:
            with patch.object(PrivateHandler, 'emit', hung_log), patch.object(web.WebSocketResponse, 'send_bytes', send):
                monitor = await self.client.ws_connect('/v1/conversations/'+item['id']+'/monitor')
                for _ in range(600):
                    self.runtime.diagnostics.write(item['id'], {'event':'audio_stage','stage':'monitor_drop','frames':1})
                pcm = bytes([7,0])*480
                for _ in range(3):
                    await self.audio[PHONE].input.put(pcm)
                    await self.models[0]['ws'].send_json({'type':'session.output_audio.delta','delta':base64.b64encode(pcm).decode()})
                    self.assertEqual(await asyncio.wait_for(self.models[0]['input'].get(),2),pcm)
                    self.assertEqual(await asyncio.wait_for(self.audio[PHONE].output.get(),2),pcm)
                await asyncio.wait_for(hung_send.wait(),1)
                self.assertFalse(gate.is_set())
                self.assertGreater(self.runtime.diagnostics.dropped,0)
                self.assertLessEqual(self.runtime.diagnostics.pending.qsize(),256)
                await monitor.close()
        finally:
            gate.set()
        await self.client.post('/v1/conversations/'+item['id']+'/end')
        await self.runtime.diagnostics.close()
        records = [json.loads(line) for line in (self.runtime.store.root/'logs/calls.jsonl').read_text().splitlines()]
        stages = [record for record in records if record['event']=='audio_stage']
        self.assertTrue(stages)
        self.assertTrue(all(record['conversationId']==item['id'] for record in records))
        self.assertNotIn(TASK['content'],str(records))

    async def test_managed_playback_burst_preserves_all_frames(self):
        for delay in (0, .02):
            with self.subTest(playback_delay=delay):
                item = await self.dial(PHONE)
                audio, model = self.audio[PHONE], self.models[-1]
                audio.write_delay = delay
                frames = [bytes([index,0])*480 for index in range(30)]
                for pcm in frames:
                    await model['ws'].send_json({'type':'session.output_audio.delta','delta':base64.b64encode(pcm).decode()})
                received = [await asyncio.wait_for(audio.output.get(),2) for _ in frames]
                self.assertEqual(received,frames)
                await audio.input.put(frames[-1])
                self.assertEqual(await asyncio.wait_for(model['input'].get(),2),frames[-1])
                self.assertEqual(self.runtime.items[item['id']]['state'],'active')
                self.assertIsNone(self.runtime.items[item['id']]['error'])
                await self.client.post('/v1/conversations/'+item['id']+'/end')

    async def test_managed_capture_continues_while_playback_waits(self):
        item = await self.dial(PHONE)
        audio, model = self.audio[PHONE], self.models[-1]
        audio.write_gate = asyncio.Event()
        pcm = bytes([9,0])*480
        await model['ws'].send_json({'type':'session.output_audio.delta','delta':base64.b64encode(pcm).decode()})
        await asyncio.wait_for(audio.write_started.wait(),2)
        await audio.input.put(pcm)
        self.assertEqual(await asyncio.wait_for(model['input'].get(),2),pcm)
        self.assertFalse(audio.write_gate.is_set())
        audio.write_gate.set()
        self.assertEqual(await asyncio.wait_for(audio.output.get(),2),pcm)
        await self.client.post('/v1/conversations/'+item['id']+'/end')

    async def test_managed_sustained_playback_overload_fails_and_releases_line(self):
        item = await self.dial(PHONE)
        audio, model = self.audio[PHONE], self.models[-1]
        audio.write_gate = asyncio.Event()
        pcm = bytes([9,0])*480
        # Keep producing while the platform accepts no audio. This must fail,
        # not discard the oldest speech or retain an unbounded history.
        for _ in range(200):
            if model['ws'].closed:
                break
            await model['ws'].send_json({'type':'session.output_audio.delta','delta':base64.b64encode(pcm).decode()})
        await self.until(lambda: item['id'] not in self.runtime.sessions)
        self.assertEqual(self.runtime.items[item['id']]['state'],'failed')
        self.assertEqual(self.runtime.items[item['id']]['error'],'ai_audio_failed')
        self.assertTrue(audio.closed)
        self.assertFalse(audio.write_gate.is_set())
        self.assertTrue(audio.output.empty())
        self.assertIsNone(self.app['state'].lease)
        await self.runtime.diagnostics.close()
        records = [json.loads(line) for line in (self.runtime.store.root/'logs/calls.jsonl').read_text().splitlines()]
        failures = [record.get('failureCode') for record in records if record['event']=='bridge_failure']
        self.assertIn('private_audio_queue_full',failures)
        self.assertNotIn('invalid_model_event',failures)
        peaks = [record['peakDepth'] for record in records if record.get('stage')=='model_output_handoff']
        self.assertTrue(peaks)
        self.assertLessEqual(max(peaks),48000)

    async def test_worker_failure_closes_audio_and_releases_line(self):
        item = await self.dial(PHONE)
        process = self.runtime.sessions[item['id']].process
        process.kill()
        await self.until(lambda: item['id'] not in self.runtime.sessions)
        self.assertTrue(self.audio[PHONE].closed)
        self.assertIsNone(self.app['state'].lease)
        self.assertFalse(self.runtime.sessions)

    async def test_transport_close_caused_by_user_end_is_not_a_failure(self):
        # 真机 e2e：网关结束通话时先停 worker，worker 的私有音频连接随之断开
        # （ConnectionResetError）。这是收尾的结果，不是故障；本通应为 ended。
        item = await self.dial(PHONE)
        session = self.runtime.sessions[item['id']]
        original = session.release_local
        async def release_local():
            session.audio.record_failure(ConnectionResetError(), 'capture_transport')
            return await original()
        session.release_local = release_local
        response = await self.client.post('/v1/conversations/'+item['id']+'/end')
        self.assertEqual(response.status, 200)
        final = self.runtime.items[item['id']]
        self.assertEqual((final['state'], final['error'], final['endReason']), ('ended', None, 'user_end'))
        self.assertEqual(self.runtime.line.state, 'idle')

    async def test_transport_close_after_remote_end_is_not_a_failure(self):
        # 真机 e2e：对方挂断时，手机先报告通话结束，十几毫秒后私有音频连接才断开。
        # 这个断开是对方挂断的结果，本通应为 ended / remote_end。
        item = await self.dial(PHONE)
        session = self.runtime.sessions[item['id']]
        tracker, original = session.tracker, session.tracker.set
        def set_state(state):
            original(state)
            if state == 'ended' and not injected:
                # 与真机相同的顺序：通话结束已记录，收尾尚未开始，传输随即断开。
                injected.append(session.finishing)
                session.audio.record_failure(ConnectionResetError(), 'capture_transport')
        injected = []
        tracker.set = set_state
        self.phones.current[PHONE] = []
        self.phones.changed(PHONE)
        await self.until(lambda: item['id'] not in self.runtime.sessions)
        self.assertEqual(injected, [False])
        final = self.runtime.items[item['id']]
        self.assertEqual((final['state'], final['error'], final['endReason']), ('ended', None, 'remote_end'))
        self.assertEqual(self.runtime.line.state, 'idle')

    async def test_cancel_while_waiting_for_sco_releases_private_transport(self):
        started = asyncio.Event()
        async def pending(audio):
            started.set()
            await asyncio.Future()
        with patch.object(Audio, 'start', pending):
            response = await self.client.post('/v1/conversations',json={'address':PHONE,'number':'+123','task':TASK})
            item = await response.json()
            await started.wait()
            await self.client.post('/v1/conversations/'+item['id']+'/end')
        self.assertTrue(self.audio[PHONE].closed)
        self.assertIsNone(self.app['state'].lease)

    async def test_audio_cleanup_failure_holds_the_line_until_an_explicit_retry_succeeds(self):
        item = await self.dial(PHONE)
        audio = self.audio[PHONE]
        failures = [RuntimeError('simulated SCO close failure')]
        original_close = audio.close
        async def close():
            if failures:
                raise failures.pop()
            await original_close()
        audio.close = close
        response = await self.client.post('/v1/conversations/'+item['id']+'/end')
        self.assertEqual((response.status, (await response.json())['error']['code']), (503, 'audio_cleanup_failed'))
        self.assertEqual(self.runtime.items[item['id']]['state'], 'cleanup_failed')
        self.assertEqual(self.runtime.items[item['id']]['cleanup'], {'local': 'failed', 'phone': 'ended'})
        snapshot = await (await self.client.get('/v1/console')).json()
        self.assertEqual((snapshot['line']['state'], snapshot['line']['reason']), ('recovering', 'audio_cleanup_failed'))
        self.assertFalse(any(d['recovering'] for d in snapshot['devices']))
        rejected = await self.client.post('/v1/conversations', json={'address': OTHER, 'number': '+123', 'task': TASK})
        self.assertEqual((rejected.status, (await rejected.json())['error']['code']), (503, 'line_recovering'))
        self.assertIs(self.app['state'].audio_ws, self.runtime.sessions[item['id']].audio)
        self.assertEqual((await self.client.post('/v1/conversations/'+item['id']+'/end')).status, 200)
        self.assertTrue(audio.closed)
        self.assertEqual(self.runtime.items[item['id']]['state'], 'failed')
        self.assertEqual(self.runtime.line.state, 'idle')
        self.assertFalse(self.runtime.sessions)
        self.assertIsNone(self.app['state'].lease)
        self.assertIsNone(self.app['state'].audio_ws)
        second = await self.dial(OTHER)
        self.assertEqual((await self.client.post('/v1/conversations/'+second['id']+'/end')).status, 200)


class MetricTests(unittest.TestCase):
    def test_fixed_aggregate_arithmetic(self):
        records = []
        now = [0.]
        metrics = AudioMetrics(records.append, lambda: now[0])
        for _ in range(10000): metrics.observe('capture_transport', .002, 960, 3)
        self.assertEqual(len(metrics.values),1)
        self.assertEqual(records,[])
        now[0] = 2
        metrics.flush()
        self.assertEqual(records[0]['frames'],10000)
        self.assertAlmostEqual(records[0]['meanMs'],2)
        self.assertEqual(records[0]['bytes'],9600000)
        self.assertEqual(records[0]['clockDomain'],'python_monotonic')
