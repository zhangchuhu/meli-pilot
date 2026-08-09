from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from provider_imagegen.config import DEFAULT_BASE_URL, load_provider_config


class ProviderConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temp_dir.name)
        (self.codex_home / "auth.json").write_text(
            '{"OPENAI_API_KEY": "test-key"}', encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_defaults_base_url_when_config_is_missing(self) -> None:
        provider = load_provider_config(self.codex_home)

        self.assertEqual(provider.base_url, DEFAULT_BASE_URL)

    def test_defaults_base_url_when_provider_has_no_base_url(self) -> None:
        (self.codex_home / "config.toml").write_text(
            'model_provider = "custom"\n[model_providers.custom]\nname = "Custom"\n',
            encoding="utf-8",
        )

        provider = load_provider_config(self.codex_home)

        self.assertEqual(provider.base_url, DEFAULT_BASE_URL)

    def test_configured_base_url_takes_precedence(self) -> None:
        (self.codex_home / "config.toml").write_text(
            'model_provider = "custom"\n'
            '[model_providers.custom]\n'
            'base_url = "https://example.com/v1/"\n',
            encoding="utf-8",
        )

        provider = load_provider_config(self.codex_home)

        self.assertEqual(provider.base_url, "https://example.com/v1")

    def test_override_takes_precedence(self) -> None:
        provider = load_provider_config(
            self.codex_home, base_url_override="https://override.example/v1/"
        )

        self.assertEqual(provider.base_url, "https://override.example/v1")


if __name__ == "__main__":
    unittest.main()
