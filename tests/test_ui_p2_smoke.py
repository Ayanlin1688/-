"""P2 打磨冒烟：弹窗信息架构（当前任务置顶）、启动按钮禁用提示、对比度下限。"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication


def _luminance(color):
    color = color.lstrip('#')
    channels = [int(color[index:index + 2], 16) / 255.0 for index in (0, 2, 4)]
    channels = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(foreground, background):
    first, second = _luminance(foreground), _luminance(background)
    hi, lo = max(first, second), min(first, second)
    return (hi + 0.05) / (lo + 0.05)


class AuditP2SmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        temp = tempfile.TemporaryDirectory()
        manager = ConfigManager(Path(temp.name) / 'config.json')
        manager.load_config()
        window = MainWindow(manager, network_time=False)
        window.resize(1400, 880)
        window.show()
        QTest.qWait(400)
        return temp, window

    def test_controls_dialog_orders_current_task_first(self):
        temp, window = self._window()
        try:
            page = window.workspace_page
            page.open_controls()
            QTest.qWait(300)
            center = page.center_scroll.widget()
            first = center.layout().itemAt(0).widget()
            self.assertIs(first, page.current_task)
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    def test_start_button_disabled_tooltip(self):
        temp, window = self._window()
        try:
            page = window.workspace_page
            self.assertTrue(page.start_button.isEnabled())
            self.assertEqual(page.start_button.toolTip(), '')
            page._running_changed(True)
            self.assertFalse(page.start_button.isEnabled())
            self.assertTrue(page.start_button.toolTip())
            page._running_changed(False)
            self.assertTrue(page.start_button.isEnabled())
            self.assertEqual(page.start_button.toolTip(), '')
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    def test_contrast_pairs_meet_floor(self):
        from ui.materials import DARK_TEXT_MAP
        backgrounds = {'bg1': '#0A0B12', 'bg2': '#10111C', 'card': '#1A1C26'}
        pairs = {
            '辅助文字': DARK_TEXT_MAP['#8B93A3'],
            '时间戳/计数': DARK_TEXT_MAP['#7A8294'],
            '表头': DARK_TEXT_MAP['#9AA6B8'],
            '弱标签': DARK_TEXT_MAP['#6B7280'],
        }
        report = []
        for name, color in pairs.items():
            for bg_name, bg in backgrounds.items():
                ratio = _contrast(color, bg)
                report.append(f'{name} vs {bg_name} = {ratio:.2f}')
                self.assertGreaterEqual(ratio, 3.0, f'{name} vs {bg_name}: {ratio:.2f}')
        print('\n'.join(report))


    def test_settings_default_duration_reaches_workspace(self):
        temp, window = self._window()
        try:
            window.settings_page.default_duration.setValue(11)
            QTest.qWait(50)
            self.assertEqual(window.config_manager.config['workspace']['duration'], 11)
            self.assertEqual(window.workspace_page.params_card.duration.value(), 11)
            self.assertEqual(window.settings_page.default_duration.value(), 11)
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

if __name__ == '__main__':
    unittest.main()
