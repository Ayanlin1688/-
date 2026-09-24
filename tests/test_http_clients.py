"""Real HTTP against a local fixture; never represents a paid provider run."""
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.api_client import ApiClient, GROK, V3_MODELS
from core.image_uploader import ImageUploader, upload_credentials, DEFAULT_UPLOAD_URL
from core.video_downloader import VideoDownloader
from core.http_client import RequestError, Cancelled, HttpClient


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls.append((self.path, dict(self.headers), body))
        if self.path == '/upload':
            if b'bad-image' in body:
                self.respond({'error': 'broken upload'}, 500)
            else:
                self.respond({'data': {'files': [{'url': self.server.base + '/image.png'}]}})
        elif self.server.reject:
            self.respond({'error': 'invalid secret-test'}, 401)
        else:
            number = sum(1 for path, _, _ in self.server.calls if path != '/upload')
            self.respond({'id': 'wrong-priority', 'data': {'task_id': 'task-' + str(number)}})

    def do_GET(self):
        self.server.gets.append(self.path)
        if self.path == '/download' or self.path == '/truncated':
            data = b'local-video-fixture' * 10000
            self.send_response(200)
            self.send_header('Content-Length', str(len(data) + (50 if self.path == '/truncated' else 0)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == '/resume':
            data = b'R' * 300000
            start = 0
            range_header = self.headers.get('Range')
            if range_header:
                self.server.ranges.append(range_header)
                start = int(range_header.split('=', 1)[1].split('-', 1)[0])
            if start == 0:
                part = data[:65536]
                self.close_connection = True
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(part)
                return
            rest = data[start:]
            self.send_response(206)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Range', f'bytes {start}-{len(data) - 1}/{len(data)}')
            self.send_header('Content-Length', str(len(rest)))
            self.end_headers()
            self.wfile.write(rest)
        elif self.path == '/models' or 'connection-check' in self.path:
            self.respond({'data': []}, 401 if self.server.reject else 200)
        else:
            task = self.path.split('/')[-1]
            n = self.server.polls.get(task, 0) + 1
            self.server.polls[task] = n
            if self.server.always_processing or n == 1:
                self.respond({'data': {'status': 'PROCESSING', 'progress': '45%'}})
            elif getattr(self.server, 'suppress_result_url', 0) > 0:
                self.server.suppress_result_url -= 1
                self.respond({'data': {'status': 'succeeded', 'progress': 100}})
            else:
                self.respond({'data': {'status': 'succeeded', 'progress': 100,
                                      'result_url': self.server.base + '/download',
                                      'video_url': 'wrong-priority'}})


class LocalServer:
    def __enter__(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        self.server.calls = []; self.server.gets = []; self.server.polls = {}
        self.server.reject = False; self.server.always_processing = False
        self.server.ranges = []; self.server.suppress_result_url = 0
        self.server.base = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *args):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


class HttpTests(unittest.TestCase):
    def test_custom_image_host_does_not_receive_provider_key(self):
        self.assertEqual(upload_credentials(dict(api_key='provider-key')), (DEFAULT_UPLOAD_URL, 'provider-key'))
        self.assertEqual(upload_credentials(dict(api_key='provider-key', upload_url='https://custom.invalid/upload')),
                         ('https://custom.invalid/upload', ''))
        self.assertEqual(upload_credentials(dict(api_key='provider-key', upload_url='https://custom.invalid/upload', upload_api_key='image-key')),
                         ('https://custom.invalid/upload', 'image-key'))

    def test_all_requests_use_connect_and_read_timeouts(self):
        session = Mock()
        session.request.return_value.status_code = 200
        client = HttpClient(' key-with-surrounding-spaces ', session=session)
        client.request('GET', 'http://localhost/example')
        self.assertEqual(session.request.call_args.kwargs['timeout'], (10, 30))
        self.assertEqual(session.request.call_args.kwargs['headers']['Authorization'], 'Bearer key-with-surrounding-spaces')

    def test_every_model_payload_and_query_auth(self):
        with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '图片.png'; path.write_bytes(b'actual-image-bytes')
            client = ApiClient(' ' + server.base + '/ ', ' secret-test ', log=lambda *args: None)
            params = dict(duration=10, aspect_ratio='9:16', resolution='720p', generate_audio=False)
            for model in ['video-v1', 'video-v2', 'video-v2-fast', *sorted(V3_MODELS), 'MiniMax-H3', GROK]:
                values = dict(params, resolution='4K') if model == 'MiniMax-H3' else params
                task = client.create_task(model, '@参考图1 汽车', [str(path)] if model == GROK else ['https://image'], values)
                self.assertTrue(task.startswith('task-'))
                endpoint, headers, body = server.calls[-1]
                self.assertEqual(headers['Authorization'], 'Bearer secret-test')
                self.assertEqual(headers['Accept'], 'application/json')
                self.assertEqual(endpoint, '/video/generations' if model == 'video-v1' else '/videos')
                if model == GROK:
                    self.assertIn('multipart/form-data', headers['Content-Type'])
                    self.assertIn(b'name="input_reference"', body)
                    self.assertIn(b'actual-image-bytes', body)
                else:
                    payload = json.loads(body)
                    self.assertEqual(payload['images'], ['https://image'])
                    self.assertEqual(payload['prompt'], '@Image1 汽车' if model in {'video-v2', 'video-v2-fast'} else '@参考图1 汽车')
                    if model in V3_MODELS:
                        self.assertEqual(payload['resolution'], '720p')
                        self.assertEqual(payload['ratio'], '9:16')
                        self.assertNotIn('aspect_ratio', payload)
                    elif model == 'MiniMax-H3':
                        self.assertEqual(payload['workflow_id'], 'multi-reference')
                        self.assertEqual(payload['seconds'], 10)
                        self.assertEqual(payload['aspect_ratio'], '9:16')
                    else:
                        self.assertEqual(payload['duration'], 10)
                self.assertEqual(client.query_task(task, model)['progress'], 45)
                final = client.query_task(task, model)
                self.assertEqual(final['status'], 'completed')
                self.assertEqual(final['result_url'], server.base + '/download')
            client.create_task(GROK, 'text only', [], params)
            self.assertIn('multipart/form-data', server.calls[-1][1]['Content-Type'])
            client.create_task('MiniMax-H3', 'text', [], dict(params, resolution='768p'))
            self.assertEqual(json.loads(server.calls[-1][2])['workflow_id'], 'text-to-video')
            self.assertTrue(client.test_connection())
            server.reject = True
            self.assertFalse(client.test_connection())
            with self.assertRaisesRegex(RequestError, 'HTTP 401') as caught:
                client.create_task('video-v1', 'text', [], params)
            self.assertNotIn('secret-test', str(caught.exception))
            client.close()

    def test_upload_order_download_skip_truncation_and_cancel(self):
        with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = root / '好.png'; good.write_bytes(b'good-image')
            bad = root / '坏.png'; bad.write_bytes(b'bad-image')
            uploader = ImageUploader(server.base + '/upload', log=lambda *args: None)
            urls = uploader.upload_images([good, bad, good])
            self.assertEqual(urls, [server.base + '/image.png', None, server.base + '/image.png'])
            self.assertIn(b'name="files"', server.calls[0][2])
            downloader = VideoDownloader(log=lambda *args: None)
            target = Path(downloader.download_video(server.base + '/download', root, '003_汽车.mp4'))
            self.assertEqual(target.read_bytes(), b'local-video-fixture' * 10000)
            gets = len(server.gets)
            downloader.download_video(server.base + '/download', root, target.name)
            self.assertEqual(len(server.gets), gets)
            downloader.overwrite_existing = True
            with self.assertRaises(Exception):
                downloader.download_video(server.base + '/truncated', root, target.name)
            self.assertEqual(target.stat().st_size, 190000)
            count = [0]
            def cancel():
                count[0] += 1
                if count[0] > 1:
                    raise Cancelled()
            downloader.check_cancel = cancel
            with self.assertRaises(Cancelled):
                downloader.download_video(server.base + '/download', root, 'cancelled.mp4')
            self.assertFalse((root / 'cancelled.mp4').exists())
            self.assertFalse(list(root.glob('*.part')))
            downloader.check_cancel = lambda: None
            resumed = Path(downloader.download_video(server.base + '/resume', root, '断点.mp4'))
            self.assertEqual(resumed.read_bytes(), b'R' * 300000)
            self.assertTrue(server.ranges)
            self.assertTrue(server.ranges[0].startswith('bytes='))
            uploader.close(); downloader.close()


if __name__ == '__main__':
    unittest.main()
