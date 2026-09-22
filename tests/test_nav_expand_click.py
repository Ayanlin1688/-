"""导航项展开态点击回归：点击后不得收缩消失（QFluentWidgets 展开动画 sizeHint=0 缺陷）。

实测复现：展开态点击「历史记录/设置/工作台」→ setExpanded 动画按 sizeHint（宽 0）设置尺寸，
被点击项宽度归零从界面消失；用户侧表现为“点击后就消失了”。
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication


class NavExpandClickTests(unittest.TestCase):
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
        window.resize(1200, 800)
        window.show()
        QTest.qWait(400)
        return temp, window

    def test_click_in_expand_mode_keeps_item_width(self):
        temp, window = self._window()
        try:
            nav = window.navigationInterface
            if nav.panel.isCollapsed():
                window._toggle_navigation()          # 展开（240 宽文字导航）
                QTest.qWait(600)
            for route in ('historyPage', 'settingsPage', 'workspacePage'):
                item = nav.widget(route)
                inner = item.itemWidget
                inner.itemClicked.emit(True, False)  # 与真实点击同一条处理链（叶子 clickArrow 恒 False）
                QTest.qWait(400)
                self.assertGreater(item.width(), 120,
                                   f'{route} 点击后宽度异常：{item.width()}（应保持展开宽度，而非收缩消失）')
        finally:
            window.close()
            window.deleteLater()
            QTest.qWait(200)
            temp.cleanup()

    def test_expand_toggle_round_trip_keeps_widths(self):
        temp, window = self._window()
        try:
            nav = window.navigationInterface
            for _ in range(2):
                window._toggle_navigation()
                QTest.qWait(500)
                for route in ('historyPage', 'settingsPage'):
                    item = nav.widget(route)
                    inner = item.itemWidget
                    inner.itemClicked.emit(True, False)
                    QTest.qWait(250)
            for route in ('historyPage', 'settingsPage', 'workspacePage'):
                self.assertGreater(nav.widget(route).width(), 40,
                                   f'{route} 宽度异常：{nav.widget(route).width()}')
        finally:
            window.close()
            window.deleteLater()
            QTest.qWait(200)
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
