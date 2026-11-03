"""Save missing catalog previews once; existing verified samples need no request."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.common.voice_samples import SAMPLE_DIRECTORY, MAX_SAMPLE_BYTES, load_samples, validate_sample
from server.common.voices import CATALOG, SAMPLE_TEXT, VOICE_IDS

SCRATCH = Path.home() / '.cache/phone-call-gateway/voice-sample-writes'


def atomic_write(path, data):
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=SCRATCH)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def generate(server, directory, selected, report_path):
    parsed = urllib.parse.urlsplit(server)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('plain_server_origin_required')
    if any(voice not in VOICE_IDS for voice in selected):
        raise ValueError('unknown_voice')
    server = server.rstrip('/')
    directory = Path(directory)
    existing = load_samples(directory, VOICE_IDS)
    try:
        manifest = json.loads((directory / 'manifest.json').read_text())
    except (OSError, ValueError):
        manifest = {'model': 'gpt-live-1', 'requestedText': SAMPLE_TEXT,
                    'format': {'rate': 24000, 'channels': 1, 'sampleBytes': 2, 'maxSeconds': 8}, 'samples': {}}
    results = []
    # The preview API has one generation slot. Do not retry failed voices here.
    for voice in selected:
        if voice in existing:
            result = {'voice': voice, 'status': 'existing', 'sha256': existing[voice]['sha256']}
        else:
            try:
                request = urllib.request.Request(server + '/v1/voices/' + voice + '/preview', method='POST')
                with urllib.request.urlopen(request, timeout=25) as response:
                    data = response.read(MAX_SAMPLE_BYTES + 1)
                duration = validate_sample(data)
                record = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'durationSeconds': duration,
                          'source': 'configured_server_live_preview', 'capturedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          'spokenTextVerified': False}
                atomic_write(directory / (voice + '.wav'), data)
                manifest['samples'][voice] = record
                atomic_write(directory / 'manifest.json', (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode())
                result = {'voice': voice, 'status': 'saved', **record}
            except urllib.error.HTTPError as error:
                try:
                    code = json.loads(error.read(4096)).get('error', {}).get('code', 'http_error')
                except (ValueError, AttributeError):
                    code = 'http_error'
                result = {'voice': voice, 'status': 'failed', 'httpStatus': error.code, 'code': code}
            except (OSError, ValueError) as error:
                result = {'voice': voice, 'status': 'failed', 'code': type(error).__name__}
        results.append(result)
        atomic_write(Path(report_path), (json.dumps(results, ensure_ascii=False, indent=2) + '\n').encode())
        print(json.dumps(result), flush=True)
    return 1 if any(item['status'] == 'failed' for item in results) else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:18765')
    parser.add_argument('--directory', type=Path, default=SAMPLE_DIRECTORY)
    parser.add_argument('--voices', nargs='+', choices=sorted(VOICE_IDS))
    parser.add_argument('--report', type=Path, default=Path.home() / '.cache/phone-call-gateway/voice-sample-generation.json')
    args = parser.parse_args()
    raise SystemExit(generate(args.server, args.directory, args.voices or [voice['id'] for voice in CATALOG['voices']], args.report))
