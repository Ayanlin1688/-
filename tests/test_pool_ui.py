"""Fluent controls, persistence and indexed concurrent progress."""
import copy
import os
from pathlib import Path
import tempfile
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from test_task_manager import wait_until


class PoolUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.save_config()
        self.window = MainWindow(self.config, network_time=False)
        QTest.qWait(20)

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater(); QTest.qWait(20)
        self.temp.cleanup()

    def test_pool_concurrency_and_shared_retry_settings_persist(self):
        settings = self.window.settings_page
        params = self.window.workspace_page.params_card
        self.assertFalse(settings.pool_enabled.isChecked())
        settings.pool_enabled.setChecked(True)
        settings.max_concurrency.setValue(2)
        settings.max_retry.setValue(7)
        self.assertEqual(params.retries.value(), 7)
        params.threshold.setValue(4)
        self.assertEqual(settings.fail_threshold.value(), 4)
        stored = ConfigManager(self.config.path).load_config()
        self.assertTrue(stored['model_pool']['enabled'])
        self.assertEqual(stored['task_strategy']['max_concurrency'], 2)
        self.assertEqual(stored['task_strategy']['max_retries'], 7)
        self.assertEqual(stored['task_strategy']['failure_skip_threshold'], 4)

    def test_cooldown_refresh_preserves_user_enable_and_model_rows(self):
        settings = self.window.settings_page
        settings.model_rows[0][1].setChecked(False)
        settings.update_pool_state([dict(name='seedance-2.5', status='冷却中', cooldown_until=time.time()+.15,
                                        remaining=.15, consecutive_failures=1)])
        self.assertIn('冷却中', settings.model_rows[0][3].text())
        self.assertFalse(self.config.config['model_pool']['models'][0]['enabled'])
        QTest.qWait(180)
        settings.refresh_pool_state()
        self.assertIn('健康', settings.model_rows[0][3].text())
        self.assertFalse(self.config.config['model_pool']['models'][0]['enabled'])
        settings._remove_model_row(settings.model_rows[1][0])
        settings._persist_models()
        self.assertEqual(len(self.config.config['model_pool']['models']), 2)

    def test_retry_rows_counts_selection_and_progress_are_isolated(self):
        page = self.window.workspace_page
        manager = page.task_manager
        tasks = [dict(prompt_name=f'task{i}', model='video-v3', status=status, images=[], task_id=str(i))
                 for i, status in enumerate(['processing', 'retry_wait', 'waiting', 'completed', 'cooling'])]
        manager._list(copy.deepcopy(tasks))
        manager.select_current(0)
        self.assertEqual(page.current_task.concurrency_label.text(), '正在生成3个，排队1个')
        manager._progress(0, 20, 2, 8)
        manager._progress(1, 90, 9, 1)
        self.assertEqual(page.current_task.progress.value(), 20)
        QTest.keyClick(page.queue_panel.list, Qt.Key_Down)
        self.assertEqual(manager.current_index, 1)
        manager._progress(1, 70, 7, 3)
        self.assertEqual(page.current_task.progress.value(), 70)
        self.assertIn('重试中', page.current_task.title.text())
        page.queue_panel.filter_box.setCurrentText('等待冷却')
        self.assertFalse(page.queue_panel.task_item(4).isHidden())
        self.assertTrue(page.queue_panel.task_item(0).isHidden())
