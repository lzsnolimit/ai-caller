"""Validate project voice samples without a provider or phone connection."""
import hashlib
import io
import json
from pathlib import Path
import re
import wave

SAMPLE_DIRECTORY = Path(__file__).with_name('voice-samples')
MAX_SAMPLE_BYTES = 384044
SAMPLE_TEXT = 'Hello. This is a short sample of my voice.'


def validate_sample(data):
    if not isinstance(data, bytes) or not 44 < len(data) <= MAX_SAMPLE_BYTES:
        raise ValueError('invalid_sample_size')
    try:
        with wave.open(io.BytesIO(data), 'rb') as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 24000, 'NONE'):
                raise ValueError('invalid_sample_format')
            frames = wav.getnframes()
            if not 0 < frames <= 192000 or len(wav.readframes(frames)) != frames * 2:
                raise ValueError('invalid_sample_frames')
            return frames / 24000
    except (wave.Error, EOFError) as error:
        raise ValueError('invalid_sample_wav') from error


def load_samples(directory, voice_ids):
    directory = Path(directory)
    try:
        manifest = json.loads((directory / 'manifest.json').read_text())
        if manifest.get('model') != 'gpt-live-1' or manifest.get('requestedText') != SAMPLE_TEXT:
            return {}
        records = manifest['samples']
        if not isinstance(records, dict):
            return {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    result = {}
    for voice in voice_ids:
        if not isinstance(voice, str) or not re.fullmatch('[a-z]+', voice):
            continue
        # Filenames come only from the fixed catalog, never manifest paths.
        path = directory / (voice + '.wav')
        record = records.get(voice)
        if not isinstance(record, dict):
            continue
        try:
            if path.is_symlink() or path.stat().st_size > MAX_SAMPLE_BYTES:
                continue
            data = path.read_bytes()
            duration = validate_sample(data)
            if hashlib.sha256(data).hexdigest() != record.get('sha256'):
                continue
            result[voice] = {'data': data, 'durationSeconds': duration, 'sha256': record['sha256']}
        except (OSError, ValueError):
            continue
    return result
