import base64
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from threading import Lock
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from core.ecommerce.image_client import EcommerceImageError, ImageResult
from core.manual_image import ManualImageError, ManualImageManager, ManualImageStore


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j9V8AAAAASUVORK5CYII=')


def _result(model='test-image'):
    return ImageResult(PNG, model, '1024x1024', '1K', 'auto', '/images/generations')


class _RelayHandler(BaseHTTPRequestHandler):
    capture_lock = Lock()
    requests_seen = []
    response_status = 200
    response_payload = {'candidates': [{'content': {'parts': [{'inlineData': {
        'mimeType': 'image/png', 'data': base64.b64encode(PNG).decode('ascii')}}]}}]}

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        raw = self.rfile.read(length)
        with self.capture_lock:
            self.requests_seen.append({
                'path': self.path,
                'headers': {key.lower(): value for key, value in self.headers.items()},
                'body': raw,
                'json': None,
            })
            if self.headers.get('Content-Type', '').startswith('application/json'):
                try:
                    self.requests_seen[-1]['json'] = json.loads(raw.decode('utf-8'))
                except (ValueError, UnicodeDecodeError):
                    pass
        payload = self.response_payload
        encoded = json.dumps(payload).encode('utf-8')
        self.send_response(self.response_status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        return


def _stop_server(server, thread):
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


class ManualImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ManualImageStore(self.root / 'manual.sqlite3')
        self.config = SimpleNamespace(
            path=self.root / 'config.json',
            config={
                'api': {'base_url': 'https://relay.example/v1', 'api_key': 'fixture-secret'},
                'paths': {'output': str(self.root / 'output')},
            },
        )
        self.callbacks = []
        self.manager = ManualImageManager(self.config, self.store,
                                          on_task=lambda item: self.callbacks.append(item))

    def tearDown(self):
        self.manager.close()
        self.store.close()
        self.temp.cleanup()

    def _tasks(self, count=1, model='fixture-image', refs=()):
        return self.manager.prepare_tasks(
            'A white ceramic cup on a clean background', model, model_version=model,
            ratio='1:1', tier='1K', count=count, reference_paths=refs)

    def test_store_round_trip_sort_count_and_delete(self):
        first = {'local_id': 'older', 'created_at': 10, 'status': 'pending', 'files': []}
        second = {'local_id': 'newer', 'created_at': 20, 'status': 'succeeded', 'files': ['x.png']}
        self.store.upsert(first)
        self.store.upsert(second)
        self.store.close()

        reopened = ManualImageStore(self.root / 'manual.sqlite3')
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.persistence_available)
        self.assertEqual([item['local_id'] for item in reopened.list()], ['newer', 'older'])
        self.assertEqual(reopened.count('succeeded'), 1)
        self.assertTrue(reopened.delete('older'))
        self.assertFalse(reopened.delete('missing'))
        self.assertEqual(reopened.count(), 1)

    def test_store_degrades_to_memory_when_database_is_unavailable(self):
        with mock.patch.object(ManualImageStore, '_connect',
                               side_effect=sqlite3.OperationalError('read only')):
            store = ManualImageStore(self.root / 'disabled.sqlite3')
        self.addCleanup(store.close)
        self.assertFalse(store.persistence_available)
        saved = store.upsert({'local_id': 'memory', 'created_at': 1,
                              'status': 'pending', 'files': []})
        self.assertEqual(saved['local_id'], 'memory')
        self.assertEqual(store.list()[0]['local_id'], 'memory')

    def test_openai_task_forwards_parameters_persists_file_and_publishes_states(self):
        task = self._tasks(refs=['one.png', 'two.webp'])[0]
        with mock.patch('core.manual_image.image_client.generate_image',
                        return_value=_result()) as generate:
            result = self.manager.run_tasks([task])[0]

        self.assertEqual(generate.call_args.args,
                         ('https://relay.example/v1', 'fixture-secret', 'fixture-image',
                          task['prompt'], '1:1', '1K'))
        self.assertEqual(generate.call_args.kwargs, {'reference_paths': ['one.png', 'two.webp']})
        self.assertEqual([item['status'] for item in self.callbacks],
                         ['pending', 'running', 'succeeded'])
        self.assertEqual(task['status'], 'pending')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['protocol'], 'openai')
        path = Path(result['files'][0])
        self.assertIn('单独生图', str(path))
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), PNG)

    def test_gemini_request_headers_body_and_inline_image_decode(self):
        _RelayHandler.requests_seen = []
        _RelayHandler.response_status = 200
        _RelayHandler.response_payload = {
            'candidates': [{'content': {'parts': [
                {'text': 'done'},
                {'inlineData': {'mimeType': 'image/png',
                                'data': base64.b64encode(PNG).decode('ascii')}},
            ]}}]
        }
        server = ThreadingHTTPServer(('127.0.0.1', 0), _RelayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(_stop_server, server, thread)
        self.config.config['api']['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
        self.config.config['image_model_pool'] = {
            'models': [{'name': 'gemini-2.5-image', 'protocol': 'gemini'}]}

        task = self._tasks(model='gemini-2.5-image')[0]
        result = self.manager.run_tasks([task])[0]

        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['protocol'], 'gemini')
        self.assertEqual(Path(result['files'][0]).read_bytes(), PNG)
        request = _RelayHandler.requests_seen[0]
        self.assertEqual(request['path'], '/v1/models/gemini-2.5-image:generateContent')
        self.assertEqual(request['headers']['authorization'], 'Bearer fixture-secret')
        self.assertEqual(request['headers']['x-goog-api-key'], 'fixture-secret')
        self.assertEqual(request['json']['contents'][0]['parts'][0]['text'], task['prompt'])
        self.assertEqual(request['json']['generationConfig']['responseModalities'], ['TEXT', 'IMAGE'])
        self.assertEqual(request['json']['generationConfig']['imageConfig']['aspectRatio'], '1:1')

    def test_gemini_empty_candidates_become_chinese_failure(self):
        _RelayHandler.requests_seen = []
        _RelayHandler.response_status = 200
        _RelayHandler.response_payload = {'candidates': []}
        server = ThreadingHTTPServer(('127.0.0.1', 0), _RelayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(_stop_server, server, thread)
        self.config.config['api']['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
        self.config.config['image_model_pool'] = {
            'models': [{'name': 'gemini-2.5-image', 'protocol': 'gemini'}]}

        result = self.manager.run_tasks(self._tasks(model='gemini-2.5-image'))[0]
        self.assertEqual(result['status'], 'failed')
        self.assertIn('没有返回生成图片', result['error'])
        self.assertFalse(result['files'])

    def test_openai_unsupported_endpoint_falls_back_to_gemini_once(self):
        task = self._tasks(model='provider-image')[0]
        unsupported = EcommerceImageError('unsupported endpoint', 404,
                                          'unsupported endpoint', retry_safe=False)
        with mock.patch('core.manual_image.image_client.generate_image', side_effect=unsupported) as openai, \
                mock.patch('core.manual_image.generate_gemini_image', return_value=_result()) as gemini:
            result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['protocol'], 'gemini')
        self.assertEqual(openai.call_count, 1)
        self.assertEqual(gemini.call_count, 1)

    def test_authentication_and_content_policy_do_not_fallback(self):
        for status, body in ((401, 'invalid api key'), (400, 'content policy violation')):
            with self.subTest(status=status):
                task = self._tasks(model='provider-image')[0]
                error = EcommerceImageError('request rejected', status, body, retry_safe=False)
                with mock.patch('core.manual_image.image_client.generate_image', side_effect=error), \
                        mock.patch('core.manual_image.generate_gemini_image') as gemini:
                    result = self.manager.run_tasks([task])[0]
                self.assertEqual(result['status'], 'failed')
                self.assertTrue(result['error'])
                self.assertEqual(gemini.call_count, 0)

    def test_two_protocol_failures_leave_a_chinese_failed_record(self):
        task = self._tasks(model='provider-image')[0]
        unsupported = EcommerceImageError('unsupported endpoint', 404,
                                          'unsupported endpoint', retry_safe=False)
        with mock.patch('core.manual_image.image_client.generate_image', side_effect=unsupported), \
                mock.patch('core.manual_image.generate_gemini_image',
                           side_effect=ManualImageError('上游服务暂时不可用', 503,
                                                       category='upstream')):
            result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['protocol'], 'gemini')
        self.assertIn('上游服务暂时不可用', result['error'])

    def test_openai_sends_all_eight_reference_images(self):
        _RelayHandler.requests_seen = []
        _RelayHandler.response_status = 200
        _RelayHandler.response_payload = {'data': [{'b64_json': base64.b64encode(PNG).decode('ascii')}]}
        server = ThreadingHTTPServer(('127.0.0.1', 0), _RelayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(_stop_server, server, thread)
        self.config.config['api']['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
        refs = []
        for index in range(8):
            path = self.root / f'ref-{index}.png'
            path.write_bytes(b'fixture reference')
            refs.append(str(path))

        result = self.manager.run_tasks(self._tasks(model='provider-image', refs=refs))[0]
        self.assertEqual(result['status'], 'succeeded')
        request = _RelayHandler.requests_seen[0]
        self.assertEqual(request['path'], '/v1/images/edits')
        self.assertEqual(request['body'].count(b'name="image"'), 8)

    def test_generation_concurrency_is_bounded_to_two(self):
        tasks = self._tasks(count=6)
        active = 0
        peak = 0
        lock = Lock()

        def generate(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return _result()

        with mock.patch('core.manual_image.image_client.generate_image', side_effect=generate):
            results = self.manager.run_tasks(tasks)
        self.assertTrue(all(item['status'] == 'succeeded' for item in results))
        self.assertLessEqual(peak, 2)
        self.assertEqual(peak, 2)

    def test_repeated_tasks_use_isolated_output_directories(self):
        tasks = self._tasks(count=2)
        with mock.patch('core.manual_image.image_client.generate_image', return_value=_result()):
            results = self.manager.run_tasks(tasks)
        paths = [Path(item['files'][0]) for item in results]
        self.assertNotEqual(paths[0].parent, paths[1].parent)
        self.assertTrue(all(path.is_file() for path in paths))

    def test_idle_cancel_does_not_cancel_next_batch(self):
        self.manager.cancel()
        task = self._tasks()[0]
        with mock.patch('core.manual_image.image_client.generate_image', return_value=_result()):
            result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'succeeded')

    def test_cancellation_marks_waiting_tasks_and_notifies_history(self):
        tasks = self._tasks(count=3)
        self.callbacks.clear()
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch('core.manual_image.image_client.generate_image') as generate:
            results = self.manager.run_tasks(tasks, cancel_event=cancelled)
        self.assertTrue(all(item['status'] == 'cancelled' for item in results))
        self.assertEqual([item['status'] for item in self.callbacks],
                         ['cancelled', 'cancelled', 'cancelled'])
        self.assertEqual(generate.call_count, 0)


if __name__ == '__main__':
    unittest.main()
