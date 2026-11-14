import os
import stat
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from server.common.call_session import CallSession
from server.common.recording import CallRecorder, recording_enabled


def read_wav(path):
    with wave.open(str(path), 'rb') as source:
        channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        frames = source.readframes(source.getnframes())
    return channels, width, rate, frames


class RecordingTests(unittest.TestCase):
    def test_disabled_unless_the_variable_is_exactly_one(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PHONE_RECORD_CALLS', None)
            self.assertFalse(recording_enabled())
        with mock.patch.dict(os.environ, {'PHONE_RECORD_CALLS': 'true'}):
            self.assertFalse(recording_enabled())
        with mock.patch.dict(os.environ, {'PHONE_RECORD_CALLS': '1'}):
            self.assertTrue(recording_enabled())

    def test_stereo_file_aligns_phone_and_ai_and_is_private(self):
        # Both channels use one timestamp. Thread startup time is not part of this test.
        with tempfile.TemporaryDirectory() as root, mock.patch('server.common.recording.time.monotonic', return_value=5000.0):
            failures = []
            recorder = CallRecorder(root, 'a' * 32, failures.append, max_lead=4)
            recorder.write(0, b'\x01\x00\x02\x00')
            recorder.write(1, b'\x03\x00\x04\x00\x05\x00')
            self.assertEqual(recorder.close(), 0)
            path = Path(root) / ('a' * 32) / 'call.wav'
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(read_wav(path), (2, 2, 24000, b'\x01\x00\x03\x00\x02\x00\x04\x00\x00\x00\x05\x00'))
            self.assertEqual(failures, [])

    def test_a_quiet_gap_stays_on_the_clock_instead_of_shifting_the_later_side(self):
        with tempfile.TemporaryDirectory() as root:
            recorder = CallRecorder(root, 'e' * 32, lambda error: None)
            start = 5000.0
            recorder.enqueue(0, b'\x01\x00\x01\x00', start)
            recorder.enqueue(1, b'\x02\x00\x02\x00', start + 2 / 24000)
            self.assertEqual(recorder.close(), 0)
            frames = read_wav(Path(root) / ('e' * 32) / 'call.wav')[3]
            self.assertEqual(frames, b'\x01\x00\x00\x00\x01\x00\x00\x00\x00\x00\x02\x00\x00\x00\x02\x00')

    def test_no_file_when_the_call_has_no_audio(self):
        with tempfile.TemporaryDirectory() as root:
            recorder = CallRecorder(root, 'b' * 32, lambda error: None)
            self.assertEqual(recorder.close(), 0)
            self.assertFalse((Path(root) / ('b' * 32) / 'call.wav').exists())

    def test_a_full_queue_drops_frames_and_a_disk_error_does_not_raise(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(CallRecorder, 'run', lambda self: None):
            recorder = CallRecorder(root, 'c' * 32, lambda error: None, max_queued=4)
            recorder.write(0, b'\x01\x00\x02\x00')
            recorder.write(0, b'\x03\x00\x04\x00')
            self.assertEqual(recorder.dropped, 1)
            recorder.close()
        with tempfile.TemporaryDirectory() as root:
            blocker = Path(root) / ('d' * 32)
            blocker.write_text('not a directory')
            failures = []
            recorder = CallRecorder(root, 'd' * 32, failures.append)
            recorder.write(0, b'\x01\x00')
            recorder.close()
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], OSError)

    def test_publish_copies_a_frame_without_waiting(self):
        seen = []
        session = CallSession.__new__(CallSession)
        session.item = {'inputBytes': 0, 'outputBytes': 0}
        session.monitors = set()
        session.audio = None
        session.recorder = type('Recorder', (), {'write': lambda self, direction, data: seen.append((direction, data))})()
        session.publish(0, b'\x01\x00')
        session.publish(1, b'\x02\x00')
        self.assertEqual(seen, [(0, b'\x01\x00'), (1, b'\x02\x00')])
        self.assertEqual((session.item['inputBytes'], session.item['outputBytes']), (2, 2))


if __name__ == '__main__':
    unittest.main()
