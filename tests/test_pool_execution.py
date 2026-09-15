"""Stage2B acceptance against real local HTTP, never a paid provider."""
import copy
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication
from core.config_manager import DEFAULT_CONFIG
from core.task_manager import TaskManager, TaskControl
from core.task_state import parameters_for_model
from core.model_parameters import GROK, validate_task_parameters
from test_http_clients import LocalServer, FixtureHandler
from test_task_manager import wait_until


class PoolHandler(FixtureHandler):
    def do_POST(self):
        if self.path == '/upload':
            if self.server.upload_errors:
                self.server.upload_errors -= 1
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.server.calls.append((self.path, dict(self.headers), body))
                return self.respond({'error': 'temporary image host failure'}, 503)
            return super().do_POST()
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        payload = json.loads(body)
        with self.server.lock:
            self.server.calls.append((self.path, dict(self.headers), body))
            model = payload['model']
            if model in self.server.fail_models:
                return self.respond({'error': 'model parameters rejected'}, self.server.failure_status)
            task_id = f'pool-{len(self.server.calls)}'
            self.server.jobs[task_id] = dict(model=model, started=time.monotonic(), polls=0)
            self.server.active.add(task_id)
            self.server.maximum = max(self.server.maximum, len(self.server.active))
            self.server.events.append(('submit', model, payload['prompt'], len(self.server.active)))
        self.respond({'task_id': task_id})

    def do_GET(self):
        if self.path == '/download' and self.server.download_errors:
            self.server.download_errors -= 1
            self.server.gets.append(self.path)
            return self.respond({'error': 'temporary download failure'}, 503)
        if not self.path.startswith('/videos/'):
            return super().do_GET()
        task_id = self.path.rsplit('/', 1)[1]
        self.server.gets.append(self.path)
        with self.server.lock:
            task = self.server.jobs[task_id]
            task['polls'] += 1
            if task['polls'] <= self.server.poll_errors:
                return self.respond({'error': 'temporary poll outage'}, 503)
            if task['model'] in self.server.remote_fail_models:
                self.server.active.discard(task_id)
                return self.respond({'status': 'failed', 'error': 'generation failed'})
            if time.monotonic() - task['started'] < self.server.processing_seconds:
                return self.respond({'status': 'processing', 'progress': 40})
            self.server.active.discard(task_id)
            self.server.events.append(('done', task['model'], task_id, len(self.server.active)))
        self.respond({'status': 'completed', 'progress': 100, 'result_url': self.server.base + '/download'})


class PoolServer(LocalServer):
    def __enter__(self):
        server = super().__enter__()
        server.RequestHandlerClass = PoolHandler
        server.lock = threading.Lock()
        server.jobs = {}; server.active = set(); server.events = []
        server.maximum = 0; server.fail_models = set(); server.remote_fail_models = set()
        server.failure_status = 422  # A definite rejection is safe for model failover.
        server.poll_errors = 0; server.processing_seconds = .1
        server.upload_errors = 0; server.download_errors = 0
        return server


class PoolExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prompts = self.root / 'prompts'; self.prompts.mkdir()
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config['prompt_detection']['enabled'] = False
        self.config['paths'] = dict(prompts=str(self.prompts), images='', output=str(self.root / 'output'))
        self.config['workspace'].update(model='video-v3', duration=10, resolution='720p', poll_interval=.02)
        self.config['task_strategy'].update(auto_retry=True, max_retries=5, retry_interval=.01,
                                            failure_skip_threshold=100, max_concurrency=1, unmatched_prompt='仍提交文生视频')
        self.config['model_pool'].update(enabled=True, strategy='轮询', auto_failover=True, cooldown=.05,
            models=[dict(name=m, enabled=True, status='健康') for m in ('video-v2', 'video-v2-fast', 'video-v3')])
        self.manager = TaskManager(); self.logs = []
        self.manager.log_message.connect(lambda message, level: self.logs.append((level, message)))

    def tearDown(self):
        self.manager.cancel_all()
        wait_until(lambda: not self.manager.is_running, timeout=10000)
        self.temp.cleanup()

    def make_prompts(self, count):
        for i in range(count):
            (self.prompts / f'{i+1:02d}.txt').write_text(f'prompt {i+1}', encoding='utf-8')

    def run_tasks(self, server, count=1):
        self.make_prompts(count)
        self.config['api'].update(base_url=server.base, api_key='local-fixture-only', upload_url=server.base + '/upload')
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running, timeout=30000)
        return self.manager.tasks

    def test_round_robin_three_tasks_use_three_actual_models(self):
        with PoolServer() as server:
            tasks = self.run_tasks(server, 3)
            self.assertEqual([json.loads(body)['model'] for path, _, body in server.calls if path == '/videos'],
                             ['video-v2', 'video-v2-fast', 'video-v3'])
            self.assertEqual([t['model'] for t in tasks], ['video-v2', 'video-v2-fast', 'video-v3'])
            self.assertEqual([t['status'] for t in tasks], ['completed'] * 3)

    def test_invalid_first_model_fails_over_without_sending_invalid_request(self):
        self.config['model_pool']['models'][0]['name'] = 'invalid-model'
        with PoolServer() as server:
            task = self.run_tasks(server)[0]
            self.assertEqual(task['model'], 'video-v2-fast')
            self.assertEqual(task['status'], 'completed')
            self.assertEqual(task['retry_count'], 1)
            self.assertEqual(len(server.calls), 1)
            self.assertTrue(any('invalid-model' in text and '冷却' in text for _, text in self.logs))

    def test_submit_error_switches_model_and_keeps_attempt_history(self):
        with PoolServer() as server:
            server.fail_models = {'video-v2'}
            task = self.run_tasks(server)[0]
            self.assertEqual(task['model'], 'video-v2-fast')
            self.assertEqual(task['status'], 'completed')
            self.assertEqual(task['retry_count'], 1)
            self.assertEqual([a['model'] for a in task['attempts']], ['video-v2', 'video-v2-fast'])

    def test_upload_retries_without_cooling_and_submit_retry_reuses_three_urls(self):
        images = self.root / 'images'; images.mkdir()
        for i in (3, 1, 2):
            (images / f'1({i}).jpg').write_bytes(b'good-image')
        self.config['paths']['images'] = str(images)
        with PoolServer() as server:
            server.upload_errors = 2  # Exhaust the uploader's existing one retry.
            server.fail_models = {'video-v2'}
            task = self.run_tasks(server)[0]
            self.assertEqual(task['status'], 'completed')
            self.assertEqual([a['phase'] for a in task['attempts'][:-1]], ['upload', 'submit'])
            self.assertEqual(task['attempts'][1]['model'], 'video-v2')
            bodies = [json.loads(body) for path, _, body in server.calls if path == '/videos']
            self.assertEqual([len(body['images']) for body in bodies], [3, 3])
            self.assertEqual(bodies[0]['images'], bodies[1]['images'])
            # Failed round: 2 attempts + 2 good images. Recovery: 3 uploads.
            # Switching video models after the submit error adds no upload.
            self.assertEqual(sum(path == '/upload' for path, _, _ in server.calls), 7)
            self.assertEqual(task['submitted_image_count'], 3)

    def test_download_retry_keeps_same_url_id_and_model(self):
        with PoolServer() as server:
            server.download_errors = 2
            task = self.run_tasks(server)[0]
            self.assertEqual(task['status'], 'completed')
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(server.gets.count('/download'), 3)
            self.assertEqual({a['task_id'] for a in task['attempts']}, {task['task_id']})
            self.assertEqual({a['model'] for a in task['attempts']}, {'video-v2'})

    def test_failover_restores_all_bound_images_for_model_with_larger_limit(self):
        images = self.root / 'images'; images.mkdir()
        for index in range(1, 11):
            (images / f'1({index}).png').write_bytes(f'good-image-{index}'.encode())
        self.config['paths']['images'] = str(images)
        self.config['model_pool']['models'] = [dict(name=name, enabled=True) for name in ('MiniMax-H3', 'video-v3')]
        with PoolServer() as server:
            server.fail_models = {'MiniMax-H3'}
            task = self.run_tasks(server)[0]
            self.assertEqual(task['status'], 'completed')
            payloads = [json.loads(body) for path, _, body in server.calls if path == '/videos']
            self.assertEqual([len(p['images']) for p in payloads], [9, 10])
            self.assertEqual(task['submitted_image_count'], 10)
            self.assertEqual(len(task['images']), 10)

    def test_failover_off_retries_same_model_and_retry_off_stops_immediately(self):
        self.config['model_pool']['auto_failover'] = False
        self.config['task_strategy']['max_retries'] = 2
        with PoolServer() as server:
            server.fail_models = {'video-v2'}
            task = self.run_tasks(server)[0]
            self.assertEqual(task['status'], 'failed')
            self.assertEqual([json.loads(body)['model'] for _, _, body in server.calls], ['video-v2'] * 3)
            self.config['task_strategy']['auto_retry'] = False
            self.config['model_pool']['auto_failover'] = True
            before = len(server.calls)
            task = self.run_tasks(server)[0]
            self.assertEqual(task['status'], 'failed')
            self.assertEqual(len(server.calls) - before, 1)
            self.assertEqual(task['retry_count'], 0)

    def test_refused_connection_pauses_creation_without_retry(self):
        self.make_prompts(1)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        self.config['api'].update(base_url=f'http://127.0.0.1:{port}', api_key='local-fixture-only')
        self.config['model_pool'].update(enabled=False, auto_failover=False)
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running, timeout=20000)
        task = self.manager.tasks[0]
        self.assertEqual(task['status'], 'submission_unknown')
        self.assertEqual(task['retry_count'], 0)
        self.assertEqual(len(task['attempts']), 1)
        self.assertEqual(sum('次重试，剩余' in text for _, text in self.logs), 0)

    def test_poll_transport_errors_keep_same_remote_task_id(self):
        with PoolServer() as server:
            server.poll_errors = 2
            task = self.run_tasks(server)[0]
            self.assertEqual(task['status'], 'completed')
            self.assertEqual(task['retry_count'], 2)
            self.assertEqual(len(server.calls), 1)
            self.assertEqual({path.rsplit('/', 1)[-1] for path in server.gets if path.startswith('/videos/')}, {task['task_id']})

    def test_remote_failed_status_allows_new_model_and_retains_failed_id(self):
        with PoolServer() as server:
            server.remote_fail_models = {'video-v2'}
            task = self.run_tasks(server)[0]
            self.assertEqual(task['status'], 'completed')
            self.assertEqual(task['model'], 'video-v2-fast')
            self.assertEqual(len(server.calls), 2)
            self.assertEqual(task['attempts'][0]['task_id'], 'pool-1')
            count = len(server.calls)
            self.manager.start_tasks(self.config)
            wait_until(lambda: not self.manager.is_running)
            self.assertEqual(len(server.calls), count)

    def test_two_workers_overlap_without_exceeding_limit_for_five_tasks(self):
        self.config['task_strategy']['max_concurrency'] = 2
        with PoolServer() as server:
            server.processing_seconds = .25
            tasks = self.run_tasks(server, 5)
            self.assertEqual(server.maximum, 2)
            self.assertEqual([t['status'] for t in tasks], ['completed'] * 5)
            self.assertEqual(len({t['task_id'] for t in tasks}), 5)

    def test_pool_parameter_adjustment_uses_supported_fields_without_dropping_images(self):
        self.config['workspace'].update(model='MiniMax-H3', duration=8, resolution='1080p', aspect_ratio='9:16')
        with PoolServer() as server:
            tasks = self.run_tasks(server, 3)
            bodies = [json.loads(body) for _, _, body in server.calls]
            self.assertEqual([p['duration'] for p in bodies], [10, 10, 8])
            self.assertEqual([p['resolution'] for p in bodies], ['1080p', '1080p', '720p'])
            self.assertEqual([t['status'] for t in tasks], ['completed'] * 3)

    def test_skip_is_scoped_to_worker_not_coordinator(self):
        control = TaskControl()
        ready = threading.Event(); done = threading.Event()
        def working():
            control.begin(1); ready.set(); done.wait(2); control.release(1)
        thread = threading.Thread(target=working); thread.start(); ready.wait(2)
        try:
            control.skip_for(1)
            control.check()
            self.assertFalse(control.skipped)
        finally:
            done.set(); thread.join(2)

    def test_concurrent_pause_drains_running_then_resume_and_cancel(self):
        self.make_prompts(5)
        self.config['task_strategy']['max_concurrency'] = 2
        with PoolServer() as server:
            server.processing_seconds = .4
            self.config['api'].update(base_url=server.base, api_key='local-fixture-only')
            self.manager.start_tasks(self.config)
            wait_until(lambda: len(server.calls) == 2)
            self.manager.pause_tasks()
            wait_until(lambda: sum(t['status'] == 'completed' for t in self.manager.tasks) == 2)
            self.assertEqual(len(server.calls), 2)
            self.manager.resume_tasks()
            wait_until(lambda: len(server.calls) == 4)
            self.manager.cancel_all()
            wait_until(lambda: not self.manager.is_running)
            self.assertEqual([t['status'] for t in self.manager.tasks], ['completed']*2 + ['cancelled']*3)

    def test_skip_selected_does_not_cancel_other_concurrent_tasks(self):
        self.make_prompts(3)
        self.config['task_strategy']['max_concurrency'] = 2
        with PoolServer() as server:
            server.processing_seconds = .3
            self.config['api'].update(base_url=server.base, api_key='local-fixture-only')
            self.manager.start_tasks(self.config)
            wait_until(lambda: len(self.manager.tasks) == 3 and all(t['status'] == 'processing' for t in self.manager.tasks[:2]))
            self.manager.select_current(1); self.manager.skip_current()
            wait_until(lambda: not self.manager.is_running)
            self.assertEqual([t['status'] for t in self.manager.tasks], ['completed', 'skipped', 'completed'])

    def test_cooldown_all_models_waits_and_recovers(self):
        self.config['model_pool']['models'] = [dict(name='video-v3', enabled=True, status='冷却中', cooldown_until=time.time()+.3)]
        with PoolServer() as server:
            tasks = self.run_tasks(server)
            self.assertEqual(tasks[0]['status'], 'completed')
            self.assertTrue(any('恢复可用' in text for _, text in self.logs))

    def test_initial_cooldown_wait_can_skip_only_that_task(self):
        self.make_prompts(2)
        with PoolServer() as server:
            for concurrency in (1, 2):
                with self.subTest(concurrency=concurrency):
                    self.config['task_strategy']['max_concurrency'] = concurrency
                    self.config['model_pool']['models'] = [dict(name='video-v3', enabled=True, status='冷却中', cooldown_until=time.time()+30)]
                    self.config['api'].update(base_url=server.base, api_key='local-fixture-only')
                    self.manager.start_tasks(self.config)
                    wait_until(lambda: self.manager.tasks and self.manager.tasks[0]['status'] == 'cooling')
                    self.manager.select_current(0); self.manager.skip_current()
                    wait_until(lambda: self.manager.tasks[0]['status'] == 'skipped')
                    self.assertTrue(self.manager.is_running)
                    self.manager.cancel_all(); wait_until(lambda: not self.manager.is_running)
                    self.assertEqual(self.manager.tasks[1]['status'], 'cancelled')
                    self.assertEqual(server.calls, [])

    def test_grok_pool_adapts_multi_image_resolution_but_single_model_stays_strict(self):
        values = dict(duration=8, aspect_ratio='9:16', resolution='1080p')
        pooled = parameters_for_model(GROK, values, pooled=True, image_count=3)
        self.assertEqual(pooled['resolution'], '720p')
        validate_task_parameters(GROK, 'product', pooled, 3)
        strict = parameters_for_model(GROK, values, pooled=False, image_count=3)
        with self.assertRaises(ValueError):
            validate_task_parameters(GROK, 'product', strict, 3)

    def test_global_concurrency_crosses_products_and_keeps_output_folders(self):
        # 全队列并发（默认 5）：所有产品共享一个队伍，不再有产品边界等待。
        self.config['task_strategy']['max_concurrency'] = 5
        images = self.root / 'images'; images.mkdir()
        self.config['paths']['images'] = str(images)
        for product in ['A产品', 'B产品']:
            (self.prompts / product).mkdir(); (images / product).mkdir()
            for i in range(2):
                (self.prompts / product / f'{i+1}.txt').write_text(f'{product} 场景{i+1}', encoding='utf-8')
        with PoolServer() as server:
            server.processing_seconds = .5
            tasks = self.run_tasks(server, count=0)
            self.assertEqual([t['status'] for t in tasks], ['completed'] * 4)
            # 四个任务先全部提交（并发跨产品），完成后各自归档到所属产品目录。
            self.assertEqual([e[0] for e in server.events][:4], ['submit'] * 4)
            self.assertEqual(server.maximum, 4)
            self.assertEqual([Path(t['result_path']).parent.name for t in tasks], ['A产品']*2 + ['B产品']*2)


if __name__ == '__main__':
    unittest.main()
