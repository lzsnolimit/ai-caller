"""Verify launcher packaging without network requests or real credentials."""

import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch


CLIENT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("live_launcher", CLIENT / "run-gpt-live.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class Executed(Exception):
    pass


class LauncherTests(unittest.TestCase):
    def launch(self, args, config, env):
        with (
            patch.object(launcher.sys, "argv", ["run-gpt-live.py", *args]),
            patch.dict(os.environ, env, clear=True),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "open", return_value=io.BytesIO(config.encode())),
            patch.object(launcher.os, "chdir") as change_dir,
            patch.object(launcher.os, "execve", side_effect=Executed) as execute,
        ):
            with self.assertRaises(Executed):
                launcher.main()
            change_dir.assert_called_once_with(CLIENT)
            path, argv, passed_env = execute.call_args.args
            self.assertEqual(path, str(CLIENT / "node_modules/.bin/tsx"))
            self.assertEqual(argv, ["tsx", "src/index.ts", *args])
            return passed_env

    def test_openai_config_and_gateway_argument(self):
        env = self.launch(
            ["--gateway", "http://raspberrypi.local:8765", "--address", "AA:BB:CC:DD:EE:FF"],
            'model = "test-model"\n',
            {"OPENAI_API_KEY": "not-a-real-key"},
        )
        self.assertEqual(env["OPENAI_LIVE_URL"], "wss://api.openai.com/v1/live/sessions")
        self.assertEqual(env["OPENAI_BACKEND_MODEL"], "gpt-6-luna")
        self.assertNotIn("OPENAI_LIVE_AUTH_HEADER", env)

    def test_azure_config(self):
        env = self.launch(
            ["--address", "AA:BB:CC:DD:EE:FF"],
            'model_provider = "azure"\nmodel = "test-deployment"\n'
            '[model_providers.azure]\nenv_key = "TEST_AZURE_KEY"\n'
            'base_url = "https://example.invalid/openai/v1/"\n',
            {"TEST_AZURE_KEY": "not-a-real-key"},
        )
        self.assertEqual(env["OPENAI_API_KEY"], "not-a-real-key")
        self.assertEqual(env["OPENAI_LIVE_AUTH_HEADER"], "api-key")
        self.assertEqual(env["OPENAI_LIVE_URL"], "wss://example.invalid/openai/v1/live/sessions")
        self.assertEqual(env["OPENAI_BACKEND_MODEL"], "gpt-6-luna")

    def test_phone_backend_override_is_independent_of_codex_model(self):
        env = self.launch(
            ["--address", "AA:BB:CC:DD:EE:FF"],
            'model = "unrelated-coding-model"\nmodel_reasoning_effort = "high"\n',
            {"OPENAI_API_KEY": "not-a-real-key", "OPENAI_BACKEND_MODEL": " custom-phone-model "},
        )
        self.assertEqual(env["OPENAI_BACKEND_MODEL"], "custom-phone-model")

    def test_empty_phone_backend_uses_luna(self):
        env = self.launch(
            ["--address", "AA:BB:CC:DD:EE:FF"],
            'model = "unrelated-coding-model"\n',
            {"OPENAI_API_KEY": "not-a-real-key", "OPENAI_BACKEND_MODEL": "   "},
        )
        self.assertEqual(env["OPENAI_BACKEND_MODEL"], "gpt-6-luna")

    def test_status_requires_no_config_or_key(self):
        self.launch(["--status"], "invalid TOML", {})

    def test_missing_install_reports_action(self):
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(SystemExit, "Run npm ci"):
                launcher.main()


if __name__ == "__main__":
    unittest.main()
