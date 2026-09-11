import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QTime
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import TimePicker, SwitchSettingCard
from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from test_task_manager import wait_until


class ScheduleUiTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.window = MainWindow(self.config, network_time=False)
        self.window.show()
        self.now = datetime(2026, 9, 11, 22, 54)
        self.window.clock.now = lambda: self.now
        self.window.schedule_engine.now = lambda: self.now
        QTest.qWait(30)

    def tearDown(self):
        self.window.close(); self.window.deleteLater(); QTest.qWait(30)
        self.temp.cleanup()

    def arm(self):
        settings = self.window.settings_page
        settings.schedule_time.setTime(QTime(23, 0))
        settings.schedule_time.timeChanged.emit(QTime(23, 0))
        settings.schedule_enabled.switchButton.setChecked(True)

    def test_fluent_controls_persist_tick_once_and_disable_after_due(self):
        settings = self.window.settings_page
        self.assertIsInstance(settings.schedule_time, TimePicker)
        self.assertIsInstance(settings.schedule_enabled, SwitchSettingCard)
        self.arm()
        self.assertIn('23:00', self.window.next_schedule_label.text())
        self.assertEqual(ConfigManager(self.config.path).load_config()['schedule']['time'], '23:00')
        self.now += timedelta(minutes=1)
        self.window._schedule_tick(); self.window._schedule_tick()
        self.assertEqual(self.window.workspace_page.log_drawer.browser.toPlainText().count('距离定时开始还有5分钟'), 1)
        with patch.object(self.window.workspace_page, 'start_generation') as start:
            self.now = self.now.replace(hour=23, minute=0)
            self.window._schedule_tick(); self.window._schedule_tick()
            start.assert_called_once()
        self.assertFalse(settings.schedule_enabled.isChecked())
        self.assertFalse(ConfigManager(self.config.path).load_config()['schedule']['enabled'])
        self.assertIn('未启用', self.window.next_schedule_label.text())

    def test_busy_skips_daily_and_switch_off_cancels(self):
        self.window.settings_page.schedule_mode.setCurrentText('每天重复')
        self.arm()
        self.window.workspace_page._redownloading = True
        self.now = self.now.replace(hour=23, minute=0)
        with patch.object(self.window.workspace_page, 'start_generation') as start:
            self.window._schedule_tick(); start.assert_not_called()
        self.assertEqual(self.window.schedule_engine.next_run, self.now + timedelta(days=1))
        self.assertIn('跳过本次', self.window.workspace_page.log_drawer.browser.toPlainText())
        self.window.workspace_page._redownloading = False
        self.window.settings_page.schedule_enabled.switchButton.setChecked(False)
        self.assertIsNone(self.window.schedule_engine.next_run)

    def test_scheduled_completion_closes_only_when_selected_and_not_cancelled(self):
        self.config.config['schedule']['after_finish'] = 'close'
        self.window.workspace_page.task_manager.tasks = [dict(status='cancelled')]
        self.window._scheduled_batch = True
        self.window._scheduled_finished()
        QTest.qWait(150)
        self.assertTrue(self.window.isVisible())
        self.window.workspace_page.task_manager.tasks = [dict(status='completed')]
        self.window._scheduled_batch = True
        self.window._scheduled_finished()
        wait_until(lambda: not self.window.isVisible())

    def test_sync_button_dispatches_background_work_and_recovers_on_failure(self):
        settings = self.window.settings_page
        with patch('ui.pages.settings_page.RepositorySync') as service:
            service.return_value.sync.side_effect = RuntimeError('test-only network unavailable')
            settings.sync_button.click()
            self.assertFalse(settings.sync_button.isEnabled())
            wait_until(lambda: not settings.jobs.busy)
            service.return_value.sync.assert_called_once()
        self.assertTrue(settings.sync_button.isEnabled())
        self.assertIn('test-only network unavailable', self.window.workspace_page.log_drawer.browser.toPlainText())
