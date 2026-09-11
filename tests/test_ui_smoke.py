import os
import unittest
import tempfile
from pathlib import Path
from core.config_manager import ConfigManager

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication
from PyQt5.QtWidgets import QListWidget
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtTest import QTest

from ui.main_window import MainWindow


class UiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = MainWindow(ConfigManager(Path(self.temp.name) / 'config.json'), network_time=False)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(20)
        self.temp.cleanup()

    def test_window_contains_navigation_pages_and_workspace_controls(self):
        window = self.window
        self.assertEqual(window.windowTitle(), "StoryboardVideoStudio")
        self.assertTrue(hasattr(window, "workspace_page"))
        self.assertTrue(hasattr(window, "history_page"))
        self.assertTrue(hasattr(window, "settings_page"))
        self.assertEqual(window.workspace_page.queue_panel.list.count(), 0)
        self.assertEqual(window.history_page.table.rowCount(), 0)
        self.assertIn("开始生成", window.workspace_page.start_button.text())
        window.close()

    def test_queue_and_recent_items_are_fluent_row_widgets(self):
        window = self.window
        task = dict(prompt_name='真实任务', status='completed', result_path=str(Path(self.temp.name) / '结果.mp4'), finished_at='2026-09-10T12:00:00')
        window.workspace_page.queue_panel.update_tasks([task])
        window.workspace_page.recent_panel.update_history([task])
        queue = window.workspace_page.queue_panel.list
        recent = window.workspace_page.recent_panel.list
        self.assertIsNotNone(queue.itemWidget(queue.item(0)))
        self.assertIsNotNone(recent)
        self.assertIsNotNone(recent.itemWidget(recent.item(0)))
        self.assertNotIn("✅", queue.item(0).text())
        window.close()

    def test_advanced_parameters_toggle_with_collapsible_state(self):
        window = self.window
        window.show()
        QCoreApplication.processEvents()
        card = window.workspace_page.params_card
        self.assertTrue(card.advanced.isVisible())
        card.toggle_advanced()
        QTest.qWait(220)
        self.assertFalse(card.advanced.isVisible())
        card.toggle_advanced()
        QTest.qWait(220)
        self.assertTrue(card.advanced.isVisible())
        window.close()


if __name__ == "__main__":
    unittest.main()
