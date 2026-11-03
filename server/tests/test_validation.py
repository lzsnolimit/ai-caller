"""Validation must not report success after omitting a Python suite."""
import importlib.util
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('python_validation', ROOT / 'scripts/test-python.py')
validation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validation)
CHECK_SPEC = importlib.util.spec_from_file_location('all_validation', ROOT / 'scripts/check.py')
checks = importlib.util.module_from_spec(CHECK_SPEC)
CHECK_SPEC.loader.exec_module(checks)


class ValidationTests(unittest.TestCase):
    def test_any_check_failure_fails_unified_command(self):
        with patch.object(checks, 'CHECKS', {'fixture pass': [0], 'fixture fail': [7]}), patch.object(checks, 'run', side_effect=lambda command: (command[0], '')), patch('builtins.print'):
            self.assertEqual(checks.main(), 1)

    def test_missing_executable_is_not_success(self):
        with patch.object(checks.subprocess, 'run', side_effect=FileNotFoundError('fixture missing executable')):
            self.assertEqual(checks.run(['fixture-missing-tool'])[0], 1)

    def test_required_python_groups_include_platform_and_launcher(self):
        self.assertEqual(set(validation.GROUPS), {'server/tests', 'server/tests_mac', 'server/common/ai/tests'})

    def test_missing_group_is_not_success(self):
        root = Path(tempfile.gettempdir()) / 'phone-call-gateway/validation-tests' / secrets.token_hex(8)
        with self.assertRaisesRegex(RuntimeError, 'Missing test suite'):
            validation.load_suite(root, ('missing',))

    def test_empty_group_is_not_success(self):
        root = Path(tempfile.gettempdir()) / 'phone-call-gateway/validation-tests' / secrets.token_hex(8)
        (root / 'empty').mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'Empty test suite'):
            validation.load_suite(root, ('empty',))
