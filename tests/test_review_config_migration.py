"""Interrupted data migrations must retain and recover the submission ledger."""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core.config_manager import ConfigManager


class InterruptedMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.legacy = root / 'legacy'
        self.legacy.mkdir()
        self.target = root / 'appdata' / 'Yanlin'
        self.target.mkdir(parents=True)
        self.legacy_config = self.legacy / 'config.json'
        self.legacy_config.write_text(json.dumps({'workspace': {'duration': 12}}), encoding='utf-8')
        self.create_ledger(self.legacy / 'submissions.sqlite3', 'original')
        legacy_patch = patch('core.config_manager.LEGACY_CONFIG', self.legacy_config)
        env_patch = patch.dict(os.environ, {'APPDATA': str(root / 'appdata'), 'YANLIN_CONFIG_DIR': ''})
        legacy_patch.start(); self.addCleanup(legacy_patch.stop)
        env_patch.start(); self.addCleanup(env_patch.stop)

    @staticmethod
    def create_ledger(path, marker):
        connection = sqlite3.connect(str(path))
        try:
            connection.execute('CREATE TABLE marker(value TEXT)')
            connection.execute('INSERT INTO marker VALUES (?)', (marker,))
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def marker(path):
        connection = sqlite3.connect(str(path))
        try:
            return connection.execute('SELECT value FROM marker').fetchone()[0]
        finally:
            connection.close()

    def test_existing_config_does_not_prevent_missing_ledger_migration(self):
        target_config = self.target / 'config.json'
        target_config.write_text(json.dumps({'workspace': {'duration': 16}}), encoding='utf-8')
        manager = ConfigManager(migrate=True)
        self.assertTrue((self.target / 'submissions.sqlite3').exists())
        self.assertEqual(self.marker(self.target / 'submissions.sqlite3'), 'original')
        self.assertEqual(manager.load_config()['workspace']['duration'], 16)

    def test_failed_ledger_copy_keeps_legacy_configuration_active(self):
        with patch('core.config_manager._copy_sqlite', side_effect=PermissionError('fixture failure')):
            manager = ConfigManager(migrate=True)
        self.assertEqual(manager.path, self.legacy_config)
        self.assertFalse((self.target / 'config.json').exists())
        recovered = ConfigManager(migrate=True)
        self.assertEqual(recovered.path, self.target / 'config.json')
        self.assertEqual(self.marker(self.target / 'submissions.sqlite3'), 'original')

    def test_incomplete_sqlite_copy_is_not_published(self):
        def interrupted(source, destination):
            destination.write_bytes(b'incomplete sqlite copy')
            raise OSError('fixture interrupted')

        with patch('core.config_manager._copy_sqlite', side_effect=interrupted):
            ConfigManager(migrate=True)
        self.assertFalse((self.target / 'submissions.sqlite3').exists())
        self.assertEqual(list(self.target.glob('.migrate-*')), [])
        ConfigManager(migrate=True)
        self.assertEqual(self.marker(self.target / 'submissions.sqlite3'), 'original')

    def test_existing_destination_ledger_is_never_overwritten(self):
        self.create_ledger(self.target / 'submissions.sqlite3', 'destination')
        ConfigManager(migrate=True)
        self.assertEqual(self.marker(self.target / 'submissions.sqlite3'), 'destination')

    def test_concurrent_destination_ledger_is_never_overwritten(self):
        destination = self.target / 'submissions.sqlite3'
        publishers = {name: getattr(os, name) for name in ('replace', 'rename', 'link')}

        def concurrent_publish(publish):
            def run(source, target):
                if Path(target) == destination and not destination.exists():
                    self.create_ledger(destination, 'concurrent')
                return publish(source, target)
            return run

        with ExitStack() as stack:
            for name, publish in publishers.items():
                stack.enter_context(patch(f'core.config_manager.os.{name}',
                                          side_effect=concurrent_publish(publish)))
            ConfigManager(migrate=True)
        self.assertEqual(self.marker(destination), 'concurrent')


class LegacyDirectoryMigrationTests(unittest.TestCase):
    def test_legacy_directories_survive_other_migration_checkpoints(self):
        for migration in ({'task_strategy': {'max_concurrency': 1}},
                          {'history': [{'local_id': 'old-task', 'status': 'completed'}]}):
            with self.subTest(migration=migration), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'config.json'
                raw = dict(prompts_dir='D:/legacy/prompts', images_dir='D:/legacy/images',
                           output_dir='D:/legacy/output', **migration)
                path.write_text(json.dumps(raw), encoding='utf-8')
                loaded = ConfigManager(path).load_config()
                self.assertEqual(loaded['paths'], {'prompts': 'D:/legacy/prompts',
                                                  'images': 'D:/legacy/images',
                                                  'output': 'D:/legacy/output'})
                restored = ConfigManager(path).load_config()
                self.assertEqual(restored['paths'], loaded['paths'])


if __name__ == '__main__':
    unittest.main()
