"""Project sample validation, offline API replay and explicit asset generation."""
import asyncio
import tempfile
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import secrets
import unittest
from unittest.mock import patch
import wave

from aiohttp.test_utils import TestClient, TestServer
from server.common.api import create_app
from server.common.storage import Store
from server.common.voice_samples import SAMPLE_DIRECTORY, SAMPLE_TEXT, load_samples, validate_sample
from server.common.voices import VOICE_IDS
from test_console import Phones, REMOVED_VOICES


def sample():
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
        wav.writeframes(bytes(960))
    return output.getvalue()


def bundle(directory, data):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'quartz.wav').write_bytes(data)
    (directory / 'manifest.json').write_text(json.dumps({'model': 'gpt-live-1', 'requestedText': SAMPLE_TEXT,
        'samples': {'quartz': {'sha256': hashlib.sha256(data).hexdigest()}}}))


class VoiceSampleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / 'phone-call-gateway/sample-tests' / secrets.token_hex(8)
        self.root.mkdir(parents=True)

    async def test_bundled_preview_works_in_fresh_apps_without_provider_or_phone(self):
        data = sample(); directory = self.root / 'samples'; bundle(directory, data)
        for number in range(2):
            phones = Phones()
            with patch.dict('os.environ', {'PHONE_VOICE_SAMPLE_DIR': str(directory), 'OPENAI_API_KEY': '', 'OPENAI_LIVE_URL': ''}), \
                 patch('server.common.voices.provider_settings', side_effect=AssertionError('provider must not be used')):
                app = create_app(phones, managed=True, store=Store(self.root / str(number)))
                client = TestClient(TestServer(app)); await client.start_server()
                try:
                    response = await client.post('/v1/voices/quartz/preview')
                    self.assertEqual(response.status, 200); self.assertEqual(await response.read(), data)
                    catalog = await (await client.get('/v1/voices')).json()
                    voice = next(v for v in catalog['voices'] if v['id'] == 'quartz')
                    self.assertEqual(voice['providerVerification'], 'bundled_sample')
                    self.assertEqual(voice['sample']['state'], 'bundled')
                    self.assertFalse(voice['sample']['mayIncurCost'])
                    self.assertEqual(phones.dials, []); self.assertFalse(app['conversations'].items)
                    self.assertFalse(app['conversations'].sessions)
                    self.assertFalse(app['voice_previews'].pending)
                finally:
                    await client.close()

    def test_invalid_truncated_oversized_hash_and_path_samples_are_not_available(self):
        valid = sample()
        self.assertEqual(validate_sample(valid), .02)
        for index, data in enumerate((b'', valid[:-2], bytes(384045), b'not wav')):
            directory = self.root / str(index); bundle(directory, data)
            self.assertEqual(load_samples(directory, VOICE_IDS), {})
        directory = self.root / 'bad-hash'; bundle(directory, valid)
        (directory / 'quartz.wav').write_bytes(valid[:-2] + b'12')
        self.assertEqual(load_samples(directory, VOICE_IDS), {})
        directory = self.root / 'symlink'; directory.mkdir()
        (self.root / 'outside.wav').write_bytes(valid)
        (directory / 'quartz.wav').symlink_to(self.root / 'outside.wav')
        (directory / 'manifest.json').write_text(json.dumps({'model': 'gpt-live-1', 'requestedText': SAMPLE_TEXT,
            'samples': {'quartz': {'sha256': hashlib.sha256(valid).hexdigest()}, '../outside': {'sha256': hashlib.sha256(valid).hexdigest()}}}))
        self.assertEqual(load_samples(directory, [*VOICE_IDS, '../outside']), {})

    async def test_all_supported_project_samples_replay_offline_in_fresh_apps(self):
        manifest = json.loads((SAMPLE_DIRECTORY / 'manifest.json').read_text())
        self.assertEqual(len(VOICE_IDS), 13)
        self.assertEqual(set(manifest['samples']), VOICE_IDS)
        self.assertEqual({path.stem for path in SAMPLE_DIRECTORY.glob('*.wav')}, VOICE_IDS)
        self.assertEqual(set(load_samples(SAMPLE_DIRECTORY, VOICE_IDS)), VOICE_IDS)
        with patch.dict('os.environ', {'PHONE_VOICE_SAMPLE_DIR': str(SAMPLE_DIRECTORY), 'OPENAI_API_KEY': '', 'OPENAI_LIVE_URL': ''}), \
             patch('server.common.voices.provider_settings', side_effect=AssertionError('no provider')) as settings, \
             patch('server.common.voices.VoicePreviews.synthesize', side_effect=AssertionError('no generation')) as generate:
            for number in range(2):
                phones = Phones()
                app = create_app(phones, managed=True, store=Store(self.root / str(number)))
                client = TestClient(TestServer(app)); await client.start_server()
                try:
                    catalog = await (await client.get('/v1/voices')).json()
                    self.assertEqual({v['id'] for v in catalog['voices']}, VOICE_IDS)
                    self.assertEqual(catalog['defaultVoice'], 'marin')
                    self.assertTrue(all(v['sample']['state'] == 'bundled' and not v['sample']['mayIncurCost'] for v in catalog['voices']))
                    async def replay(voice):
                        response = await client.post(f'/v1/voices/{voice}/preview')
                        self.assertEqual(response.status, 200)
                        self.assertEqual(hashlib.sha256(await response.read()).hexdigest(), manifest['samples'][voice]['sha256'])
                    await asyncio.gather(*(replay(voice) for voice in VOICE_IDS))
                    self.assertFalse(phones.dials)
                    self.assertFalse(app['conversations'].items)
                    self.assertFalse(app['conversations'].sessions)
                    self.assertFalse(app['voice_previews'].pending)
                finally:
                    await client.close()
            settings.assert_not_called()
            generate.assert_not_called()

    def test_generator_saves_once_and_skips_verified_asset_without_network(self):
        path = Path(__file__).resolve().parents[2] / 'scripts/generate-voice-samples.py'
        spec = importlib.util.spec_from_file_location('sample_generator_test', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        directory, report = self.root / 'generated', self.root / 'report.json'
        data = sample()
        with patch.object(module.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as request:
            self.assertEqual(module.generate('http://127.0.0.1:1', directory, ['quartz'], report), 0)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0].full_url, 'http://127.0.0.1:1/v1/voices/quartz/preview')
        with patch.object(module.urllib.request, 'urlopen', side_effect=AssertionError('no repeat generation')):
            self.assertEqual(module.generate('http://127.0.0.1:1', directory, ['quartz'], report), 0)
            with self.assertRaises(ValueError):
                module.generate('http://127.0.0.1:1', directory, ['../outside'], report)
            for voice in REMOVED_VOICES:
                with self.subTest(voice=voice), self.assertRaisesRegex(ValueError, 'unknown_voice'):
                    module.generate('http://127.0.0.1:1', directory, [voice], report)
        self.assertEqual(load_samples(directory, VOICE_IDS)['quartz']['data'], data)

    def test_generator_rejects_failed_audio_without_saving(self):
        path = Path(__file__).resolve().parents[2] / 'scripts/generate-voice-samples.py'
        spec = importlib.util.spec_from_file_location('sample_generator_invalid', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        directory = self.root / 'bad-generated'
        with patch.object(module.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad')):
            self.assertEqual(module.generate('http://127.0.0.1:1', directory, ['quartz'], self.root / 'report.json'), 1)
        self.assertFalse((directory / 'quartz.wav').exists())
