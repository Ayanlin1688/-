"""P0 布局升级冒烟：监看带 / 概览卡分级 / 更多菜单 / 取消确认 / 顶栏状态胶囊。"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication


class WorkspaceP0SmokeTests(unittest.TestCase):
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

    # ---------- P0-1 监看带 ----------
    def test_monitor_bar_idle_and_updates(self):
        temp, window = self._window()
        try:
            page = window.workspace_page
            bar = page.task_monitor
            self.assertTrue(bar.isVisible())
            self.assertEqual(bar.title.text(), '尚未开始任务')
            self.assertFalse(bar.skip_button.isEnabled())
            task = {'status': 'processing', 'prompt_name': '02产品演示', 'product': '家居',
                    'model': 'video-v3', 'images': [1, 2]}
            bar.update_task(1, task)
            self.assertIn('02', bar.title.text())
            self.assertIn('02产品演示', bar.title.text())
            self.assertEqual(bar.state_label.text(), '生成中')
            self.assertTrue(bar.product_chip.isVisible())
            self.assertEqual(bar.model_chip.text(), 'video-v3')
            bar.update_progress(42, 125, 180)
            self.assertEqual(bar.percent.text(), '42%')
            self.assertIn('已用', bar.timing.text())
            self.assertIn('125', bar.timing.text())
            self.assertIn('剩余', bar.timing.text())
            bar.show_idle()
            self.assertEqual(bar.title.text(), '尚未开始任务')
            self.assertEqual(bar.percent.text(), '0%')
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    def test_monitor_failed_state_color_path(self):
        temp, window = self._window()
        try:
            bar = window.workspace_page.task_monitor
            bar.update_task(4, {'status': 'failed', 'prompt_name': '05产品演示', 'model': 'video-v3'})
            self.assertEqual(bar.state_label.text(), '失败')
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    # ---------- P0-2 概览卡分级 ----------
    def test_summary_four_primary_cards_and_compound(self):
        temp, window = self._window()
        try:
            summary = window.workspace_page.summary
            self.assertEqual(len(summary.blocks), 4)
            self.assertEqual(len(summary.stat_cards), 4)
            for key in ('product', 'failed', 'active'):
                self.assertIn(key, summary.compound.rows)
            tasks = [
                {'status': 'completed', 'product': '汽车', 'images': [1]},
                {'status': 'processing', 'product': '汽车', 'images': [1]},
                {'status': 'failed', 'product': '家居'},
            ]
            summary.update_tasks(tasks, [], 12.0, True)
            self.assertEqual(summary.blocks[0].value.text(), '1/3')
            self.assertIn('生成中 1', summary.blocks[0].sub.text())
            self.assertEqual(summary.blocks[3].value.text(), '1')   # 待处理 = processing
            self.assertEqual(summary.compound.rows['failed'].text(), '1')
            self.assertEqual(summary.compound.rows['active'].text(), '1')
            self.assertEqual(summary.cards[0].number.text(), '3')  # 兼容字段
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    # ---------- P0-3 更多菜单 + 取消确认 ----------
    def test_more_menu_structure(self):
        temp, window = self._window()
        try:
            panel = window.workspace_page.queue_panel
            self.assertTrue(panel.more_button.isVisible())
            self.assertFalse(panel.cancel_button.isVisible())       # 收进菜单，不再占位
            self.assertEqual(panel.cancel_button.height(), 32)      # 兼容：尺寸仍可读
            menu = panel.build_more_menu()
            texts = [action.text() for action in menu.actions()]
            self.assertEqual(len(texts), 3)
            for expected in ('重置模型识别', '生成参数', '取消全部'):
                self.assertIn(expected, texts)
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    def test_cancel_all_idle_path_is_safe(self):
        temp, window = self._window()
        try:
            page = window.workspace_page
            page._confirm_cancel_all()   # 未运行：直接完成，无弹窗、无异常
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    # ---------- P0-4 顶栏状态胶囊 ----------
    def test_status_pill_and_hidden_metrics(self):
        temp, window = self._window()
        try:
            page = window.workspace_page
            self.assertTrue(page.status_pill.isVisible())
            self.assertFalse(page.product_status.isVisible())
            self.assertFalse(page.task_status.isVisible())
            self.assertFalse(page.concurrent_status.isVisible())
            page._update_status_strip([{'status': 'completed'}], 65.0, True)
            self.assertEqual(page.status_text.text(), '批量生成中')
            self.assertIn('00:01:05', page.elapsed_status.text())
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    # ---------- 布局顺序：监看带位于目录行与任务队列之间 ----------
    def test_layout_order(self):
        temp, window = self._window()
        try:
            page = window.workspace_page
            root = page.layout()
            names = []
            for index in range(root.count()):
                item = root.itemAt(index)
                widget = item.widget()
                names.append(widget.objectName() if widget else None)
            monitor_y = page.task_monitor.y()
            splitter_y = page.splitter.y()
            self.assertLess(monitor_y, splitter_y)
            for name in names:
                if name == 'workspacePage':
                    continue
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
