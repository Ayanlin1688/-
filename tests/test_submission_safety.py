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
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication
from core.config_manager import DEFAULT_CONFIG
from core.submission_safety import SubmissionGate
from core.api_client import ApiClient, SubmissionUncertain, _is_definitive_precreation_rejection
from core.http_client import RequestError
from core.submission_ledger import SubmissionLedger, account_scope, ledger_path
from core.task_manager import TaskManager
from core.task_state import task_signature
from test_http_clients import LocalServer, FixtureHandler
from test_task_manager import wait_until


class SafetyHandler(FixtureHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls.append((self.path, dict(self.headers), body))
        if self.server.create_status:
            response = self.server.response_payload or {'error': 'ambiguous provider failure'}
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
        server.response_payload = None
        server.drop_response = False
        return server


class ConcurrentLimitHandler(FixtureHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls.append((self.path, dict(self.headers), body))
        # The first admitted create succeeds; later creates are rate limited.
        # Prompt order is not the admission order, so do not special-case task 1.
        if not getattr(self.server, 'winner_sent', False):
            self.server.winner_sent = True
            return self.respond({'task_id': 'running-before-limit'})
        return self.respond({'error': 'too many requests'}, 429)


class ConcurrentLimitServer(LocalServer):
    def __enter__(self):
        server = super().__enter__()
        server.RequestHandlerClass = ConcurrentLimitHandler
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
        wait_until(lambda: not self.manager.is_running, timeout=15000)
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

    def test_nested_quota_rejection_is_failed_and_can_be_retried(self):
        payload = {
            'code': 'fail_to_fetch_task',
            'message': json.dumps({
                'code': 'insufficient_user_quota',
                'message': '预扣费额度失败：用户剩余额度 ¥0.152000，需要预扣费额度 ¥1.800000',
                'data': None,
            }, ensure_ascii=False),
            'data': None,
        }
        self.config['task_strategy']['auto_retry'] = False
        with SafetyServer() as server:
            server.create_status = 403
            server.response_payload = payload
            failed = self.run_batch(server)
            self.assertEqual(failed['status'], 'failed')
            self.assertIn('额度不足', failed['error'])
            self.assertIn('¥0.152000', failed['error'])
            records = SubmissionLedger(ledger_path(self.config)).records(account_scope(self.config))
            self.assertTrue(records)
            self.assertNotIn(records[-1]['ledger_state'], {'unknown', 'reserved', 'submitting'})

            server.create_status = 0
            server.response_payload = None
            self.manager = TaskManager()
            retried = self.run_batch(server)
            self.assertEqual(retried['status'], 'completed')
            self.assertEqual(len([path for path, _, _ in server.calls if path == '/videos']), 2)

    def test_nested_auth_rejection_is_failed_and_can_be_retried(self):
        self.config['task_strategy']['auto_retry'] = False
        with SafetyServer() as server:
            server.create_status = 401
            server.response_payload = {
                'code': 'unauthorized',
                'message': 'invalid_api_key：鉴权失败，请检查 API Key',
                'data': None,
            }
            failed = self.run_batch(server)
            self.assertEqual(failed['status'], 'failed')
            self.assertIn('鉴权失败', failed['error'])
            records = SubmissionLedger(ledger_path(self.config)).records(account_scope(self.config))
            self.assertNotIn(records[-1]['ledger_state'], {'unknown', 'reserved', 'submitting'})

            server.create_status = 0
            server.response_payload = None
            self.manager = TaskManager()
            retried = self.run_batch(server)
            self.assertEqual(retried['status'], 'completed')
            self.assertEqual(len([path for path, _, _ in server.calls if path == '/videos']), 2)

    def test_unknown_403_and_non_definitive_responses_keep_duplicate_guard(self):
        self.config['task_strategy']['auto_retry'] = False
        with SafetyServer() as server:
            server.create_status = 403
            server.response_payload = {'code': 'waf_block', 'message': 'request blocked by gateway', 'data': None}
            first = self.run_batch(server)
            self.assertEqual(first['status'], 'submission_unknown')
            records = SubmissionLedger(ledger_path(self.config)).records(account_scope(self.config))
            self.assertEqual(records[-1]['ledger_state'], 'unknown')
            before = len([path for path, _, _ in server.calls if path == '/videos'])
            self.manager = TaskManager()
            second = self.run_batch(server)
            self.assertEqual(second['status'], 'submission_unknown')
            self.assertEqual(len([path for path, _, _ in server.calls if path == '/videos']), before)

    def test_500_429_and_200_without_id_remain_uncertain(self):
        self.config['task_strategy']['auto_retry'] = False
        with SafetyServer() as server:
            for status in (500, 429, 200):
                self.prompt.write_text(f'non definitive response {status}', encoding='utf-8')
                server.create_status = status
                server.response_payload = {'code': 'provider_failure', 'message': '暂时无法确认', 'data': None}
                with self.subTest(status=status):
                    task = self.run_batch(server)
                    self.assertEqual(task['status'], 'submission_unknown')
                    records = SubmissionLedger(ledger_path(self.config)).records(account_scope(self.config))
                    self.assertEqual(records[-1]['ledger_state'], 'unknown')

    def test_timeout_path_remains_submission_uncertain(self):
        client = ApiClient('http://fixture.invalid', 'local-fixture', log=lambda *args: None)
        client.request = Mock(side_effect=RequestError('连接超时'))
        params = dict(duration=8, aspect_ratio='16:9', resolution='720p', generate_audio=False)
        with self.assertRaises(SubmissionUncertain):
            client.create_task('video-v3', 'text', [], params)

    def test_precreation_classifier_rejects_only_explicit_quota_or_auth(self):
        nested = {
            'code': 'fail_to_fetch_task',
            'message': json.dumps({'code': 'insufficient_user_quota', 'message': '余额不足'}, ensure_ascii=False),
        }
        self.assertTrue(_is_definitive_precreation_rejection(403, nested))
        self.assertTrue(_is_definitive_precreation_rejection(401, {'code': 'invalid_api_key'}))
        self.assertFalse(_is_definitive_precreation_rejection(403, {'code': 'forbidden', 'message': 'WAF blocked'}))
        self.assertFalse(_is_definitive_precreation_rejection(500, nested))
        self.assertFalse(_is_definitive_precreation_rejection(403, dict(nested, task_id='already-created')))

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
            # 三次 429 触发暂停涉及累计 5s+10s 退避等待；重负载机器上需要更宽裕的等待上限。
            wait_until(lambda: self.manager.is_paused, timeout=90000)
            self.assertEqual(self.manager.worker.gate.limit, 1)
            self.assertEqual(len(server.calls), 4)
            self.assertEqual(self.manager.tasks[4]['status'], 'waiting')
            frozen = len(server.calls)
            before = server.polls.get('running-before-limit', 0)
            wait_until(lambda: server.polls.get('running-before-limit', 0) > before)
            self.assertEqual(len(server.calls), frozen)
            self.assertEqual(sum(task.get('task_id') == 'running-before-limit' for task in self.manager.tasks), 1)
            server.always_processing = False
            wait_until(lambda: any(task.get('task_id') == 'running-before-limit' and task.get('status') == 'completed'
                                   for task in self.manager.tasks), timeout=45000)
            self.assertEqual(len(server.calls), 4)
            self.manager.cancel_all()
            wait_until(lambda: not self.manager.is_running)


class RateLimitRecoveryHandler(FixtureHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls.append((self.path, dict(self.headers), body))
        if self.path == '/upload':
            return self.respond({'data': {'files': [{'url': self.server.base + '/image.png'}]}})
        posts = sum(1 for path, _, _ in self.server.calls if path != '/upload')
        if posts <= self.server.limit_until:
            return self.respond({'error': 'too many requests'}, 429)
        return self.respond({'data': {'task_id': 'recovered-%d' % posts}})


class RateLimitRecoveryServer(LocalServer):
    def __enter__(self):
        server = super().__enter__()
        server.RequestHandlerClass = RateLimitRecoveryHandler
        server.limit_until = 0
        return server


class GateRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_gate_pause_auto_recovers_and_escalates(self):
        clock = [0.0]
        gate = SubmissionGate(4, now=lambda: clock[0], probe_seconds=10)
        for _ in range(3):
            gate.failed(429)
        self.assertTrue(gate.paused)
        self.assertEqual(gate.limit, 1)
        self.assertAlmostEqual(gate.pause_remaining(), 10.0)
        clock[0] = 10.0
        self.assertTrue(gate.auto_resume())
        self.assertFalse(gate.paused)
        gate.failed(429)  # 试探失败 → 再次暂停，冷却翻倍
        self.assertTrue(gate.paused)
        self.assertAlmostEqual(gate.pause_remaining(), 20.0)
        clock[0] += 20.0
        self.assertTrue(gate.auto_resume())
        gate.succeeded()
        self.assertEqual(gate.limit, 2)

    def test_gate_wait_returns_after_probe_due(self):
        clock = [0.0]
        gate = SubmissionGate(2, now=lambda: clock[0], probe_seconds=5)

        class Control:
            def __init__(self):
                self.delays = 0

            def check(self):
                pass

            def delay(self, seconds):
                self.delays += 1
                clock[0] += max(.02, min(seconds, .25))

        for _ in range(3):
            gate.failed(429)
        control = Control()
        resumed = []
        gate.on_recover = lambda: resumed.append(clock[0])
        gate.wait(control)
        self.assertFalse(gate.paused)
        self.assertEqual(len(resumed), 1)
        self.assertGreaterEqual(resumed[0], 5.0)
        self.assertGreater(control.delays, 0)

    def test_stale_success_does_not_reopen_rate_limit(self):
        clock = [0.0]
        gate = SubmissionGate(4, now=lambda: clock[0], probe_seconds=10)

        class Control:
            def check(self):
                pass

            def delay(self, seconds):
                pass

            def before_task(self):
                pass

        with gate.permit(Control()) as seen:
            queued_epoch = seen
        for _ in range(3):
            gate.failed(429)
        self.assertTrue(gate.paused)
        self.assertFalse(gate.succeeded(queued_epoch))
        self.assertTrue(gate.paused)
        self.assertEqual(gate.limit, 1)
        self.assertEqual(gate.consecutive_429, 3)
        clock[0] = gate.paused_until
        self.assertTrue(gate.auto_resume())
        gate.succeeded()
        self.assertEqual(gate.limit, 2)
        self.assertEqual(gate.consecutive_429, 0)

    def test_rate_limited_queue_auto_recovers_without_manual_resume(self):
        temp = tempfile.TemporaryDirectory()
        try:
            root = Path(temp.name)
            prompts = root / 'prompts'
            prompts.mkdir()
            for index in range(1, 8):
                (prompts / f'{index}.txt').write_text(f'Recovery task {index}', encoding='utf-8')
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['prompt_detection']['enabled'] = False
            config['paths'].update(prompts=str(prompts), output=str(root / 'out'))
            config['workspace'].update(poll_interval=.01, duration=8)
            config['task_strategy'].update(unmatched_prompt='仍提交文生视频', retry_interval=.01, max_concurrency=2)
            config['_submission_ledger_path'] = str(root / 'submissions.sqlite3')
            config['_submission_probe_seconds'] = .2
            manager = TaskManager()
            logs = []
            manager.log_message.connect(lambda message, level: logs.append((level, message)))
            try:
                with RateLimitRecoveryServer() as server:
                    server.limit_until = 3
                    config['api'].update(base_url=server.base, api_key='***')
                    self.assertTrue(manager.start_tasks(config))
                    wait_until(lambda: not manager.is_running, timeout=30000)
                statuses = [t['status'] for t in manager.tasks]
                self.assertGreaterEqual(statuses.count('completed'), 2)
                self.assertGreaterEqual(statuses.count('submission_unknown'), 3)
                self.assertTrue(any('自动恢复提交' in message for _, message in logs))
                self.assertFalse(manager.is_paused)
            finally:
                manager.cancel_all()
                wait_until(lambda: not manager.is_running, timeout=15000)
        finally:
            temp.cleanup()


class SlowFirstSubmitHandler(FixtureHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls.append((self.path, dict(self.headers), body))
        if self.path == '/upload':
            return self.respond({'data': {'files': [{'url': self.server.base + '/image.png'}]}})
        posts = sum(1 for path, _, _ in self.server.calls if path != '/upload')
        if posts == 1:
            time.sleep(.6)  # 第一个提交放慢：把第二个同文案请求逼进 submitting 窗口
        return self.respond({'data': {'task_id': 'slow-%d' % posts}})


class SlowFirstSubmitServer(LocalServer):
    def __enter__(self):
        server = super().__enter__()
        server.RequestHandlerClass = SlowFirstSubmitHandler
        return server


class SamePromptSerializationTests(unittest.TestCase):
    """同文案、不同参考图的两个请求：必须串行化提交，而不是误判为待确认。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_different_requests_serialize_instead_of_false_unknown(self):
        temp = tempfile.TemporaryDirectory()
        try:
            root = Path(temp.name)
            prompts = root / 'prompts'
            prompts.mkdir()
            images = root / 'images'
            images.mkdir()
            (images / '1(1).png').write_bytes(b'good-image')
            (prompts / '1.txt').write_text('同一段文案。', encoding='utf-8')
            (prompts / '2.txt').write_text('同一段文案。', encoding='utf-8')
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['prompt_detection']['enabled'] = False
            config['paths'].update(prompts=str(prompts), images=str(images), output=str(root / 'out'))
            config['workspace'].update(poll_interval=.01, duration=8)
            config['task_strategy'].update(unmatched_prompt='仍提交文生视频', retry_interval=.01, max_concurrency=2)
            config['_submission_ledger_path'] = str(root / 'submissions.sqlite3')
            manager = TaskManager()
            try:
                with SlowFirstSubmitServer() as server:
                    config['api'].update(base_url=server.base, api_key='***', upload_url=server.base + '/upload')
                    self.assertTrue(manager.start_tasks(config))
                    wait_until(lambda: not manager.is_running, timeout=20000)
                posts = [path for path, _, _ in server.calls if path == '/videos']
                statuses = [t['status'] for t in manager.tasks]
                self.assertEqual(len(posts), 2, statuses)
                self.assertEqual(statuses, ['completed', 'completed'])
            finally:
                manager.cancel_all()
                wait_until(lambda: not manager.is_running, timeout=15000)
        finally:
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
