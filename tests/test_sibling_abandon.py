"""兄弟任务"已提交后被跳过"→ 等待任务必须接管提交(CC-P2-1 回归)。

实测复现(修复前):队列内两个同文案、不同参考图任务;先提交者进入 processing 后
被跳过(本地弃用、账本记录保持活跃),等待中的兄弟被陈旧占用静默判重、从未提交。
修复后:等待者放行接管提交;POST /videos 计数为 2,无误判 duplicate。
"""
import copy
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest

from core.config_manager import DEFAULT_CONFIG
from core.task_manager import TaskManager
from test_http_clients import LocalServer
from test_task_manager import wait_until


class SiblingAbandonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _start_batch(self, temp, server):
        root = Path(temp)
        prompts = root / 'prompts'; images = root / 'images'
        prompts.mkdir(); images.mkdir()
        text = 'same text for both prompts'
        (prompts / '1.txt').write_text(text, encoding='utf-8')
        (prompts / '2.txt').write_text(text, encoding='utf-8')
        (images / '1(1).png').write_bytes(b'image-one')
        (images / '2(1).png').write_bytes(b'image-two-different')
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['prompt_detection']['enabled'] = False
        config['paths'].update(prompts=str(prompts), images=str(images), output=str(root / 'out'))
        config['api'].update(base_url=server.base, api_key='***', upload_url=server.base + '/upload')
        config['workspace'].update(poll_interval=.02, duration=8)
        config['task_strategy'].update(unmatched_prompt='仍提交文生视频', retry_interval=.01, max_concurrency=2)
        config['_submission_ledger_path'] = str(root / 'submissions.sqlite3')
        server.always_processing = True
        manager = TaskManager()
        manager.start_tasks(config)
        return manager

    def _wait_split(self, manager, timeout=25000):
        """等到“一个提交成功（processing）+ 一个在等待（uploading）”，返回 (submitter, waiter)。"""
        def phase():
            if not manager.tasks or len(manager.tasks) < 2:
                return False
            statuses = [t.get('status') for t in manager.tasks]
            return 'processing' in statuses and 'uploading' in statuses
        try:
            wait_until(phase, timeout)
        except AssertionError:
            self.fail(f'未进入 提交/等待 分层状态：{[t.get("status") for t in (manager.tasks or [])]}')
        submitter = next(i for i, t in enumerate(manager.tasks) if t.get('status') == 'processing')
        return submitter, 1 - submitter

    def test_skip_submitter_lets_waiter_take_over(self):
        with tempfile.TemporaryDirectory() as temp, LocalServer() as server:
            manager = self._start_batch(temp, server)
            try:
                submitter, waiter = self._wait_split(manager)
                manager.select_current(submitter)
                manager.skip_current()
                try:
                    wait_until(lambda: manager.tasks[waiter].get('status') == 'processing', 20000)
                except AssertionError:
                    self.fail(f'等待者未能接管提交：{[t.get("status") for t in manager.tasks]}')
                posts = [c for c in server.calls if c[0] == '/videos']
                self.assertEqual(len(posts), 2, '两个任务都应提交：先提交者 + 接管者')
                self.assertIsNone(manager.tasks[waiter].get('duplicate_of'))
            finally:
                manager.cancel_all()
                wait_until(lambda: not manager.is_running, 8000)

    def test_skip_waiter_keeps_submitter_processing(self):
        with tempfile.TemporaryDirectory() as temp, LocalServer() as server:
            manager = self._start_batch(temp, server)
            try:
                submitter, waiter = self._wait_split(manager)
                manager.select_current(waiter)
                manager.skip_current()
                wait_until(lambda: manager.tasks[waiter].get('status') == 'skipped', 8000)
                # 提交者不受影响,且没有发生第二次提交。
                QTest.qWait(600)
                self.assertEqual(manager.tasks[submitter].get('status'), 'processing')
                posts = [c for c in server.calls if c[0] == '/videos']
                self.assertEqual(len(posts), 1)
            finally:
                manager.cancel_all()
                wait_until(lambda: not manager.is_running, 8000)


if __name__ == '__main__':
    unittest.main()
