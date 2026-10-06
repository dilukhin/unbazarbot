from pathlib import Path
import tempfile
import unittest

from voicebot.config import load_config


class ConfigTemplateTests(unittest.TestCase):
    def test_template_is_closed_and_keeps_three_models(self):
        config = load_config('config.example.yaml')
        self.assertEqual(config.admin_user_ids, set())
        self.assertFalse(config.allow_private_transcription_for_non_admins)
        self.assertEqual(len(config.models), 3)

    def test_alternate_path_is_supported_and_missing_config_is_not_substituted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'custom.yaml'
            path.write_text(Path('config.example.yaml').read_text())
            self.assertEqual(load_config(path).default_model, load_config('config.example.yaml').default_model)
            with self.assertRaises(FileNotFoundError):
                load_config(Path(tmp) / 'missing.yaml')
