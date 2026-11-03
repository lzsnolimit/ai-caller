import asyncio
import tempfile
import json
from pathlib import Path
import secrets
import stat
import unittest
from unittest.mock import patch

from server.common.diagnostics import Diagnostics, sanitize


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_only_private_logs_and_flush(self):
        root = Path(tempfile.gettempdir()) / "phone-call-gateway/log-tests" / secrets.token_hex(8)
        log = Diagnostics(root)
        conversation = secrets.token_hex(16)
        log.write(conversation, {"event": "backend_response", "observedBackendModel": "gpt-6-luna",
            "responseEvent": "response.completed", "durationMs": 1250, "workerDroppedLogs": 7,
            "number": "private-number", "task": "private-task", "audio": "private-audio",
            "delta": "private-transcript", "error": "private-key"})
        await log.close()
        raw = (root / "logs/calls.jsonl").read_text()
        record = json.loads(raw)
        self.assertEqual(record["conversationId"], conversation)
        self.assertEqual(record["workerDroppedLogs"], 7)
        self.assertEqual(record["droppedLogs"], 0)
        self.assertEqual(record["observedBackendModel"], "gpt-6-luna")
        self.assertNotIn("private-", raw)
        self.assertEqual(stat.S_IMODE((root / "logs").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((root / "logs/calls.jsonl").stat().st_mode), 0o600)

    async def test_disk_failure_never_fails_call(self):
        root = Path(tempfile.gettempdir()) / "phone-call-gateway/log-tests" / secrets.token_hex(8)
        with patch("server.common.diagnostics.PrivateHandler", side_effect=OSError("private-error")):
            log = Diagnostics(root)
            await asyncio.to_thread(log.thread.join, 1)
            log.write(secrets.token_hex(16), {"event": "worker_start"})
            self.assertTrue(log.failed)
            self.assertGreater(log.dropped, 0)
            await log.close()

    async def test_untrusted_worker_values_do_not_break_sanitizer(self):
        for value in (None, [], {"event": []}, {"event": {}}, {"event": "private"}):
            self.assertIsNone(sanitize(value))
        self.assertEqual(sanitize({"event": "audio_stats", "responseEvent": {}, "direction": [],
            "inputBytes": True, "stage": {}, "observedBackendModels": ["secret", {}, "gpt-6-luna"]}),
            {"event": "audio_stats", "observedBackendModels": ["gpt-6-luna"]})
