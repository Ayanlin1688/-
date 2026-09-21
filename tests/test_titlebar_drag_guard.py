"""标题栏拖拽加固回归（R13）：阈值内抖动不启动窗口移动；超阈值维持库行为。"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QEvent, QPointF, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

import qframelesswindow.titlebar as tbmod
from core.config_manager import ConfigManager
from ui.main_window import MainWindow


class TitleBarDragGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        self._original = tbmod.startSystemMove
        tbmod.startSystemMove = lambda window, pos: self.calls.append((pos.x(), pos.y()))
        self.window = MainWindow(ConfigManager(Path(self.temp.name) / 'config.json'), network_time=False)
        self.window.show()
        QTest.qWait(140)
        self.bar = self.window.titleBar

    def tearDown(self):
        tbmod.startSystemMove = self._original
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(40)
        self.temp.cleanup()

    def press(self, x, y):
        event = QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(self.bar, event)

    def move(self, x, y):
        event = QMouseEvent(QEvent.MouseMove, QPointF(x, y), Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(self.bar, event)

    def release(self, x, y):
        event = QMouseEvent(QEvent.MouseButtonRelease, QPointF(x, y), Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        QApplication.sendEvent(self.bar, event)

    def test_small_jitter_does_not_start_window_move(self):
        self.press(300, 20)
        for offset in (1, 2, 3, 4, 5):
            self.move(300 + offset, 20)
        self.release(305, 20)
        self.assertEqual(self.calls, [])

    def test_move_beyond_threshold_starts_window_move(self):
        self.press(300, 20)
        self.move(302, 20)
        self.assertEqual(self.calls, [])
        self.move(320, 20)
        self.assertGreaterEqual(len(self.calls), 1)
        self.release(320, 20)

    def test_next_press_can_start_move_again(self):
        self.press(300, 20)
        self.move(320, 20)
        self.assertGreaterEqual(len(self.calls), 1)
        first = len(self.calls)
        self.release(320, 20)
        self.press(300, 20)
        self.move(332, 20)
        self.assertGreaterEqual(len(self.calls), first + 1)
        self.release(332, 20)

    def test_press_left_of_buttons_outside_drag_region_does_not_start_move(self):
        # 按钮簇左侧按下（非拖拽区）：即使大幅移动也不启动系统移动。
        width = self.bar.width()
        self.press(width - 60, 16)
        self.move(width - 10, 16)
        self.release(width - 10, 16)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
