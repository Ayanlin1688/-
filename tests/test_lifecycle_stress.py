"""窗口生命周期压力：快速多次打开/关闭不崩溃、无异常残留。"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from ui.main_window import MainWindow


class LifecycleStressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_rapid_open_close_cycles(self):
        temp = tempfile.TemporaryDirectory()
        try:
            for index in range(6):
                window = MainWindow(ConfigManager(Path(temp.name) / f'config-{index}.json'), network_time=False)
                window.show()
                QTest.qWait(40)
                window.close()
                window.deleteLater()
                QTest.qWait(25)
        finally:
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
