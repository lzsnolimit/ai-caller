import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.common.contracts import GatewayError
from server.common.voices import provider_settings


class ProviderConfigTests(unittest.TestCase):
    """The Codex config file is optional: OPENAI_API_KEY alone must be enough."""

    def env(self, home, **values):
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith('OPENAI_') and k != 'CODEX_HOME'}
        return mock.patch.dict(os.environ, {**clean, 'CODEX_HOME': home, **values}, clear=True)

    def test_key_without_codex_config_uses_openai_defaults(self):
        with tempfile.TemporaryDirectory() as home, self.env(home, OPENAI_API_KEY='not-a-real-key'):
            url, headers = provider_settings()
        self.assertEqual(url, 'wss://api.openai.com/v1/live/sessions')
        self.assertEqual(headers, {'Authorization': 'Bearer not-a-real-key'})

    def test_missing_key_is_not_configured(self):
        with tempfile.TemporaryDirectory() as home, self.env(home):
            with self.assertRaises(GatewayError) as caught:
                provider_settings()
        self.assertEqual(caught.exception.code, 'ai_provider_not_configured')

    def test_codex_config_selects_provider_key_and_url(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / 'config.toml').write_text(
                'model_provider = "custom"\n[model_providers.custom]\n'
                'base_url = "https://example.invalid/v1"\nenv_key = "CUSTOM_KEY"\n')
            with self.env(home, CUSTOM_KEY='not-a-real-key'):
                url, headers = provider_settings()
        self.assertEqual(url, 'wss://example.invalid/v1/live/sessions')
        self.assertEqual(headers, {'Authorization': 'Bearer not-a-real-key'})


if __name__ == '__main__':
    unittest.main()
