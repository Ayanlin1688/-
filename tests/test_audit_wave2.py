"""波次二审计修复的回归护栏：表头对齐、日志上限、跳过意图保留、账本 ignore_ids、https 约束、注册表压缩。"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QPoint
from PyQt5.QtWidgets import QApplication


_APP = None


def _app():
    global _APP
    if _APP is None:
        # 必须模块级保留引用：丢弃引用会让 QApplication 被回收，之后构建 QWidget 直接崩溃。
        _APP = QApplication.instance() or QApplication([])
    return _APP


class TaskControlSkipIntentTests(unittest.TestCase):
    """跳过意图在“交接窗口”（release 之后、begin 之前）与排队期间都必须保留。"""

    def test_skip_between_release_and_begin_is_kept(self):
        from core.task_state import TaskControl
        from core.http_client import Cancelled
        control = TaskControl()
        control.begin(0)
        control.release(0)
        control.skip_for(0)          # 交接窗口：不在 active 集合内
        control.begin(0)             # 任务真正开始执行
        with self.assertRaises(Cancelled):
            control.check()
        control.release(0)

    def test_skip_while_queued_is_kept(self):
        from core.task_state import TaskControl
        from core.http_client import Cancelled
        control = TaskControl()
        control.skip_for(3)          # 排队中（从未 begin）
        control.begin(3)
        with self.assertRaises(Cancelled):
            control.check()
        control.release(3)

    def test_classic_skip_still_works_and_release_clears(self):
        from core.task_state import TaskControl
        from core.http_client import Cancelled
        control = TaskControl()
        control.begin(5)
        control.skip_for(5)
        with self.assertRaises(Cancelled):
            control.check()
        control.release(5)
        control.begin(5)             # 结束后标记被清理：新一轮 begin 不再触发跳过
        control.check()
        control.release(5)


class LedgerIgnoreIdsTests(unittest.TestCase):
    """reserve(ignore_ids=...) 跳过弃用兄弟记录，且不影响同请求防重语义。"""

    def test_ignore_ids_releases_busy_blocker(self):
        from core.submission_ledger import SubmissionLedger
        with tempfile.TemporaryDirectory() as temp:
            ledger = SubmissionLedger(Path(temp) / 'submissions.sqlite3')
            first = dict(prompt_sha256='p1', signature='s1', local_id='a')
            outcome, saved = ledger.reserve(first, 'scope')
            self.assertEqual(outcome, 'claimed')
            second = dict(prompt_sha256='p1', signature='s2', local_id='b')
            outcome, saved = ledger.reserve(second, 'scope')
            self.assertEqual(outcome, 'busy')
            blocker = saved['ledger_id']
            outcome, saved = ledger.reserve(second, 'scope', ignore_ids=[blocker])
            self.assertEqual(outcome, 'claimed')

    def test_same_request_duplicate_still_blocks(self):
        from core.submission_ledger import SubmissionLedger
        with tempfile.TemporaryDirectory() as temp:
            ledger = SubmissionLedger(Path(temp) / 'submissions.sqlite3')
            first = dict(prompt_sha256='p1', signature='s1', local_id='a')
            ledger.reserve(first, 'scope')
            same = dict(prompt_sha256='p1', signature='s1', local_id='b')
            outcome, _ = ledger.reserve(same, 'scope')
            self.assertEqual(outcome, 'duplicate')


class UpdateCheckHttpsTests(unittest.TestCase):
    """更新资产只信任 https：非 https 的下载链接不下发。"""

    def test_http_asset_url_is_filtered(self):
        from core.update_check import check_for_update
        manifest = {'version': '99.0.0', 'assets': [{'download_url': 'http://evil.example/x.zip', 'sha256': 'abc'}]}
        result = check_for_update('https://manifest.example/update.json', fetch=lambda url: manifest)
        self.assertIsNotNone(result)
        self.assertEqual(result['url'], '')

    def test_https_asset_url_is_kept(self):
        from core.update_check import check_for_update
        manifest = {'version': '99.0.0', 'assets': [{'download_url': 'https://cdn.example/x.zip', 'sha256': 'abc'}]}
        result = check_for_update('https://manifest.example/update.json', fetch=lambda url: manifest)
        self.assertEqual(result['url'], 'https://cdn.example/x.zip')


class ThemeRegistryCompactionTests(unittest.TestCase):
    """弱引用注册表在主题遍历时原位压缩：死引用剔除，条目不再只增不减。"""

    def test_dead_callback_reference_is_removed(self):
        from ui import materials

        class Target:
            def __init__(self):
                self.hits = 0

            def refresh(self):
                self.hits += 1

        materials.run_theme_callbacks()  # 先清理前序测试残留的死引用
        target = Target()
        materials.register_theme_callback(target.refresh)
        entry = materials._THEME_CALLBACKS[-1]
        materials.run_theme_callbacks()
        self.assertEqual(target.hits, 1)
        self.assertIn(entry, materials._THEME_CALLBACKS)      # 活引用保留
        del target
        # 注意：此处不要 gc.collect()——全量运行时会强制回收前面 UI 测试遗留的 Qt
        # 包装对象，触发 PyQt 原生段错误；引用计数即可让 weakref 失效。
        materials.run_theme_callbacks()
        self.assertNotIn(entry, materials._THEME_CALLBACKS)   # 死引用被原位压缩剔除


class LogDrawerCapTests(unittest.TestCase):
    """日志条目超过上限后只保留最新的 MAX_ENTRIES 条。"""

    def test_entries_are_capped(self):
        _app()
        from ui.widgets.log_drawer import LogDrawer
        drawer = LogDrawer()
        drawer.MAX_ENTRIES = 50
        for index in range(60):
            drawer.append_log(f'第{index}条', 'info')
        self.assertEqual(len(drawer.entries), 50)
        self.assertEqual(drawer.entries[-1][1], '第59条')
        self.assertEqual(drawer.entries[0][1], '第10条')


class TaskTableHeaderAlignmentTests(unittest.TestCase):
    """表头与数据行在多种宽度下逐像素对齐（cellRect 起点完全一致）。"""

    def _build(self, width):
        _app()
        from ui.widgets.workspace_task_table import WorkspaceTaskTable
        table = WorkspaceTaskTable({})
        table.show()
        _app().processEvents()
        table.resize(width, 600)
        _app().processEvents()
        tasks = [dict(local_id=f'id{i}', prompt_path=f'C:/x/{i}.txt', prompt_name=f'{i:02d}',
                      product='p', model='video-v3', status='waiting', images=[], progress=0,
                      elapsed=0, eta=-1, match_method='', requested_model='video-v3') for i in range(1, 3)]
        table.update_tasks(tasks)
        _app().processEvents()
        table._apply_column_widths()
        _app().processEvents()
        return table

    @staticmethod
    def _cell_xs(grid, surface, cols=12):
        parent = grid.parentWidget()
        return [parent.mapTo(surface, grid.cellRect(0, col).topLeft()).x() for col in range(cols)]

    def _assert_aligned(self, width):
        table = self._build(width)
        try:
            surface = table.surface
            header_x = self._cell_xs(table._header_grid, surface)
            row_x = self._cell_xs(table.rows[0].grid, surface)
            self.assertEqual(header_x, row_x,
                             f'宽度 {width}（实际 {table.width()}）下表头与数据列未对齐：'
                             f'header={header_x} row={row_x}')
        finally:
            table.close()
            table.deleteLater()
            _app().processEvents()

    def test_alignment_at_medium_width(self):
        self._assert_aligned(1200)

    def test_alignment_at_wide_width(self):
        self._assert_aligned(1500)


if __name__ == '__main__':
    unittest.main()
