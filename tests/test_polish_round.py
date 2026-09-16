"""收尾批次集成：账号与许可卡片 / 导航五组 / 历史空态 / 减弱动效开关。"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from test_task_manager import wait_until


class PolishRoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.window = MainWindow(self.config, network_time=False)
        self.window.show()
        QTest.qWait(100)

    def tearDown(self):
        from ui.motion import set_reduced_motion
        set_reduced_motion(False)
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater()
        QTest.qWait(30)
        self.temp.cleanup()

    def test_settings_license_card_shows_badge_and_trial_progress(self):
        settings = self.window.settings_page
        self.assertIn('license', settings._page_order)
        self.assertEqual(settings.license_badge.text(), '试用中')
        self.assertTrue(settings.license_status_label.text().startswith('许可状态'))
        self.assertFalse(settings.license_trial_bar.isHidden())
        self.assertIn('试用剩余', settings.license_trial_caption.text())

    def test_settings_rail_five_sections_in_plan_order(self):
        settings = self.window.settings_page
        self.assertEqual(list(settings._rail_buttons.keys()),
                         ['许可与激活', '外观与语言', '模型池', '默认参数', '中转站管理',
                          '当前线路', 'GitHub 同步', '任务策略', '定时执行'])

    def test_history_empty_state_and_action_navigates(self):
        history = self.window.history_page
        history.update_history([])
        self.assertFalse(history.empty_view.isHidden())
        self.assertTrue(history.table.isHidden())
        hits = []
        history.create_requested.connect(lambda: hits.append(1))
        history.empty_action.click()
        self.assertEqual(hits, [1])
        self.window.switchTo(self.window.settings_page)
        QTest.qWait(80)
        history.empty_action.click()
        QTest.qWait(80)
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspace_page)
        history.update_history([dict(prompt_name='样例任务', status='completed', model='video-v3', product='示例')])
        self.assertTrue(history.empty_view.isHidden())
        self.assertFalse(history.table.isHidden())

    def test_reduce_motion_switch_persists_and_applies(self):
        from ui.motion import reduced_motion
        settings = self.window.settings_page
        settings.reduce_motion.switchButton.setChecked(True)
        QTest.qWait(50)
        self.assertTrue(self.config.config['appearance']['reduce_motion'])
        self.assertTrue(reduced_motion())
        settings.reduce_motion.switchButton.setChecked(False)
        QTest.qWait(50)
        self.assertFalse(self.config.config['appearance']['reduce_motion'])
        self.assertFalse(reduced_motion())


if __name__ == '__main__':
    unittest.main()
