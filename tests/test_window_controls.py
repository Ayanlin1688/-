"""Window-control regressions: maximize toggle, rapid-toggle race, icon sync (R7)."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
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

    def test_toggle_maximize_and_restore_syncs_button_state(self):
        btn = self.window.titleBar.maxBtn
        self.assertFalse(self.window.isMaximized())
        self.window._studio_toggle_maximized()
        QTest.qWait(120)
        self.assertTrue(self.window.isMaximized())
        self.assertTrue(btn._isMax)
        self.window._studio_toggle_maximized()
        QTest.qWait(120)
        self.assertFalse(self.window.isMaximized())
        self.assertFalse(btn._isMax)

    def test_rapid_double_toggle_lands_on_restored_state(self):
        # 快速连点（间隔小于 160ms 校验窗口）必须最终落在“还原”状态：
        # 修复前，第一次点击的延迟校验会把第二次点击的还原结果重新最大化（竞态）。
        self.window._studio_toggle_maximized()
        QTest.qWait(60)
        self.window._studio_toggle_maximized()
        QTest.qWait(450)
        self.assertFalse(self.window.isMaximized())
        self.assertFalse(self.window.titleBar.maxBtn._isMax)


if __name__ == '__main__':
    unittest.main()
