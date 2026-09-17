"""Real localhost HTTP matrix; never a paid API or playable-media validation.

Hold the first remote wave in processing until the requested active count has
been reached. POST responses always return promptly, so serialized admission
cannot deadlock the fixture. Unique image URLs and download bytes expose both
cross-task reference leakage and accidental output reuse.
"""
import copy
import json
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication

from core.config_manager import DEFAULT_CONFIG
from core.task_manager import TaskManager
from test_task_manager import wait_until


class MatrixHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, payload, status=200, content_type='application/json'):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        server = self.server
        with server.lock:
            server.posts_in_flight += 1
            server.max_posts_in_flight = max(server.max_posts_in_flight, server.posts_in_flight)
        try:
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            # Enlarge the request service window to detect unsynchronized POSTs.
            # This delay never waits for another request to arrive.
            time.sleep(.025)
            with server.lock:
                server.posts.append(self.path)
                if self.path == '/upload':
                    tokens = re.findall(rb'MATRIX_IMAGE\[([A-Z][0-9]+:[0-9]+)\]', body)
                    if len(tokens) != 1:
                        result, status = {'error': 'unexpected upload body'}, 422
                    else:
                        token = tokens[0].decode()
                        url = f'{server.base}/images/{token}.png'
                        server.uploads.append(token)
                        result, status = {'data': {'files': [{'url': url}]}}, 200
                elif self.path == '/videos':
                    payload = json.loads(body)
                    task_id = f'matrix-{len(server.jobs) + 1}'
                    server.jobs[task_id] = {'payload': payload, 'polls': 0}
                    server.active.add(task_id)
                    server.max_active = max(server.max_active, len(server.active))
                    if len(server.active) == server.target:
                        server.wave_reached = True
                    result, status = {'task_id': task_id}, 200
                else:
                    result, status = {'error': 'unknown endpoint'}, 404
        finally:
            # End the processing window before the client sees the response.
            # Counting handler teardown would report spurious overlap after a
            # fully delivered response when the next request is legitimate.
            with server.lock:
                server.posts_in_flight -= 1
        self.respond(result, status)

    def do_GET(self):
        server = self.server
        with server.lock:
            server.gets.append(self.path)
            if self.path.startswith('/videos/'):
                task_id = self.path.rsplit('/', 1)[1]
                job = server.jobs[task_id]
                job['polls'] += 1
                if not server.wave_reached or job['polls'] == 1:
                    response = {'status': 'processing', 'progress': 30}
                else:
                    server.active.discard(task_id)
                    response = {'status': 'completed', 'progress': 100,
                                'result_url': f'{server.base}/download/{task_id}'}
                content_type = 'application/json'
            elif self.path.startswith('/download/'):
                task_id = self.path.rsplit('/', 1)[1]
                response = server.video_bytes(task_id)
                content_type = 'video/mp4'
            else:
                return self.respond({'error': 'unknown endpoint'}, 404)
        self.respond(response, content_type=content_type)


class MatrixServer:
    def __init__(self, concurrency):
        self.concurrency = concurrency

    def __enter__(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), MatrixHandler)
        server.base = f'http://127.0.0.1:{server.server_port}'
        server.lock = threading.Lock()
        server.target = self.concurrency
        server.wave_reached = False
        server.active, server.jobs = set(), {}
        server.posts, server.gets, server.uploads = [], [], []
        server.max_active = server.posts_in_flight = server.max_posts_in_flight = 0
        server.video_bytes = lambda task_id: (
            b'local-http-matrix-fixture\x00' + task_id.encode() + b'\x00' +
            server.jobs[task_id]['payload']['prompt'].encode()) * 100
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        return server

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ConcurrencyMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def run_matrix_case(self, concurrency):
        with tempfile.TemporaryDirectory() as temp, MatrixServer(concurrency) as server:
            root = Path(temp)
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['paths'] = {key: str(root / key) for key in ('prompts', 'images', 'output')}
            config['_submission_ledger_path'] = str(root / 'ledger.sqlite3')
            config['api'].update(base_url=server.base, api_key='local-matrix-only',
                                 upload_url=server.base + '/upload')
            config['workspace'].update(model='MiniMax-H3', resolution='768p', duration=8,
                                       aspect_ratio='9:16', poll_interval=.02, poll_timeout=20)
            config['prompt_detection']['enabled'] = False
            config['model_pool']['enabled'] = False
            config['task_strategy'].update(max_concurrency=concurrency, auto_retry=False,
                                           unmatched_prompt='跳过并警告')
            expected = {}
            for product, count in (('A产品', 3), ('B产品', 2)):
                prompts = root / 'prompts' / product
                images = root / 'images' / product
                prompts.mkdir(parents=True)
                images.mkdir(parents=True)
                for number in range(1, count + 1):
                    marker = f'{product[0]}{number}'
                    prompt = f'MATRIX_PROMPT_{marker}'
                    (prompts / f'{number}.txt').write_text(prompt, encoding='utf-8')
                    for view in (10, 2, 1):
                        (images / f'{number}({view}).png').write_bytes(
                            f'MATRIX_IMAGE[{marker}:{view}]'.encode())
                    expected[prompt] = [f'{server.base}/images/{marker}:{view}.png' for view in (1, 2, 10)]

            manager = TaskManager()
            try:
                manager.start_tasks(config)
                wait_until(lambda: not manager.is_running, timeout=25000)
                tasks = manager.tasks
                self.assertEqual([task['status'] for task in tasks], ['completed'] * 5,
                                 [(task['status'], task.get('error')) for task in tasks])
                self.assertTrue(server.wave_reached)
                self.assertEqual(server.max_active, concurrency)
                self.assertEqual(server.max_posts_in_flight, 1)
                self.assertEqual(len(server.jobs), 5)
                self.assertEqual(len(server.uploads), 15)
                self.assertEqual(len(set(server.uploads)), 15)
                self.assertEqual(len({task['task_id'] for task in tasks}), 5)
                self.assertEqual(len({task['result_path'] for task in tasks}), 5)
                for task in tasks:
                    job = server.jobs[task['task_id']]
                    payload = job['payload']
                    self.assertEqual(payload['images'], expected[payload['prompt']])
                    self.assertEqual(task['submitted_image_count'], 3)
                    self.assertGreaterEqual(job['polls'], 2)
                    output = Path(task['result_path'])
                    self.assertEqual(output.parent.name, task['product'])
                    self.assertEqual(output.read_bytes(), server.video_bytes(task['task_id']))

                original_ids = [task['task_id'] for task in tasks]
                original_outputs = [task['result_path'] for task in tasks]
                requests_before = (len(server.posts), len(server.gets))
                # Fresh manager, same durable ledger: no new upload, creation,
                # polling or downloading for already completed owned outputs.
                manager = TaskManager()
                manager.start_tasks(config)
                wait_until(lambda: not manager.is_running, timeout=10000)
                self.assertEqual([task['status'] for task in manager.tasks], ['duplicate'] * 5)
                self.assertEqual([task['task_id'] for task in manager.tasks], original_ids)
                self.assertEqual([task['result_path'] for task in manager.tasks], original_outputs)
                self.assertEqual((len(server.posts), len(server.gets)), requests_before)
            finally:
                manager.cancel_all()
                wait_until(lambda: not manager.is_running, timeout=10000)

    def test_two_concurrent_five_tasks(self):
        self.run_matrix_case(2)

    def test_three_concurrent_five_tasks(self):
        self.run_matrix_case(3)

    def test_four_concurrent_five_tasks(self):
        self.run_matrix_case(4)

    def test_five_concurrent_five_tasks(self):
        self.run_matrix_case(5)


if __name__ == '__main__':
    unittest.main()
