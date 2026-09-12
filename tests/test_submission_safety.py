"""Paid creation is never repeated after an ambiguous response."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication
from core.config_manager import DEFAULT_CONFIG
from core.task_manager import TaskManager
from core.task_state import task_signature
from test_http_clients import LocalServer, FixtureHandler
from test_task_manager import wait_until


class SafetyHandler(FixtureHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls.append((self.path, dict(self.headers), body))
        if self.server.create_status:
            response = {'error': 'ambiguous provider failure'}
            if self.server.error_id:
                response['task_id'] = self.server.error_id
            return self.respond(response, self.server.create_status)
        if self.server.drop_response:
            self.connection.close()
            return
        return self.respond({'task_id': 'safe-task'})


class SafetyServer(LocalServer):
    def __enter__(self):
        server = super().__enter__()
        server.RequestHandlerClass = SafetyHandler
        server.create_status = 0
        server.error_id = ''
        server.drop_response = False
        return server


class ConcurrentLimitHandler(FixtureHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls.append((self.path, dict(self.headers), body))
        number = int(json.loads(body)['prompt'].split()[-1])
        self.server.barrier.wait(timeout=5)
        if number == 1:
            return self.respond({'task_id': 'running-before-limit'})
        time.sleep(.1 + number * .02)
        return self.respond({'error': 'too many requests'}, 429)


class ConcurrentLimitServer(LocalServer):
    def __enter__(self):
        server = super().__enter__()
        server.RequestHandlerClass = ConcurrentLimitHandler
        server.barrier = threading.Barrier(4)
        server.always_processing = True
        return server


class SubmissionSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prompts = self.root / 'prompts'
        self.prompts.mkdir()
        self.prompt = self.prompts / '1.txt'
        self.prompt.write_text('A simple view of a car', encoding='utf-8')
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config['prompt_detection']['enabled'] = False
        self.config['paths'].update(prompts=str(self.prompts), output=str(self.root / 'out'))
        self.config['workspace'].update(poll_interval=.01, duration=8)
        self.config['task_strategy'].update(unmatched_prompt='仍提交文生视频', retry_interval=.01)
        self.config['_submission_ledger_path'] = str(self.root / 'submissions.sqlite3')
        self.manager = TaskManager()

    def tearDown(self):
        self.manager.cancel_all()
        wait_until(lambda: not self.manager.is_running)
        self.temp.cleanup()

    def run_batch(self, server):
        self.config['api'].update(base_url=server.base, api_key='local-fixture')
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running, timeout=10000)
        return self.manager.tasks[0]

    def test_signature_uses_content_not_name_or_mtime(self):
        task = {'prompt_path': str(self.prompt), 'images': []}
        params = self.config['workspace']
        first = task_signature(task, 'video-v3', params, 'https://provider.invalid/v1')
        other = self.prompts / 'renamed.txt'
        other.write_text(self.prompt.read_text(), encoding='utf-8')
        self.assertEqual(first, task_signature(dict(task, prompt_path=str(other)), 'video-v3', params, 'https://provider.invalid/v1'))
        stat = self.prompt.stat()
        self.prompt.write_text('A simple view of a bus', encoding='utf-8')
        os.utime(self.prompt, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertNotEqual(first, task_signature(task, 'video-v3', params, 'https://provider.invalid/v1'))

    def test_lost_response_is_not_recreated_even_after_restart(self):
        with SafetyServer() as server:
            server.drop_response = True
            task = self.run_batch(server)
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(task['status'], 'submission_unknown')
            self.assertEqual(task['prompt_sha256'], hashlib.sha256(b'A simple view of a car').hexdigest())
            self.manager = TaskManager()
            self.run_batch(server)
            self.assertEqual(len(server.calls), 1)

    def test_503_and_429_never_create_again(self):
        with SafetyServer() as server:
            for status in (503, 429):
                with self.subTest(status=status):
                    self.prompt.write_text(f'Provider error {status}', encoding='utf-8')
                    server.create_status = status
                    before = len(server.calls)
                    task = self.run_batch(server)
                    self.assertEqual(len(server.calls) - before, 1)
                    self.assertEqual(task['status'], 'submission_unknown')

    def test_error_response_id_is_recovered_without_new_post(self):
        with SafetyServer() as server:
            server.create_status = 503
            server.error_id = 'already-created'
            task = self.run_batch(server)
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(task['task_id'], 'already-created')
            self.assertEqual(task['status'], 'completed')

    def test_auth_and_route_errors_remain_blocked_after_restart(self):
        with SafetyServer() as server:
            for status in (401, 403, 404):
                with self.subTest(status=status):
                    self.prompt.write_text(f'Provider error {status}', encoding='utf-8')
                    server.create_status = status
                    before = len(server.calls)
                    first = self.run_batch(server)
                    self.assertEqual(first['status'], 'submission_unknown')
                    self.manager = TaskManager()
                    self.config['task_strategy']['prevent_duplicates'] = False
                    second = self.run_batch(server)
                    self.assertEqual(second['status'], 'submission_unknown')
                    self.assertEqual(len(server.calls) - before, 1)

    def test_same_content_two_names_only_one_create_and_duplicate_status(self):
        (self.prompts / '2.txt').write_text(self.prompt.read_text(), encoding='utf-8')
        self.config['task_strategy']['max_concurrency'] = 2
        with SafetyServer() as server:
            self.run_batch(server)
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(sorted(t['status'] for t in self.manager.tasks), ['completed', 'duplicate'])

    def test_three_429_pause_new_work_while_existing_id_keeps_polling(self):
        for index in range(1, 6):
            (self.prompts / f'{index}.txt').write_text(f'Local task {index}', encoding='utf-8')
        self.config['task_strategy']['max_concurrency'] = 4
        with ConcurrentLimitServer() as server:
            self.config['api'].update(base_url=server.base, api_key='local-fixture')
            self.manager.start_tasks(self.config)
            wait_until(lambda: self.manager.is_paused, timeout=10000)
            self.assertEqual(self.manager.worker.gate.limit, 1)
            self.assertEqual(len(server.calls), 4)
            self.assertEqual(self.manager.tasks[4]['status'], 'waiting')
            before = server.polls.get('running-before-limit', 0)
            wait_until(lambda: server.polls.get('running-before-limit', 0) > before)
            server.always_processing = False
            wait_until(lambda: self.manager.tasks[0]['status'] == 'completed')
            self.assertEqual(len(server.calls), 4)
            self.manager.cancel_all()
            wait_until(lambda: not self.manager.is_running)


if __name__ == '__main__':
    unittest.main()
