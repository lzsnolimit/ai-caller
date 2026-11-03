"""One offline validation command for Python, AI and Client syntax."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CHECKS = {
    'Python suites': [sys.executable, str(ROOT / 'scripts/test-python.py')],
    'AI types': ['npm', '--prefix', 'server/common/ai', 'run', 'typecheck'],
    'AI tests': ['npm', '--prefix', 'server/common/ai', 'test'],
    'Client syntax': ['node', '--check', 'client/assets/app.js'],
}


def run(command):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        return result.returncode, result.stdout
    except OSError as error:
        return 1, str(error)


def main():
    failed = False
    with ThreadPoolExecutor(max_workers=20) as pool:
        jobs = {pool.submit(run, command): name for name, command in CHECKS.items()}
        for job in as_completed(jobs):
            code, output = job.result()
            print(f'[{jobs[job]}] {"PASS" if code == 0 else "FAIL"}', flush=True)
            if output:
                print(output.rstrip(), flush=True)
            failed |= code != 0
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
