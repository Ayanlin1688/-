"""Operational CLIs keep desktop data and verification fixtures isolated."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from core.config_manager import ConfigManager, default_config_path
from core.repository_sync import GitSyncError, RepositorySync
from core.submission_ledger import SubmissionLedger, account_scope, ledger_path
from test_submission_safety import SafetyServer


ROOT = Path(__file__).resolve().parent.parent


class OperationsReviewTests(unittest.TestCase):
    def test_config_check_reads_current_data_directory_without_modifying_it(self):
        for portable in (False, True):
            with self.subTest(portable=portable), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                scripts = root / 'scripts'
                scripts.mkdir()
                script = scripts / 'check_api_config.py'
                shutil.copy2(ROOT / 'scripts' / script.name, script)
                legacy = ConfigManager(root / 'config.json')
                legacy.save_config()
                current = ConfigManager(root / 'appdata' / 'Yanlin' / 'config.json')
                current.config['api']['api_key'] = 'current-fixture-key'
                current.save_config()
                before = current.path.read_bytes()
                result = subprocess.run(
                    [sys.executable, '-X', 'utf8', str(script)], cwd=root,
                    env=dict(os.environ, APPDATA=str(root / 'appdata'), PYTHONPATH=str(ROOT),
                             YANLIN_CONFIG_DIR=str(current.path.parent) if portable else ''),
                    capture_output=True, text=True, encoding='utf-8', timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('api_key_configured: True', result.stdout)
                self.assertNotIn('current-fixture-key', result.stdout + result.stderr)
                self.assertEqual(current.path.read_bytes(), before)

    def test_config_check_migrates_only_current_data_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scripts = root / 'scripts'
            scripts.mkdir()
            script = scripts / 'check_api_config.py'
            shutil.copy2(ROOT / 'scripts' / script.name, script)
            legacy = ConfigManager(root / 'config.json')
            legacy.save_config()
            original_legacy = legacy.path.read_bytes()
            current = root / 'current' / 'config.json'
            current.parent.mkdir()
            current.write_text(json.dumps({'api': {'api_key': 'current-fixture-key'},
                                           'custom_preference': 'keep-me'}), encoding='utf-8')
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', str(script), '--migrate'], cwd=root,
                env=dict(os.environ, YANLIN_CONFIG_DIR=str(current.parent), PYTHONPATH=str(ROOT)),
                capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(legacy.path.read_bytes(), original_legacy)
            migrated = json.loads(current.read_text(encoding='utf-8'))
            self.assertIn('workspace', migrated)
            self.assertEqual(migrated['api']['api_key'], 'current-fixture-key')
            self.assertEqual(migrated['custom_preference'], 'keep-me')

    def test_reference_debug_keeps_unknown_intent_after_temporary_inputs_are_removed(self):
        from core.image_uploader import connection_probe_png
        from scripts import run_reference_debug
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prompt = root / '1.txt'
            prompt.write_text('A car in daylight.', encoding='utf-8')
            images = [root / f'1 ({index}).jpg' for index in (1, 2, 3)]
            for image in images:
                image.write_bytes(connection_probe_png())
            manager = ConfigManager(root / 'config.json')
            manager.config['api']['api_key'] = 'local-fixture-only'
            manager.save_config()
            outcomes = []

            def reserve_without_post(task_manager, config):
                ledger = SubmissionLedger(ledger_path(config))
                outcome, record = ledger.reserve(
                    dict(local_id='reference-fixture', prompt_sha256='same-prompt',
                         signature='same-request', status='submission_unknown'), account_scope(config))
                outcomes.append(outcome)
                ledger.save(record, 'unknown')
                task_manager.record_updated.emit(record)
                raise RuntimeError('stop before provider')

            with patch.object(run_reference_debug, 'PROMPT', prompt), \
                    patch.object(run_reference_debug, 'IMAGES', images), \
                    patch.object(run_reference_debug, 'OUT', root / 'evidence'), \
                    patch.object(run_reference_debug, 'ConfigManager', return_value=manager), \
                    patch.object(run_reference_debug, 'QCoreApplication'), \
                    patch.object(run_reference_debug, 'QTimer'), \
                    patch.object(run_reference_debug.TaskManager, 'start_tasks', new=reserve_without_post), \
                    patch.object(run_reference_debug.signal, 'signal'), \
                    patch.object(sys, 'argv', ['run_reference_debug.py', '--submit']), \
                    contextlib.redirect_stdout(io.StringIO()):
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'stop before provider'):
                        run_reference_debug.main()
            self.assertEqual(outcomes, ['claimed', 'unknown'])
            self.assertTrue((root / 'submissions.sqlite3').is_file())
            records = ConfigManager(manager.path).history_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['status'], 'submission_unknown')

    def test_entrypoint_crash_logs_follow_config_data_directory(self):
        import main
        from core import crash_reporter
        with tempfile.TemporaryDirectory() as folder:
            manager = ConfigManager(Path(folder) / 'data' / 'config.json')
            previous_sys, previous_threading = sys.excepthook, threading.excepthook
            previous_reporter = crash_reporter._reporter
            try:
                with patch.object(main, 'ConfigManager', return_value=manager), \
                        patch.object(main, 'QApplication', side_effect=RuntimeError('stop before GUI')):
                    with self.assertRaisesRegex(RuntimeError, 'stop before GUI'):
                        main.main()
                self.assertEqual(crash_reporter._reporter.log_root, manager.path.parent / 'logs')
            finally:
                sys.excepthook = previous_sys
                threading.excepthook = previous_threading
                crash_reporter._reporter = previous_reporter

    def test_startup_check_overrides_inherited_production_data_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            production = Path(folder) / 'production'
            process = Mock()
            process.communicate.side_effect = [subprocess.TimeoutExpired('main.py', 3), (b'', b'')]
            launched = []

            def launch(*args, **kwargs):
                with patch.dict(os.environ, kwargs['env'], clear=True):
                    resolved = default_config_path()
                self.assertNotEqual(resolved.parent, production)
                self.assertTrue(resolved.parent.is_relative_to(kwargs['cwd']))
                launched.append(resolved)
                return process

            with patch.dict(os.environ, {'YANLIN_CONFIG_DIR': str(production)}), \
                    patch('subprocess.Popen', side_effect=launch), \
                    contextlib.redirect_stdout(io.StringIO()):
                runpy.run_path(str(ROOT / 'scripts' / 'check_startup.py'), run_name='__main__')
            self.assertEqual(len(launched), 1)
            self.assertFalse(production.exists())

    def test_smoke_without_config_option_uses_desktop_data_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = ConfigManager(root / 'desktop' / 'config.json')
            prompts = root / 'prompts'
            prompts.mkdir()
            manager.config['paths'].update(prompts=str(prompts), output=str(root / 'output'))
            manager.config['api']['api_key'] = 'local-fixture-only'
            manager.save_config()
            legacy = root / 'legacy'
            legacy.mkdir()
            ConfigManager(legacy / 'config.json').save_config()
            launch = ('from pathlib import Path; import scripts.run_live_smoke as smoke; '
                      f'smoke.ROOT = Path({str(legacy)!r}); raise SystemExit(smoke.main())')
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', '-c', launch], cwd=ROOT,
                env=dict(os.environ, YANLIN_CONFIG_DIR=str(manager.path.parent)),
                capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((manager.path.parent / 'live-smoke-result.json').exists())

    def test_live_smoke_persists_history_to_desktop_sqlite_store(self):
        with tempfile.TemporaryDirectory() as folder, SafetyServer() as server:
            root = Path(folder)
            prompts = root / 'prompts'
            prompts.mkdir()
            (prompts / '1.txt').write_text('A car under daylight.', encoding='utf-8')
            manager = ConfigManager(root / 'config.json')
            manager.config['migrations'] = {'r6_unattended_defaults': True, 'history_to_sqlite': True}
            manager.config['paths'].update(prompts=str(prompts), output=str(root / 'output'))
            manager.config['api'].update(base_url=server.base, api_key='local-fixture-only')
            manager.config['workspace']['poll_interval'] = 0.01
            manager.config['task_strategy'].update(unmatched_prompt='仍提交文生视频', auto_retry=False)
            manager.save_config()
            original = manager.path.read_bytes()
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', str(ROOT / 'scripts' / 'run_live_smoke.py'),
                 '--config', str(manager.path), '--submit'], cwd=ROOT,
                capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(sum(path == '/videos' for path, _, _ in server.calls), 1)
            report = json.loads((root / 'live-smoke-result.json').read_text(encoding='utf-8'))
            records = ConfigManager(manager.path).history_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['status'], 'completed')
            self.assertEqual(records[0]['local_id'], report['tasks'][0]['local_id'])
            self.assertEqual(manager.path.read_bytes(), original)


class RepositoryProtectionReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'repository'
        self.root.mkdir()
        self.remote = Path(temporary.name) / 'remote.git'
        self.git('init', '--bare', str(self.remote))
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('remote', 'add', 'origin', str(self.remote))
        (self.root / '.gitignore').write_bytes((ROOT / '.gitignore').read_bytes())
        (self.root / 'main.py').write_text('print(1)\n', encoding='utf-8')

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root,
                                       stderr=subprocess.STDOUT).decode('utf-8').strip()

    def test_forced_history_database_staging_is_blocked_before_push(self):
        names = ('history.sqlite3', 'history.sqlite3-wal', 'history.sqlite3-shm', 'HISTORY.SQLITE3')
        for name in names:
            with self.subTest(name=name):
                (self.root / name).write_bytes(b'private task history')
                self.git('add', '-f', name)
                sync = RepositorySync(self.root, expected_origin=str(self.remote), retry_delay=0)
                with self.assertRaises(GitSyncError):
                    sync.sync('fix: protect local history')
                self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main'), '')
                self.git('rm', '--cached', name)
                (self.root / name).unlink()

    def test_sync_command_checks_all_saved_station_credentials(self):
        from scripts import sync_github
        manager = ConfigManager(self.root / 'config.json')
        manager.config['api']['api_key'] = 'active-fixture-key'
        manager.config['stations'] = [
            {'api_key': 'inactive-provider-fixture-key', 'upload_api_key': 'inactive-upload-fixture-key'}]
        manager.save_config()
        for secret in ('inactive-provider-fixture-key', 'inactive-upload-fixture-key'):
            with self.subTest(credential_kind='upload' if 'upload' in secret else 'api'):
                (self.root / 'diagnostic.txt').write_text(secret, encoding='utf-8')

                def local_sync(root, secrets, **kwargs):
                    return RepositorySync(root, secrets, expected_origin=str(self.remote), retry_delay=0, **kwargs)

                with patch.object(sync_github, 'ROOT', self.root), \
                        patch.object(sync_github, 'ConfigManager', return_value=manager), \
                        patch.object(sync_github, 'RepositorySync', side_effect=local_sync), \
                        patch.object(sys, 'argv', ['sync_github.py', '--message', 'fix: protect station keys']), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = sync_github.main()
                self.assertEqual(result, 1)
                self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main'), '')


if __name__ == '__main__':
    unittest.main()
