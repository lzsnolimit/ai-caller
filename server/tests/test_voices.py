"""Preview cancellation, failure and cache bounds without external services."""
import asyncio
import tempfile
from pathlib import Path
import secrets
import unittest
from unittest.mock import AsyncMock, patch
from server.common.contracts import GatewayError
from server.common.voices import VoicePreviews

class VoicePreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(tempfile.gettempdir()) / 'phone-call-gateway/sample-tests' / secrets.token_hex(8)
        self.env = patch.dict('os.environ', {'PHONE_VOICE_SAMPLE_DIR': str(path)})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_cancelled_listener_does_not_cancel_shared_generation(self):
        preview = VoicePreviews()
        gate = asyncio.Event()
        async def generate(voice):
            await gate.wait()
            return b'fixture'
        with patch.object(preview,'synthesize',side_effect=generate) as synthesize:
            first = asyncio.create_task(preview.get('marin'))
            second = asyncio.create_task(preview.get('marin'))
            await asyncio.sleep(.01)
            first.cancel()
            await asyncio.gather(first,return_exceptions=True)
            gate.set()
            self.assertEqual(await second,b'fixture')
            self.assertEqual(synthesize.call_count,1)
        await preview.close()

    async def test_provider_error_has_no_fallback_and_cools_down(self):
        preview = VoicePreviews()
        with patch.object(preview,'synthesize',new=AsyncMock(side_effect=GatewayError('voice_preview_provider_rejected',502))) as synthesize:
            with self.assertRaisesRegex(GatewayError,'voice_preview_provider_rejected'):
                await preview.get('quartz')
            with self.assertRaisesRegex(GatewayError,'voice_preview_retry_later'):
                await preview.get('quartz')
            self.assertEqual(synthesize.call_count,1)
            self.assertFalse(preview.cache)
            self.assertFalse(preview.pending)
        await preview.close()

    async def test_shutdown_cancels_generation(self):
        preview = VoicePreviews()
        with patch.object(preview,'synthesize',new=AsyncMock(side_effect=lambda voice: None)):
            async def never(voice):
                await asyncio.Future()
            preview.synthesize = never
            task = asyncio.create_task(preview.get('marin'))
            await asyncio.sleep(.01)
            await asyncio.wait_for(preview.close(),1)
            result = await asyncio.gather(task,return_exceptions=True)
            self.assertIsInstance(result[0],asyncio.CancelledError)
            self.assertFalse(preview.pending)

    async def test_generation_timeout_is_bounded(self):
        preview = VoicePreviews()
        async def never(voice):
            await asyncio.Future()
        with patch.object(preview,'synthesize',side_effect=never), patch('server.common.voices.PREVIEW_TIMEOUT',.02):
            with self.assertRaisesRegex(GatewayError,'voice_preview_unavailable'):
                await asyncio.wait_for(preview.get('marin'),1)
        self.assertFalse(preview.pending)
        self.assertFalse(preview.cache)
        await preview.close()
