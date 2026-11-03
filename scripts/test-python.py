"""Run every offline Python suite; fail if any suite is missing or empty."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ('server/tests', 'server/tests_mac', 'server/common/ai/tests')
sys.path[:0] = [str(ROOT), str(ROOT / 'server')]


def load_suite(root=ROOT, groups=GROUPS):
    suite = unittest.TestSuite()
    for group in groups:
        folder = root / group
        if not folder.is_dir():
            raise RuntimeError(f'Missing test suite: {group}')
        tests = unittest.TestLoader().discover(str(folder), pattern='test_*.py')
        if tests.countTestCases() == 0:
            raise RuntimeError(f'Empty test suite: {group}')
        suite.addTests(tests)
    return suite


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(load_suite())
    raise SystemExit(0 if result.wasSuccessful() else 1)
