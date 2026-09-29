"""Window-control regressions: maximize, fullscreen, double-click and state sync."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QEvent, QPointF, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from core.config_manager import ConfigManager
from ui.main_window import MainWindow


class WindowControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = MainWindow(ConfigManager(Path(self.temp.name) / 'config.json'), network_time=False)
        self.window.show()
        QTest.qWait(120)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(40)
        self.temp.cleanup()

    def test_toggle_fullscreen_and_restore_syncs_button_state(self):
        btn = self.window.titleBar.maxBtn
        normal_geometry = self.window.geometry()
        self.assertFalse(self.window.isMaximized())
        self.window._studio_toggle_maximized()
        QTest.qWait(120)
        self.assertTrue(self.window.isFullScreen())
        self.assertTrue(self.window._window_geometry_matches(True))
        self.assertTrue(btn._isMax)
        self.window._studio_toggle_maximized()
        QTest.qWait(120)
        self.assertFalse(self.window.isFullScreen())
        self.assertFalse(self.window.isMaximized())
        self.assertEqual(self.window.geometry(), normal_geometry)
        self.assertFalse(btn._isMax)

    def test_maximize_button_click_toggles_fullscreen_state(self):
        btn = self.window.titleBar.maxBtn
        QTest.mouseClick(btn, Qt.LeftButton)
        QTest.qWait(180)
        self.assertTrue(self.window.isFullScreen())
        self.assertTrue(self.window._window_geometry_matches(True))
        self.assertTrue(btn._isMax)
        QTest.mouseClick(btn, Qt.LeftButton)
        QTest.qWait(180)
        self.assertFalse(self.window.isFullScreen())
        self.assertFalse(self.window.isMaximized())
        self.assertFalse(btn._isMax)

    def test_rapid_double_toggle_lands_on_restored_state(self):
        # 快速连点（间隔小于 160ms 校验窗口）必须最终落在“还原”状态：
        # 旧校验不得覆盖第二次点击的还原意图。
        self.window._studio_toggle_maximized()
        QTest.qWait(60)
        self.window._studio_toggle_maximized()
        QTest.qWait(450)
        self.assertFalse(self.window.isFullScreen())
        self.assertFalse(self.window.isMaximized())
        self.assertFalse(self.window.titleBar.maxBtn._isMax)

    def test_fullscreen_round_trip_restores_frame_and_button_state(self):
        self.window.showFullScreen()
        QTest.qWait(120)
        self.assertTrue(self.window.isFullScreen())
        self.assertTrue(self.window._window_geometry_matches(True))
        self.assertEqual(self.window._frame_margin, 0)
        self.assertEqual(self.window.titleBar.geometry().x(), 0)
        self.assertTrue(self.window.titleBar.maxBtn._isMax)

        self.window.showNormal()
        QTest.qWait(120)
        self.assertFalse(self.window.isFullScreen())
        self.assertEqual(self.window._frame_margin, self.window._window_margin)
        self.assertEqual(self.window.titleBar.geometry().x(), self.window._window_margin)
        self.assertFalse(self.window.titleBar.maxBtn._isMax)

    def test_titlebar_double_click_uses_studio_toggle(self):
        event = QMouseEvent(
            QEvent.MouseButtonDblClick,
            QPointF(300, 20),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        self.window.titleBar.mouseDoubleClickEvent(event)
        QTest.qWait(120)
        self.assertTrue(self.window.isFullScreen())
        self.assertTrue(self.window._window_geometry_matches(True))
        self.assertTrue(self.window.titleBar.maxBtn._isMax)

    def test_maximize_compatibility_entry_still_uses_available_screen(self):
        self.window._apply_max_state(True)
        QTest.qWait(120)
        self.assertTrue(self.window.isMaximized())
        self.assertTrue(self.window._window_geometry_matches(False))
        self.window._apply_max_state(False)
        QTest.qWait(120)
        self.assertFalse(self.window.isMaximized())

    def test_fullscreen_from_maximized_restores_maximized_state(self):
        self.window._apply_max_state(True)
        QTest.qWait(120)
        self.assertTrue(self.window.isMaximized())
        self.window._studio_toggle_maximized()
        QTest.qWait(120)
        self.assertTrue(self.window.isFullScreen())
        self.window._studio_toggle_maximized()
        QTest.qWait(180)
        self.assertFalse(self.window.isFullScreen())
        self.assertTrue(self.window.isMaximized())
        self.assertTrue(self.window.titleBar.maxBtn._isMax)


if __name__ == '__main__':
    unittest.main()
