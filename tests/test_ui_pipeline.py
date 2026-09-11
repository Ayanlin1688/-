"""UI + QThread + local HTTP + disk, all using an isolated config."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest
from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from test_task_manager import wait_until
from test_http_clients import LocalServer


class PipelineUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_scan_generate_history_reopen_redownload_and_safe_close(self):
        with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompt = root / '测试分镜.txt'; prompt.write_text('<Picture1> test', encoding='utf-8')
            image = root / '测试分镜.png'; image.write_bytes(b'fixture-image')
            manager = ConfigManager(root / 'config.json'); manager.load_config()
            manager.config['paths'] = dict(prompts=str(root), images=str(root), output=str(root / '视频输出'))
            manager.config['api'].update(base_url=server.base, api_key='test-only', upload_url=server.base + '/upload')
            manager.config['workspace']['poll_interval'] = 3
            manager.save_config()
            window = MainWindow(manager); window.show()
            workspace = window.workspace_page
            wait_until(lambda: len(workspace.data_source.matches) == 1 and not workspace.jobs.busy)
            self.assertEqual(workspace.queue_panel.list.count(), 1)
            self.assertIn('已匹配 1', workspace.data_source.status_label.text())
            window.settings_page.test_button.click()
            wait_until(lambda: not window.settings_page.jobs.busy)
            self.assertTrue(window.settings_page.test_button.isEnabled())
            workspace.start_button.click()
            self.assertFalse(workspace.start_button.isEnabled())
            wait_until(lambda: not workspace.task_manager.is_running)
            self.assertEqual(window.history_page.table.rowCount(), 1)
            self.assertEqual(window.history_page.table.item(0, 3).text(), '已完成')
            self.assertEqual(workspace.recent_panel.list.count(), 1)
            record = manager.config['history'][0]
            target = Path(record['result_path'])
            self.assertTrue(target.is_file())
            with patch('os.startfile') as opened:
                window.history_page.table.setCurrentCell(0, 0)
                window.history_page.open_button.click()
                opened.assert_called_once_with(str(target.parent))
            window.close(); window.deleteLater(); QTest.qWait(40)
            reopened = MainWindow(ConfigManager(root / 'config.json')); reopened.show()
            wait_until(lambda: not reopened.workspace_page.jobs.busy)
            self.assertEqual(reopened.history_page.table.rowCount(), 1)
            self.assertEqual(reopened.config_manager.config['history'][0]['task_id'], record['task_id'])
            target.unlink()
            post_count = len(server.calls)
            reopened.workspace_page.redownload(record)
            wait_until(lambda: not reopened.workspace_page.jobs.busy)
            self.assertTrue(target.exists())
            self.assertEqual(len(server.calls), post_count)
            self.assertEqual(reopened.config_manager.config['history'][0]['filename'], target.name)
            # Changed prompt is a new job. Closing during polling waits for the worker to stop.
            prompt.write_text('changed text', encoding='utf-8')
            server.always_processing = True
            reopened.workspace_page.start_button.click()
            wait_until(lambda: reopened.workspace_page.task_manager.tasks and reopened.workspace_page.task_manager.tasks[0]['status'] == 'processing')
            reopened.close()
            self.assertTrue(reopened._closing)
            wait_until(lambda: not reopened.workspace_page.task_manager.is_running and not reopened.isVisible())
            self.assertEqual(reopened.config_manager.config['history'][-1]['status'], 'cancelled')
            reopened.deleteLater(); QTest.qWait(40)
