"""Bounded transcript retention; no phone or cloud operations."""
import unittest
from server.common.transcripts import TranscriptBuffer, Transcripts


class TranscriptTests(unittest.TestCase):
    def test_retention_resume_gaps_and_event_validation(self):
        buffer = TranscriptBuffer()
        for seq in range(1, 601):
            buffer.accept({'type': 'transcript.delta', 'sequence': seq, 'role': 'caller', 'eventId': str(seq),
                           'text': ' 重复 ' * 20, 'startMs': seq, 'endMs': seq + 1})
        value = buffer.snapshot('one', 'ended', 1, buffer.cache_id)
        self.assertTrue(value['gap'])
        self.assertEqual(len(value['events']), 512)
        self.assertLessEqual(buffer.chars, buffer.MAX_CHARS)
        self.assertTrue(buffer.snapshot('one', 'ended', 900, 'old')['reset'])
        buffer.accept({'type': 'transcript.status', 'sequence': 602, 'dropped': 2, 'invalid': 0})
        self.assertEqual(buffer.dropped, 2)
        buffer.accept({'type': 'transcript.delta', 'sequence': 603, 'role': 'caller', 'eventId': '603',
                       'text': 'bad', 'startMs': float('nan'), 'endMs': 1})
        self.assertEqual(buffer.invalid, 1)

    def test_global_cache_is_bounded_and_eviction_is_visible(self):
        cache = Transcripts()
        old = {'id': 'old'}
        original = cache.get(old)
        for i in range(100):
            cache.get({'id': str(i)})
        self.assertEqual(len(cache.buffers), cache.MAX_CONVERSATIONS)
        restored = cache.get(old)
        self.assertNotEqual(original.cache_id, restored.cache_id)
        self.assertTrue(restored.snapshot('old', 'ended')['gap'])
