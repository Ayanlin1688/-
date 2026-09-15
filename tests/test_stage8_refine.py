"""Stage 8 acceptance: round-2 refinements (toolbar alignment, dialogs, stations nav)."""
import copy
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest
from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from ui.components.studio_dialog import StudioDialog
from ui.components.match_dialog import MatchDialog
from ui.components.model_selector import ModelComboBox
from test_task_manager import wait_until


class Round2RefineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.load_config()
        self.config.config['paths'] = {'prompts': '', 'images': '', 'output': ''}
        self.config.save_config()
        self.window = MainWindow(self.config, network_time=False)
        self.window.resize(1467, 951)
        self.window.show()
        QTest.qWait(200)

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater(); QTest.qWait(30)
        self.temp.cleanup()

    def test_toolbar_controls_share_one_height_and_empty_state(self):
        panel = self.window.workspace_page.queue_panel
        heights = {name: getattr(panel, name).height() for name in
                   ('filter_box', 'reset_models_button', 'params_button', 'match_button', 'cancel_button')}
        self.assertEqual(set(heights.values()), {32}, heights)
        self.assertTrue(panel.empty_panel.isVisible())
        self.assertFalse(panel.list.isVisible())
        self.assertEqual(panel.empty_button.height(), 32)
        self.assertIn('选择目录', panel.empty_button.text())

    def test_titlebar_gap_and_dialog_chrome(self):
        QTest.qWait(120)  # 等待延迟应用的标题栏补丁
        self.assertTrue(getattr(self.window.titleBar, '_studio_gap', False))
        labels = [c for c in self.window.titleBar.children() if hasattr(c, 'text') and callable(getattr(c, 'text', None)) and c.text()]
        title = labels[-1]
        self.assertGreaterEqual(title.x(), 26)

        page = self.window.workspace_page
        page.open_controls()
        QTest.qWait(300)
        dlg = page.controls_dialog
        self.assertIsInstance(dlg, StudioDialog)
        self.assertEqual(dlg.header.title_label.text(), '生成参数与任务详情')
        image = dlg.grab().toImage()
        corner = image.pixelColor(3, 3)
        self.assertEqual(corner.alpha(), 0, '弹窗四角应为圆角（透明）')
        inside = image.pixelColor(40, 40)
        self.assertEqual(inside.alpha(), 255)
        self.assertGreaterEqual(page.data_source.fields['prompts'].width(), 400)
        dlg.close(); QTest.qWait(120)

    def test_match_dialog_uses_studio_chrome(self):
        md = MatchDialog(self.window, log_callback=lambda *a: None, config_manager=self.config, matches=[])
        md.resize(1000, 680); md.show()
        QTest.qWait(200)
        self.assertEqual(md.header.title_label.text(), '图片与提示词匹配详情')
        image = md.grab().toImage()
        self.assertEqual(image.pixelColor(3, 3).alpha(), 0)
        md.close(); QTest.qWait(120)

    def test_model_combo_shows_compact_closed_text(self):
        combo = ModelComboBox()
        records = {'video-v3': {'id': 'video-v3', 'kind': 'video', 'protocol_known': True,
                                'resolutions': ['720p', '1080p'], 'pricing_text': '计费未提供'}}
        full = 'video-v3 · 720p / 1080p · 计费未提供'
        combo._compact[full] = 'video-v3 · 720p'
        combo.setText(full)
        shown = combo.text() if not callable(getattr(combo, 'text', None)) else combo.text()
        self.assertIn('video-v3', str(shown))
        self.assertNotIn('计费', str(shown))

    def test_station_nav_per_station_and_dynamic_title(self):
        settings = self.window.settings_page
        self.window.switchTo(settings)
        QTest.qWait(200)
        settings._capture_current_station()
        QTest.qWait(80)
        self.assertEqual(len(settings._station_nav_buttons), 1)
        self.assertIn('当前线路', settings.api_group.titleLabel.text())
        station_id = settings._stations()[0]['id']
        settings._station_nav_buttons[station_id].click()
        QTest.qWait(120)
        self.assertTrue(settings._station_nav_buttons[station_id].property('railActive'))
        # 滚动联动不应崩溃且能高亮某个分类
        settings.scroll.verticalScrollBar().setValue(600)
        QTest.qWait(120)
        active = [b for b in settings._rail_buttons.values() if b.property('railActive')]
        self.assertTrue(active)


if __name__ == '__main__':
    unittest.main()
