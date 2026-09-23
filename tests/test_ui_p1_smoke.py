"""P1 规范层冒烟：表格对齐与时间单行、状态胶囊、对比度映射、日志操作图标化。"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication


class AuditP1SmokeTests(unittest.TestCase):
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

    # ---------- 任务表对齐规范 ----------
    def test_task_table_alignment_rules(self):
        temp, window = self._window()
        try:
            panel = window.workspace_page.queue_panel
            tasks = [dict(local_id=f'id{i}', prompt_path=f'C:/x/{i}.txt', prompt_name=f'{i:02d}产品演示',
                          product='汽车', model='video-v3', status='waiting', images=[],
                          progress=0, elapsed=0, eta=-1, requested_model='video-v3') for i in range(1, 3)]
            panel.update_tasks(tasks)
            QTest.qWait(200)
            row = panel.rows[0]
            # 行内呼吸点保留：承载「活跃任务心跳」动画语义，状态列胶囊与其共存。
            self.assertFalse(row.dot.isHidden())
            self.assertTrue(row.ratio.alignment() & Qt.AlignCenter)              # 比例居中
            self.assertTrue(row.resolution.alignment() & Qt.AlignCenter)
            self.assertTrue(row.duration.alignment() & Qt.AlignCenter)
            self.assertTrue(row.timing.alignment() & Qt.AlignRight)              # 时间右对齐
            heads = panel._header_cells
            self.assertTrue(heads[1].alignment() & Qt.AlignLeft)                 # 提示词表头左
            self.assertTrue(heads[9].alignment() & Qt.AlignRight)                # 用时/剩余表头右
            self.assertTrue(heads[4].alignment() & Qt.AlignCenter)               # 比例表头居中
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    # ---------- 历史表对齐与时间单行 ----------
    def test_history_alignment_and_single_line_time(self):
        temp, window = self._window()
        try:
            records = [dict(local_id=f'h{i}', status='completed', prompt_name=f'{i:02d}产品演示',
                            product='汽车', model='video-v3', created_at='2026-09-22T10:01:00',
                            finished_at='2026-09-22T10:01:48', size_bytes=8500000 + i * 120000,
                            result_path=f'C:/out/{i}.mp4') for i in range(1, 4)]
            page = window.history_page
            window.switchTo(page)
            page.update_history(records)
            QTest.qWait(300)
            prompt_item = page.table.item(0, 2)
            self.assertTrue(prompt_item.textAlignment() & Qt.AlignLeft)
            size_item = page.table.item(0, 7)
            self.assertTrue(size_item.textAlignment() & Qt.AlignRight)
            created = page.table.item(0, 5).text()
            self.assertNotIn('\n', created)
            self.assertEqual(len(created), 11)
            self.assertEqual(created, '09-22 10:01')
            header_item = page.table.horizontalHeaderItem(5)
            self.assertTrue(header_item.textAlignment() & Qt.AlignRight)
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    # ---------- 对比度映射提亮 ----------
    def test_dark_text_map_brightened(self):
        from ui import materials
        previous = materials.LIGHT_MODE
        try:
            materials.set_light_mode(False)
            self.assertEqual(materials.map_text_color('#8B93A3'), '#A8B0C0')
            self.assertEqual(materials.map_text_color('#7A8294'), '#99A2B4')
            self.assertEqual(materials.map_text_color('#6B7280'), '#8F98AB')
        finally:
            materials.set_light_mode(previous)

    # ---------- 日志操作图标化 ----------
    def test_log_actions_are_icon_buttons(self):
        temp, window = self._window()
        try:
            log = window.workspace_page.log_drawer
            self.assertEqual(log.export_button.text(), '')
            self.assertTrue(log.export_button.toolTip())
            self.assertEqual(log.clear_button.text(), '')
            self.assertTrue(log.clear_button.toolTip())
            self.assertEqual(log.export_button.width(), 28)
            self.assertEqual(log.clear_button.width(), 28)
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()

    # ---------- 设置侧栏：分组标题存在 + 点击跳转功能 ----------
    def test_settings_rail_group_headers(self):
        temp, window = self._window()
        try:
            page = window.settings_page
            window.switchTo(page)
            QTest.qWait(300)
            from qfluentwidgets import CaptionLabel
            heads = [w for w in page.rail.findChildren(CaptionLabel) if w.text()]
            self.assertGreaterEqual(len(heads), 4)          # 分组标题齐备
            before = page.page_stack.currentIndex()
            page._jump_to_group('appearance')
            self.assertNotEqual(page.page_stack.currentIndex(), before)  # 点击可跳页
        finally:
            window.close(); window.deleteLater(); QTest.qWait(150)
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
