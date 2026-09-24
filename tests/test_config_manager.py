import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config_manager import ConfigManager, DEFAULT_CONFIG, default_config_path


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


    def test_generation_defaults_follow_the_parameters_that_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'config.json'
            manager = ConfigManager(path)
            manager.load_config()
            manager.update(('defaults', 'duration'), 11)
            self.assertEqual(manager.config['workspace']['duration'], 11)
            manager.update(('workspace', 'poll_interval'), 9)
            self.assertEqual(manager.config['workspace']['poll_interval'], 9)
            self.assertEqual(manager.config['defaults']['poll_interval'], 5)
            path.write_text(json.dumps({'workspace': {'duration': 12, 'model': 'video-v2'}, 'defaults': {'duration': 4, 'model': 'video-v1'}}), encoding='utf-8')
            loaded = ConfigManager(path).load_config()
            self.assertEqual(loaded['workspace']['duration'], 12)
            self.assertEqual(loaded['defaults']['duration'], 4)
            self.assertEqual(loaded['defaults']['model'], 'video-v1')

class DataDirectoryTests(unittest.TestCase):
    def test_default_path_uses_appdata_and_env_override(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {'APPDATA': temp, 'YANLIN_CONFIG_DIR': ''}, clear=False):
                self.assertEqual(default_config_path(), Path(temp) / 'Yanlin' / 'config.json')
            with patch.dict(os.environ, {'YANLIN_CONFIG_DIR': temp}, clear=False):
                self.assertEqual(default_config_path(), Path(temp) / 'config.json')

    def test_first_run_migrates_legacy_files_on_demand(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy_dir = Path(temp) / 'legacy'
            legacy_dir.mkdir()
            (legacy_dir / 'config.json').write_text(json.dumps({'workspace': {'duration': 12}}), encoding='utf-8')
            ledger = sqlite3.connect(str(legacy_dir / 'submissions.sqlite3'))
            ledger.execute('CREATE TABLE marker(x)')
            ledger.commit()
            ledger.close()
            appdata = Path(temp) / 'appdata'
            appdata.mkdir()
            with patch('core.config_manager.LEGACY_CONFIG', legacy_dir / 'config.json'), \
                 patch.dict(os.environ, {'APPDATA': str(appdata), 'YANLIN_CONFIG_DIR': ''}, clear=False):
                manager = ConfigManager(migrate=True)
                self.assertEqual(manager.path, appdata / 'Yanlin' / 'config.json')
                self.assertTrue(manager.path.is_file())
                self.assertTrue((appdata / 'Yanlin' / 'submissions.sqlite3').is_file())
                loaded = manager.load_config()
                self.assertEqual(loaded['workspace']['duration'], 12)

    def test_without_migration_falls_back_to_legacy_location(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy_dir = Path(temp) / 'legacy'
            legacy_dir.mkdir()
            (legacy_dir / 'config.json').write_text('{}', encoding='utf-8')
            appdata = Path(temp) / 'appdata'
            appdata.mkdir()
            with patch('core.config_manager.LEGACY_CONFIG', legacy_dir / 'config.json'), \
                 patch.dict(os.environ, {'APPDATA': str(appdata), 'YANLIN_CONFIG_DIR': ''}, clear=False):
                manager = ConfigManager()
                self.assertEqual(manager.path, legacy_dir / 'config.json')
                self.assertFalse((appdata / 'Yanlin').exists())


if __name__ == "__main__":
    unittest.main()
