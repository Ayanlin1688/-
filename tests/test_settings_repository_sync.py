import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QThread, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from core.config_manager import ConfigManager, DEFAULT_CONFIG
from core.repository_sync import GITHUB_REPOSITORY, RepositorySync, default_repository_root, validate_repository_root
from ui.pages.settings_page import SettingsPage
try:
    from .test_task_manager import wait_until
except ImportError:
    from test_task_manager import wait_until


class SettingsRepositorySyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = (Path(self.temp.name) / '中文 项目').resolve()
        self.root.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Ayanlin1688')
        self.git('config', 'user.email', '329457281+Ayanlin1688@users.noreply.github.com')
        self.git('config', 'core.autocrlf', 'false')
        self.git('remote', 'add', 'origin', GITHUB_REPOSITORY)
        (self.root / '.gitignore').write_bytes((default_repository_root() / '.gitignore').read_bytes())
        (self.root / 'main.py').write_text('print("ready")\n', encoding='utf-8')
        self.config = ConfigManager(Path(self.temp.name) / 'settings' / 'config.json')
        self.log = Mock()
        self.page = SettingsPage(self.config, self.log)
        self.page.resize(1100, 700)
        self.page.show()
        self.page.scroll.ensureWidgetVisible(self.page.sync_button)
        QTest.qWait(20)

    def tearDown(self):
        wait_until(lambda: not self.page.jobs.busy)
        self.page.stop_timers()
        self.page.close()
        self.page.deleteLater()
        QTest.qWait(20)
        self.temp.cleanup()

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.root, capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        return result.stdout.decode('utf-8').strip()

    def select(self, root):
        with patch('ui.pages.settings_page.QFileDialog.getExistingDirectory', return_value=str(root)) as choose:
            self.page.repo_select_button.click()
            choose.assert_called_once()
            self.assertFalse(self.page.sync_button.isEnabled())
            self.assertFalse(self.page.repo_select_button.isEnabled())
            self.assertIn('完成后', self.page.repo_select_button.toolTip())
            wait_until(lambda: not self.page.jobs.busy)

    def assert_sync_rejected(self, message):
        with patch('ui.pages.settings_page.RepositorySync') as service, patch('qfluentwidgets.InfoBar.error') as error:
            self.page.sync_button.click()
            wait_until(lambda: not self.page.jobs.busy)
            service.assert_not_called()
            error.assert_called_once()
            self.assertIn(message, error.call_args.args[1])
        self.assertIn(message, self.page.repo_status_label.text())
        self.assertTrue(self.page.sync_button.isEnabled())
        self.assertTrue(self.page.repo_select_button.isEnabled())

    def test_default_is_empty_and_source_uses_project_root_without_persisting_it(self):
        self.assertEqual(DEFAULT_CONFIG['sync']['repo_root'], '')
        self.assertEqual(self.config.config['sync']['repo_root'], '')
        self.assertEqual(self.page.repo_root_edit.text(), str(default_repository_root()))
        self.assertTrue(self.page.repo_root_edit.isReadOnly())
        self.assertFalse(self.config.path.exists())

    def test_selection_validates_read_only_in_background_and_persists_local_path(self):
        before = self.git('status', '--porcelain')
        threads = []
        def inspect(root, **kwargs):
            threads.append(QThread.currentThread())
            return validate_repository_root(root, **kwargs)
        with patch('ui.pages.settings_page.validate_repository_root', side_effect=inspect), \
                patch.object(RepositorySync, 'sync') as sync:
            self.select(self.root)
            sync.assert_not_called()
        self.assertEqual(self.git('status', '--porcelain'), before)
        self.assertNotEqual(threads[0], self.app.thread())
        self.assertEqual(ConfigManager(self.config.path).load_config()['sync']['repo_root'], str(self.root))
        self.assertIn('目录已就绪：' + str(self.root), self.page.repo_status_label.text())
        self.assertIn('main · origin 已匹配', self.page.repo_status_label.text())

    def test_cancel_folder_selection_preserves_configuration(self):
        before = dict(self.config.config['sync'])
        with patch('ui.pages.settings_page.QFileDialog.getExistingDirectory', return_value=''), \
                patch('ui.pages.settings_page.validate_repository_root') as validate:
            self.page.repo_select_button.click()
            validate.assert_not_called()
        self.assertEqual(self.config.config['sync'], before)
        self.assertFalse(self.page.jobs.busy)
        self.assertTrue(self.page.repo_select_button.isEnabled())

    def test_non_git_directory_is_rejected(self):
        directory = Path(self.temp.name) / 'not-git'
        directory.mkdir()
        self.select(directory)
        self.assert_sync_rejected('不是可用的Git工作区')

    def test_nested_working_directory_is_rejected_as_not_git_root(self):
        directory = self.root / 'child'
        directory.mkdir()
        self.select(directory)
        self.assert_sync_rejected('不是项目Git根目录')

    def test_non_main_branch_is_rejected(self):
        self.git('symbolic-ref', 'HEAD', 'refs/heads/develop')
        self.select(self.root)
        self.assert_sync_rejected('请先切换到main分支')

    def test_other_origin_including_legacy_is_rejected_without_rewriting_remote(self):
        for origin in ('https://github.com/another/project.git',
                       'https://github.com/admin11044/StoryboardVideoStudio.git'):
            with self.subTest(origin=origin):
                self.git('remote', 'set-url', 'origin', origin)
                self.select(self.root)
                self.assert_sync_rejected('拒绝同步，不会推送')
                self.assertEqual(self.git('remote', 'get-url', 'origin'), origin)
                self.assertEqual(self.git('remote'), 'origin')

    def test_mismatched_push_url_is_also_rejected(self):
        self.git('remote', 'set-url', '--push', 'origin', 'https://github.com/another/project.git')
        self.select(self.root)
        self.assert_sync_rejected('包括推送地址')
        self.assertEqual(self.git('remote', 'get-url', 'origin'), GITHUB_REPOSITORY)

    def test_missing_origin_is_rejected(self):
        self.git('remote', 'remove', 'origin')
        self.select(self.root)
        self.assert_sync_rejected('无法读取授权仓库地址')
        self.assertEqual(self.git('remote'), '')

    def test_invalid_branch_and_origin_are_reported_separately(self):
        self.git('symbolic-ref', 'HEAD', 'refs/heads/develop')
        self.git('remote', 'set-url', 'origin', 'https://github.com/another/project.git')
        self.select(self.root)
        text = self.page.repo_status_label.text()
        self.assertIn('分支：', text)
        self.assertIn('\norigin：', text)
        self.assertEqual(self.page.repo_status_label.lightColor.name(), '#f56c6c')
        self.assertEqual(self.page.repo_status_label.darkColor.name(), '#f56c6c')

    def test_frozen_empty_root_prompts_before_any_git_or_sync(self):
        self.page.stop_timers()
        self.page.close()
        self.page.deleteLater()
        QTest.qWait(20)
        with patch('ui.pages.settings_page.sys.frozen', True, create=True), \
                patch('ui.pages.settings_page.validate_repository_root') as validate, \
                patch('ui.pages.settings_page.RepositorySync') as service, \
                patch('qfluentwidgets.InfoBar.warning') as warning:
            self.page = SettingsPage(self.config, self.log)
            self.assertEqual(self.page.repo_root_edit.text(), '')
            self.page.sync_button.click()
            warning.assert_called_once()
            self.assertIn('请先在设置→GitHub同步选择本地项目Git根目录', warning.call_args.args[1])
            validate.assert_not_called()
            service.assert_not_called()
            self.assertFalse(self.page.jobs.busy)

    def test_configured_frozen_root_and_all_credentials_reach_guarded_sync(self):
        self.config.update(('sync', 'repo_root'), str(self.root))
        self.config.config['api']['api_key'] = 'active-fixture-key'
        self.config.config['stations'] = [{'api_key': 'station-fixture-key', 'upload_api_key': 'upload-fixture-key'}]
        with patch('ui.pages.settings_page.sys.frozen', True, create=True), \
                patch('ui.pages.settings_page.RepositorySync') as service, \
                patch('qfluentwidgets.InfoBar.success') as success:
            service.return_value.sync.return_value = dict(commit='fixture-commit')
            self.page.sync_button.click()
            wait_until(lambda: not self.page.jobs.busy)
            service.return_value.sync.assert_called_once_with()
            self.assertEqual(service.call_args.kwargs['root'], str(self.root))
            self.assertEqual(service.call_args.kwargs['secrets'],
                             ('active-fixture-key', 'station-fixture-key', 'upload-fixture-key'))
            success.assert_called_once()
        self.assertTrue(self.page.sync_button.isEnabled())
        self.assertTrue(self.page.repo_select_button.isEnabled())

    def test_correct_main_origin_tree_uses_full_guarded_workflow_without_network(self):
        self.config.update(('sync', 'repo_root'), str(self.root))
        with patch.object(RepositorySync, '_push_and_verify') as push, \
                patch('qfluentwidgets.InfoBar.success') as success:
            self.page.sync_button.click()
            wait_until(lambda: not self.page.jobs.busy)
            commit = self.git('rev-parse', 'HEAD')
            push.assert_called_once_with('origin', commit)
            success.assert_called_once()
            self.assertIn(commit, success.call_args.args[1])
        self.assertEqual(set(self.git('ls-files').splitlines()), {'.gitignore', 'main.py'})
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_sync_revalidates_branch_after_directory_was_ready(self):
        self.select(self.root)
        self.git('symbolic-ref', 'HEAD', 'refs/heads/develop')
        self.assert_sync_rejected('请先切换到main分支')

    def test_slow_validation_keeps_event_loop_responsive_and_prevents_reentry(self):
        self.config.update(('sync', 'repo_root'), str(self.root))
        release = threading.Event()
        heartbeat = []
        def slow(root, **kwargs):
            release.wait(5)
            return validate_repository_root(root, **kwargs)
        with patch('ui.pages.settings_page.validate_repository_root', side_effect=slow) as validate, \
                patch('ui.pages.settings_page.RepositorySync') as service, \
                patch('qfluentwidgets.InfoBar.warning') as warning, \
                patch('qfluentwidgets.InfoBar.success'):
            service.return_value.sync.return_value = dict(commit='fixture-commit')
            try:
                self.page.sync_github()
                QTimer.singleShot(0, lambda: heartbeat.append(True))
                QTest.qWait(30)
                self.assertEqual(heartbeat, [True])
                self.assertFalse(self.page.sync_button.isEnabled())
                self.assertFalse(self.page.repo_select_button.isEnabled())
                self.page.sync_github()
                self.page.select_repository_root()
                self.assertEqual(warning.call_count, 2)
                self.assertEqual(validate.call_count, 1)
            finally:
                release.set()
                wait_until(lambda: not self.page.jobs.busy)
            service.return_value.sync.assert_called_once()
        self.assertTrue(self.page.sync_button.isEnabled())
        self.assertTrue(self.page.repo_select_button.isEnabled())

    def test_validation_exception_reports_failure_and_restores_both_buttons(self):
        self.config.update(('sync', 'repo_root'), str(self.root))
        with patch('ui.pages.settings_page.validate_repository_root', side_effect=RuntimeError('fixture-validation-error')), \
                patch('ui.pages.settings_page.RepositorySync') as service, \
                patch('qfluentwidgets.InfoBar.error') as error:
            self.page.sync_button.click()
            wait_until(lambda: not self.page.jobs.busy)
            service.assert_not_called()
            error.assert_called_once()
            self.assertIn('fixture-validation-error', error.call_args.args[1])
        self.assertTrue(self.page.sync_button.isEnabled())
        self.assertTrue(self.page.repo_select_button.isEnabled())

    def test_multiline_errors_and_controls_fit_card_after_window_resize(self):
        self.git('symbolic-ref', 'HEAD', 'refs/heads/develop')
        self.git('remote', 'set-url', 'origin', 'https://github.com/another/project.git')
        self.select(self.root)
        for width in (960, 1200, 800):
            with self.subTest(width=width):
                self.page.resize(width, 680)
                QTest.qWait(20)
                status = self.page.repo_status_label
                self.assertGreaterEqual(status.height(), status.heightForWidth(status.width()))
                for control in (status, self.page.repo_root_edit, self.page.repo_select_button, self.page.sync_button):
                    top_left = control.mapTo(self.page.repo_sync_card, control.rect().topLeft())
                    bottom_right = control.mapTo(self.page.repo_sync_card, control.rect().bottomRight())
                    self.assertTrue(self.page.repo_sync_card.rect().contains(top_left))
                    self.assertTrue(self.page.repo_sync_card.rect().contains(bottom_right))


if __name__ == '__main__':
    unittest.main()
