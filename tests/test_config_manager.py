import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config_manager import ConfigManager, DEFAULT_CONFIG


class ConfigManagerTests(unittest.TestCase):
    def test_defaults_include_all_persisted_sections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = ConfigManager(Path(temp_dir) / "config.json").load_config()

        self.assertEqual(set(config), set(DEFAULT_CONFIG))
        self.assertEqual(config["workspace"]["duration"], 8)
        self.assertEqual(config["appearance"]["theme"], "dark")
        self.assertEqual(len(config["model_pool"]["models"]), 3)

    def test_update_and_round_trip_preserves_nested_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            manager = ConfigManager(path)
            manager.load_config()
            manager.update(("paths", "output"), "D:/videos")
            manager.update(("workspace", "duration"), 12)
            manager.save_config()

            loaded = ConfigManager(path).load_config()
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded["paths"]["output"], "D:/videos")
        self.assertEqual(loaded["workspace"]["duration"], 12)
        self.assertEqual(saved["workspace"]["duration"], 12)

    def test_invalid_json_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text("{not-json", encoding="utf-8")
            config = ConfigManager(path).load_config()

        self.assertEqual(config["api"]["base_url"], DEFAULT_CONFIG["api"]["base_url"])

    def test_atomic_failure_keeps_previous_file_and_reports_ui_error(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ConfigManager(Path(temp) / 'config.json')
            manager.save_config()
            original = manager.path.read_bytes()
            errors = []
            manager.error_callback = lambda message, level: errors.append((message, level))
            with patch('core.config_manager.os.replace', side_effect=PermissionError('disk denied')):
                self.assertFalse(manager.update(('workspace', 'duration'), 12))
            self.assertEqual(manager.path.read_bytes(), original)
            self.assertEqual(manager.config['workspace']['duration'], 12)
            self.assertEqual(errors[0][1], 'error')
            self.assertFalse(list(Path(temp).glob('*.tmp')))

    def test_legacy_naming_and_empty_binding_migrate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'config.json'
            path.write_text(json.dumps({'paths': {'prompts': str(root)},
                                        'task_strategy': {'naming_rule': '{提示词名}_{模型}.mp4'},
                                        'matching_order': {'空白.txt': [], '演示.txt': ['demo:image.png']}}), encoding='utf-8')
            manager = ConfigManager(path)
            config = manager.load_config()
            self.assertEqual(config['download_settings']['naming_rule'], '{提示词名}_{模型}.mp4')
            self.assertEqual(config['match_overrides'], {str((root / '空白.txt').resolve()): []})
            manager.update(('task_strategy', 'naming_rule'), '{task_id}.mp4')
            self.assertEqual(ConfigManager(path).load_config()['download_settings']['naming_rule'], '{task_id}.mp4')


if __name__ == "__main__":
    unittest.main()
