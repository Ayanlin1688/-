"""Execution closure regressions; never contacts a paid provider."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core.config_manager import DEFAULT_CONFIG
from core.task_manager import TaskWorker
from core.submission_safety import SubmissionGate
from core.task_state import TaskControl
from test_http_clients import LocalServer


class ExecutionClosureTests(unittest.TestCase):
    def config(self, root, server):
        prompts = root / 'prompts'
        prompts.mkdir()
        (prompts / '01.txt').write_text('A product camera movement', encoding='utf-8')
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['paths'].update(prompts=str(prompts), images='', output=str(root / 'out'))
        config['api'].update(base_url=server.base, api_key='fixture', upload_url=server.base + '/upload')
        config['task_strategy'].update(max_concurrency=1, auto_retry=False, unmatched_prompt='仍提交文生视频')
        config['workspace'].update(poll_interval=.01, poll_timeout=.06)
        return config

    def test_default_poll_timeout_is_two_hours(self):
        self.assertEqual(DEFAULT_CONFIG['workspace'].get('poll_timeout'), 7200)

    def test_scan_reports_cap_before_submission_and_retains_bindings(self):
        with tempfile.TemporaryDirectory() as directory, LocalServer() as server:
            root = Path(directory)
            config = self.config(root, server)
            images = root / 'images'; images.mkdir()
            for i in range(10):
                (images / f'01({i}).png').write_bytes(b'fixture-image')
            config['paths']['images'] = str(images)
            config['prompt_detection']['enabled'] = False
            config['workspace'].update(model='MiniMax-H3', resolution='1080p')
            worker = TaskWorker(config, [])
            logs = []
            worker.log_message.connect(lambda message, level: logs.append(message))
            worker._scan()
            self.assertEqual(len(worker.tasks[0]['images']), 10)
            self.assertTrue(any('已自动截取前9张' in text for text in logs), logs)
            self.assertEqual(server.calls, [])

    def test_known_id_recovers_even_if_unmatched_policy_now_skips(self):
        with tempfile.TemporaryDirectory() as directory, LocalServer() as server:
            config = self.config(Path(directory), server)
            server.always_processing = True
            first = TaskWorker(config, [])
            first.run()
            paid_id = first.tasks[0]['task_id']
            self.assertTrue(paid_id)
            config['task_strategy']['unmatched_prompt'] = '跳过并警告'
            server.always_processing = False
            second = TaskWorker(config, first.tasks)
            second.run()
            self.assertEqual(second.tasks[0]['status'], 'completed', second.tasks[0])
            self.assertEqual(second.tasks[0]['task_id'], paid_id)
            self.assertEqual(len([row for row in server.calls if row[0] == '/videos']), 1)

    def test_lost_reference_files_recover_existing_paid_id_without_post(self):
        with tempfile.TemporaryDirectory() as directory, LocalServer() as server:
            root = Path(directory)
            config = self.config(root, server)
            images = root / 'images'
            images.mkdir()
            image = images / '01.jpg'
            image.write_bytes(b'good-image')
            config['paths']['images'] = str(images)
            server.always_processing = True
            first = TaskWorker(config, [])
            first.run()
            paid_id = first.tasks[0]['task_id']
            self.assertTrue(paid_id)
            image.unlink()
            config['task_strategy']['unmatched_prompt'] = '跳过并警告'
            server.always_processing = False
            second = TaskWorker(config, [])
            second.run()
            self.assertEqual(second.tasks[0]['status'], 'completed', second.tasks[0])
            self.assertEqual(second.tasks[0]['task_id'], paid_id)
            self.assertEqual(len([row for row in server.calls if row[0] == '/videos']), 1)

    def test_poll_stall_reports_elapsed_and_keeps_known_id(self):
        with tempfile.TemporaryDirectory() as directory, LocalServer() as server:
            config = self.config(Path(directory), server)
            config['workspace'].update(poll_timeout=150, poll_interval=61)
            worker = TaskWorker(config, [])
            logs = []
            worker.log_message.connect(lambda message, level: logs.append(message))
            clock = [0.0]
            worker.control.delay = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
            server.always_processing = True
            with patch('core.task_execution.time.monotonic', side_effect=lambda: clock[0]):
                worker.run()
            self.assertTrue(any('上游进度未更新' in message for message in logs), logs)
            self.assertTrue(any('已轮询1分1秒' in message for message in logs), logs)
            self.assertGreaterEqual(worker.tasks[0]['poll_elapsed_seconds'], 61)
            self.assertTrue(worker.tasks[0]['task_id'])

    def test_conversion_error_stops_failover_and_posts_nothing(self):
        with tempfile.TemporaryDirectory() as directory, LocalServer() as server:
            config = self.config(Path(directory), server)
            config['model_pool']['enabled'] = True
            config['task_strategy'].update(auto_retry=True, max_retries=2, retry_interval=0)
            worker = TaskWorker(config, [])
            with patch('core.task_execution.convert_for_model', side_effect=ValueError('invalid structured prompt')) as conversion:
                worker.run()
            self.assertEqual(conversion.call_count, 1)
            self.assertEqual(worker.tasks[0]['status'], 'failed')
            self.assertEqual(server.calls, [])

    def test_submission_permit_is_exclusive_and_releases_waiter(self):
        gate = SubmissionGate(5)
        control = TaskControl()
        entered = threading.Event()
        release = threading.Event()
        second = threading.Event()
        def hold():
            with gate.permit(control):
                entered.set()
                release.wait(2)
        def follow():
            with gate.permit(TaskControl()):
                second.set()
        first_thread = threading.Thread(target=hold)
        first_thread.start()
        self.assertTrue(entered.wait(2))
        follower = threading.Thread(target=follow)
        follower.start()
        self.assertFalse(second.wait(.05))
        release.set()
        first_thread.join(2)
        follower.join(2)
        self.assertTrue(second.is_set())


    def test_completed_without_url_keeps_polling_same_task(self):
        with tempfile.TemporaryDirectory() as directory, LocalServer() as server:
            config = self.config(Path(directory), server)
            config['task_strategy'].update(auto_retry=False, max_retries=0)
            config['workspace'].update(poll_interval=.01, poll_timeout=5)
            server.suppress_result_url = 1
            worker = TaskWorker(config, [])
            logs = []
            worker.log_message.connect(lambda message, level: logs.append(message))
            worker.run()
            task = worker.tasks[0]
            self.assertEqual(task['status'], 'completed', task)
            self.assertGreaterEqual(server.polls.get(task['task_id'], 0), 3)
            self.assertEqual(len([row for row in server.calls if row[0] == '/videos']), 1)
            self.assertTrue(any('下载地址尚未返回' in message for message in logs), logs)


if __name__ == '__main__':
    unittest.main()
