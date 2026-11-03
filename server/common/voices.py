"""Supported Live voices and bounded, phone-independent sample generation."""
import asyncio
import base64
import binascii
import io
import json
import os
from pathlib import Path
import time
import tomllib
from urllib.parse import urlsplit, urlunsplit
import wave

import aiohttp

from .contracts import GatewayError
from .voice_samples import SAMPLE_DIRECTORY, SAMPLE_TEXT, load_samples

CATALOG = json.loads((Path(__file__).parent / 'voice-catalog.json').read_text())
VOICE_IDS = frozenset(v['id'] for v in CATALOG['voices'])
MAX_PCM = 8 * 24000 * 2
PREVIEW_TIMEOUT = 20


async def _send_silence(ws):
    # One 20 ms frame of mono PCM16 at 24 kHz advances the Live timeline.
    event = {'type': 'session.input_audio.append', 'audio': base64.b64encode(bytes(960)).decode()}
    loop = asyncio.get_running_loop()
    try:
        while True:
            next_frame = loop.time() + .02
            await ws.send_json(event)
            # Do not queue frames or send a burst to recover lost time.
            await asyncio.sleep(max(0, next_frame - loop.time()))
    except Exception:
        await ws.close()
        raise


def validate_voice(value):
    if not isinstance(value, str) or value not in VOICE_IDS:
        raise GatewayError('invalid_voice')
    return value


def provider_settings():
    """Use the worker's existing provider rules without exposing credentials."""
    key, url = os.environ.get('OPENAI_API_KEY'), os.environ.get('OPENAI_LIVE_URL')
    header = os.environ.get('OPENAI_LIVE_AUTH_HEADER')
    if not (key and url):
        try:
            # Codex configuration is optional; without it, use OpenAI and OPENAI_API_KEY.
            try:
                config = tomllib.loads((Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml').read_text())
            except FileNotFoundError:
                config = {}
            name = config.get('model_provider', 'openai')
            provider = config.get('model_providers', {}).get(name, {})
            key = os.environ.get(provider.get('env_key', 'OPENAI_API_KEY'))
            base = urlsplit(provider.get('base_url', 'https://api.openai.com/v1'))
            if base.scheme != 'https' or base.username or base.password or base.query or base.fragment:
                raise ValueError()
            url = urlunsplit(('wss', base.netloc, base.path.rstrip('/') + '/live/sessions', '', ''))
            header = 'api-key' if name == 'azure' else None
        except (OSError, ValueError):
            raise GatewayError('ai_provider_not_configured', 503) from None
    parsed = urlsplit(url)
    if not key or parsed.scheme != 'wss' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GatewayError('ai_provider_not_configured', 503)
    return url, ({header: key} if header else {'Authorization': 'Bearer ' + key})


class VoicePreviews:
    def __init__(self, samples_dir=None):
        self.cache, self.pending, self.failures = {}, {}, {}
        self.bundled = load_samples(samples_dir if samples_dir is not None else os.environ.get('PHONE_VOICE_SAMPLE_DIR', SAMPLE_DIRECTORY), VOICE_IDS)
        self.closed = False

    def catalog(self):
        # Cache is private process memory; one eight-second WAV per supported voice.
        return {**CATALOG, 'verificationScope': 'sample_provenance', 'sampleText': SAMPLE_TEXT, 'voices': [
            {**voice, 'source': CATALOG['source'],
             'providerVerification': 'bundled_sample' if voice['id'] in self.bundled else ('preview_succeeded' if voice['id'] in self.cache else 'not_verified'),
             'sample': {'available': voice['id'] in self.bundled or voice['id'] in self.cache, 'canGenerate': voice['id'] not in self.bundled,
                        'state': 'bundled' if voice['id'] in self.bundled else ('cached' if voice['id'] in self.cache else 'on_demand'),
                        'source': 'project_asset' if voice['id'] in self.bundled else 'server_live_preview',
                        'durationSeconds': self.bundled.get(voice['id'], {}).get('durationSeconds'),
                        'method': 'POST', 'url': '/v1/voices/' + voice['id'] + '/preview',
                        'mayIncurCost': voice['id'] not in self.bundled and voice['id'] not in self.cache}}
            for voice in CATALOG['voices']]}

    async def get(self, voice):
        voice = validate_voice(voice)
        if self.closed:
            raise GatewayError('server_stopping', 503)
        if voice in self.bundled:
            return self.bundled[voice]['data']
        if voice in self.cache:
            return self.cache[voice]
        if self.failures.get(voice, 0) > time.monotonic():
            raise GatewayError('voice_preview_retry_later', 429)
        task = self.pending.get(voice)
        if task is None:
            # Keep optional generation bounded independently of call/audio work.
            if self.pending:
                raise GatewayError('voice_preview_busy', 429)
            task = self.pending[voice] = asyncio.create_task(self.generate(voice))
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(task)

    async def generate(self, voice):
        try:
            async with asyncio.timeout(PREVIEW_TIMEOUT):
                value = await self.synthesize(voice)
            self.cache[voice] = value
            return value
        except asyncio.CancelledError:
            raise
        except GatewayError:
            self.failures[voice] = time.monotonic() + 30
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError, binascii.Error, KeyError, TypeError):
            self.failures[voice] = time.monotonic() + 30
            raise GatewayError('voice_preview_unavailable', 502) from None
        finally:
            self.pending.pop(voice, None)

    async def synthesize(self, voice):
        url, headers = await asyncio.to_thread(provider_settings)
        pcm = bytearray()
        # No phone adapter, line lease, worker, task text or live-call session is used.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as client:
            async with client.ws_connect(url, headers=headers, max_msg_size=1024 * 1024,
                                         timeout=aiohttp.ClientWSTimeout(ws_close=1)) as ws:
                await ws.send_json({'type': 'session.start', 'session': {
                    'model': 'gpt-live-1', 'store': False,
                    'instructions': 'You are an AI voice sample. Say only the supplied sample sentence once. Then stay silent.',
                    'audio': {'format': {'type': 'audio/pcm', 'rate': 24000}, 'output': {'voice': voice}},
                    'delegation': {'type': 'responses', 'responses': {
                        'model': os.environ.get('OPENAI_BACKEND_MODEL', '').strip() or 'gpt-6-luna'}}}})
                started, closing, sender = False, False, None
                deadline = asyncio.get_running_loop().time() + 17
                try:
                    while True:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise GatewayError('voice_preview_timeout', 504)
                        try:
                            message = await ws.receive(timeout=min(remaining, 1.0) if pcm and not closing else remaining)
                        except asyncio.TimeoutError:
                            if pcm and not closing:
                                if sender is not None:
                                    sender.cancel()
                                    await asyncio.gather(sender, return_exceptions=True)
                                await ws.send_json({'type': 'session.close'})
                                closing = True
                                deadline = min(deadline, asyncio.get_running_loop().time() + 2)
                                continue
                            raise
                        if message.type != aiohttp.WSMsgType.TEXT:
                            raise GatewayError('voice_preview_connection_closed', 502)
                        event = json.loads(message.data)
                        if event['type'] == 'session.started' and not started:
                            started = True
                            sender = asyncio.create_task(_send_silence(ws), name='voice-preview-silence')
                            await ws.send_json({'type': 'session.commentary.append', 'delegation_id': None,
                                                'content': 'Say exactly once: ' + SAMPLE_TEXT})
                        elif event['type'] == 'session.output_audio.delta' and started and not closing:
                            delta = event['delta']
                            if not isinstance(delta, str) or len(delta) > 4 * ((MAX_PCM + 2) // 3):
                                raise GatewayError('voice_preview_audio_limit', 502)
                            chunk = base64.b64decode(delta, validate=True)
                            if not chunk or len(chunk) % 2:
                                raise GatewayError('voice_preview_audio_limit', 502)
                            # Continuous Live PCM can include silence after speech.
                            # Keep complete samples up to the clip cap, then close.
                            pcm.extend(chunk[:MAX_PCM - len(pcm)])
                            if len(pcm) == MAX_PCM:
                                if sender is not None:
                                    sender.cancel()
                                    await asyncio.gather(sender, return_exceptions=True)
                                await ws.send_json({'type': 'session.close'})
                                closing = True
                                deadline = min(deadline, asyncio.get_running_loop().time() + 2)
                        elif event['type'] == 'error':
                            raise GatewayError('voice_preview_provider_rejected', 502)
                        elif event['type'] == 'session.closed':
                            if not pcm or event.get('reason') == 'connection_lost':
                                raise GatewayError('voice_preview_empty', 502)
                            break
                finally:
                    if sender is not None:
                        sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(pcm)
        return output.getvalue()

    async def close(self):
        self.closed = True
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.cache.clear()
