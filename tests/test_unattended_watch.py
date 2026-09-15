"""无人值守监听生命周期：失败退避重挂、新任务自动触发、运行中让位。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from ui.pages.workspace_page import WorkspacePage
from test_task_manager import wait_until


class BoomMatcher:
    """扫描必定失败的替身：模拟目录瞬断 / 网络盘掉线。"""

    warnings = []
    recursive = True

    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def from_config(cls, config):
        return cls()

    def scan_and_match(self, paths):
        raise RuntimeError('目录暂不可用')


class WatchLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_page(self, root, settle=True):
        manager = ConfigManager(root / 'config.json')
        manager.config['paths'] = {'prompts': str(root / '提示词'), 'images': str(root / '图片'),
                                   'output': str(root / '输出')}
        manager.config['task_strategy']['watch_interval'] = 5
        manager.config['prompt_detection']['enabled'] = False
        page = WorkspacePage(manager)
        logs = []
        page.append_log = lambda message, level='info': logs.append((level, message))
        if settle:
            # 让启动时的 2.5s 自动挂载先发生，再停表由测试手动驱动，保证确定性。
            QTest.qWait(2600)
            page._watch_timer.stop()
        wait_until(lambda: not page.jobs.busy)
        return page, logs

    def teardown_page(self, page):
        page.shutdown()
        wait_until(lambda: not page.jobs.busy)
        page.close()
        page.deleteLater()
        QTest.qWait(20)

    def test_scan_failure_rearms_watch_with_backoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('提示词', '图片', '输出'):
                (root / name).mkdir()
            (root / '提示词' / 'a.txt').write_text('场景一', encoding='utf-8')
            page, logs = self.make_page(root)
            try:
                with patch('ui.pages.workspace_page.StoryboardMatcher', BoomMatcher):
                    page._watch_fired()
                    wait_until(lambda: not page.jobs.busy)
                self.assertEqual(page._watch_retry, 1)
                self.assertTrue(page._watch_timer.isActive())
                self.assertTrue(any('自动重试' in message for _, message in logs))
                # 恢复后重扫成功 → 退避计数清零
                page._watch_fired()
                wait_until(lambda: not page.jobs.busy)
                self.assertEqual(page._watch_retry, 0)
                self.assertTrue(page._watch_timer.isActive())
            finally:
                self.teardown_page(page)

    def test_new_task_triggers_automatic_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('提示词', '图片', '输出'):
                (root / name).mkdir()
            (root / '提示词' / 'a.txt').write_text('场景一', encoding='utf-8')
            page, logs = self.make_page(root)
            try:
                page._watch_fired()  # 第一次：建立基线
                wait_until(lambda: not page.jobs.busy)
                self.assertIsNotNone(page._watch_seen)
                starts = []
                page.start_generation = lambda: starts.append(1)
                (root / '提示词' / 'b.txt').write_text('场景二', encoding='utf-8')
                page._watch_fired()
                wait_until(lambda: not page.jobs.busy)
                self.assertEqual(starts, [1])
                self.assertTrue(any('发现 1 个新任务' in message for _, message in logs))
            finally:
                self.teardown_page(page)

    def test_running_queue_defers_watch_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('提示词', '图片', '输出'):
                (root / name).mkdir()
            page, logs = self.make_page(root)
            try:
                page.task_manager.is_running = True
                try:
                    page._watch_fired()
                finally:
                    page.task_manager.is_running = False
                self.assertFalse(page._watch_pending_scan)
                self.assertTrue(page._watch_timer.isActive())
            finally:
                self.teardown_page(page)


if __name__ == '__main__':
    unittest.main()
