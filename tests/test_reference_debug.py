"""Real local image uploads and request inspection for optional diagnostics."""
import copy
from email.parser import BytesParser
from email import policy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtGui import QImage, QColor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from core.api_client import ApiClient
from core.config_manager import ConfigManager, DEFAULT_CONFIG
from core.prompt_processor import process_prompt
from core.reference_diagnostics import ReferenceDiagnostics
from core.task_manager import TaskWorker
from test_http_clients import FixtureHandler
from ui.main_window import MainWindow


class ReferenceHandler(FixtureHandler):
    def do_POST(self):
        if self.path != '/upload':
            return super().do_POST()
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.server.calls.append((self.path, dict(self.headers), body))
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + self.headers['Content-Type'] + '\r\n\r\n').encode() + body)
        part = list(message.iter_parts())[0]
        self.server.uploads.append(part.get_payload(decode=True))
        self.respond({'files': [{'url': self.server.base + '/reference/' + str(len(self.server.uploads))}]})

    def do_GET(self):
        if self.path == '/redirect-reference':
            self.server.reference_headers.append(dict(self.headers))
            self.send_response(302)
            self.send_header('Location', '/reference/1')
            self.send_header('Set-Cookie', 'implicit-secret=redirect-value; Path=/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if not self.path.startswith('/reference/'):
            return super().do_GET()
        self.server.gets.append(self.path)
        self.server.reference_headers.append(dict(self.headers))
        if self.server.reference_failure:
            return self.respond({'error': 'probe unavailable'}, 503)
        data = self.server.changed_image or self.server.uploads[int(self.path.rsplit('/', 1)[1]) - 1]
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Set-Cookie', 'implicit-secret=response-value; Path=/')
        self.end_headers(); self.wfile.write(data)


class ReferenceDebugTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ReferenceHandler)
        self.server.base = f'http://127.0.0.1:{self.server.server_port}'
        self.server.calls = []; self.server.gets = []; self.server.polls = {}
        self.server.reject = False; self.server.always_processing = False
        self.server.uploads = []; self.server.reference_headers = []
        self.server.changed_image = None; self.server.reference_failure = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.paths = []
        for index in range(3):
            path = self.root / f'参考 {index+1}.png'
            image = QImage(80 + index, 120 + index, QImage.Format_RGB32)
            image.fill(QColor(30 + index * 60, 40, 90)); image.save(str(path)); self.paths.append(str(path))
        self.prompt = self.root / '玫瑰毯子1.txt'
        self.prompt.write_text('<Picture 1 > <Picture2> @Image3 <Picture 5>', encoding='utf-8')
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config['_submission_ledger_path'] = str(self.root / 'submissions.sqlite3')
        self.config['prompt_detection']['enabled'] = False  # Exercise H3 image diagnostics, not format detection.
        self.config['paths'] = dict(prompts=str(self.root), images=str(self.root), output=str(self.root / 'out'))
        self.config['api'].update(base_url=self.server.base, api_key='secret-video-key', upload_api_key='secret-upload-key', upload_url=self.server.base + '/upload')
        self.config['workspace'].update(model='MiniMax-H3', resolution='1080p', duration=8, poll_interval=.01)
        self.config['match_overrides'] = {str(self.prompt.resolve()): self.paths}
        self.config['diagnostics'] = dict(debug_mode=True)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def run_worker(self):
        worker = TaskWorker(self.config, []); logs = []
        worker.log_message.connect(lambda message, level: logs.append((message, level)))
        worker.run()
        # 全队列并发后任务在池线程执行，日志信号排队到主线程；泵一次事件循环收齐日志。
        QTest.qWait(120)
        return worker, logs

    def test_marker_whitespace_multidigit_and_existing_markers(self):
        self.assertEqual(process_prompt('<Picture 1> <Picture1> <Picture 1 > <Picture10> @Image10 @Image1 @参考图2'),
                         '@参考图1 @参考图1 @参考图1 @参考图10 @参考图10 @参考图1 @参考图2')

    def test_three_image_debug_order_integrity_prompt_and_range_warning(self):
        worker, logs = self.run_worker()
        debug = '\n'.join(message for message, level in logs if level == 'debug')
        self.assertIn('绑定3张参考图', debug)
        self.assertIn('玫瑰毯子1.txt', debug)
        self.assertIn('80x120', debug)
        self.assertIn('image/png', debug)
        self.assertIn('提示词替换前', debug); self.assertIn('<Picture 1 >', debug)
        self.assertIn('提示词替换后', debug); self.assertIn('@参考图5', debug)
        self.assertEqual(self.server.uploads, [Path(path).read_bytes() for path in self.paths])
        self.assertEqual([p for p in self.server.gets if p.startswith('/reference/')], ['/reference/1', '/reference/2', '/reference/3'])
        self.assertTrue(all('Authorization' not in headers and 'X-Upload-Token' not in headers for headers in self.server.reference_headers))
        payload = json.loads(next(body for path, _, body in self.server.calls if path == '/videos'))
        self.assertEqual(payload['images'], [self.server.base + f'/reference/{i}' for i in (1, 2, 3)])
        logged = json.loads(next(message.split(': ', 1)[1] for message, level in logs if level == 'debug' and message.startswith('请求体: ')))
        self.assertEqual(logged, payload)
        self.assertIn('SHA256一致', debug)
        self.assertTrue(any('Picture 5，但只绑定了3张图，可能影响生成质量' in message and level == 'warning' for message, level in logs))
        self.assertEqual(worker.tasks[0]['submitted_image_count'], 3)

    def test_debug_off_has_no_private_dumps_or_extra_downloads_but_warns(self):
        self.config['diagnostics']['debug_mode'] = False
        worker, logs = self.run_worker()
        self.assertEqual(worker.tasks[0]['status'], 'completed')
        self.assertFalse(any(level == 'debug' for _, level in logs))
        self.assertFalse(self.server.reference_headers)
        self.assertTrue(any('Picture 5，但只绑定了3张图，可能影响生成质量' in message and level == 'warning' for message, level in logs))

    def test_automatic_series_binding_reaches_three_uploads_and_three_wire_urls(self):
        self.config['match_overrides'] = {}
        for index, source in enumerate(self.paths, 1):
            Path(source).rename(self.root / f'1 ({index}).png')
        worker, logs = self.run_worker()
        self.assertEqual(worker.tasks[0]['status'], 'completed')
        self.assertIn('序号匹配', worker.tasks[0]['match_method'])
        self.assertEqual([Path(p).name for p in worker.tasks[0]['images']], ['1 (1).png', '1 (2).png', '1 (3).png'])
        self.assertTrue(any('上传图片3张' in message for message, _ in logs))
        self.assertEqual(self.server.uploads, [(self.root / f'1 ({i}).png').read_bytes() for i in (1, 2, 3)])
        wire = json.loads(next(body for path, _, body in self.server.calls if path == '/videos'))
        self.assertEqual(wire['images'], [self.server.base + '/reference/1', self.server.base + '/reference/2', self.server.base + '/reference/3'])
        debug_images = json.loads(next(message.split(': ', 1)[1] for message, level in logs
                                      if level == 'debug' and message.startswith('实际提交images: ')))
        self.assertEqual(debug_images, wire['images'])

    def test_changed_dimensions_warn_and_probe_failure_does_not_stop_submission(self):
        smaller = self.root / 'small.png'; image = QImage(10, 10, QImage.Format_RGB32); image.fill(QColor('red')); image.save(str(smaller))
        self.server.changed_image = smaller.read_bytes()
        worker, logs = self.run_worker()
        self.assertEqual(worker.tasks[0]['status'], 'completed')
        self.assertTrue(any('尺寸变化' in message and level == 'warning' for message, level in logs))
        self.server.reference_failure = True
        self.config['_rerun_signatures'] = [worker.tasks[0]['signature']]
        worker, logs = self.run_worker()
        self.assertEqual(worker.tasks[0]['status'], 'completed')
        self.assertTrue(any('检查失败' in message and level == 'warning' for message, level in logs))

    def test_actual_request_debug_is_redacted_without_modifying_wire_body(self):
        logs = []
        client = ApiClient(self.server.base, 'secret-video-key', log=lambda message, level: logs.append((message, level)))
        client.debug_mode = True
        self.addCleanup(client.close)
        client.create_task('MiniMax-H3', 'text secret-video-key', [self.server.base + '/image.png'], self.config['workspace'])
        debug = '\n'.join(message for message, level in logs if level == 'debug')
        self.assertIn('请求体:', debug); self.assertIn('[REDACTED]', debug)
        self.assertNotIn('secret-video-key', debug)
        self.assertIn('secret-video-key', json.loads(self.server.calls[-1][2])['prompt'])

    def test_diagnostics_remain_anonymous_with_netrc_cookies_and_redirects(self):
        self.server.uploads.append(Path(self.paths[0]).read_bytes())
        diagnostics = ReferenceDiagnostics(lambda *_: None)
        self.addCleanup(diagnostics.close)
        diagnostics.session.cookies.set('implicit-secret', 'old-cookie')
        with patch('requests.sessions.get_netrc_auth', return_value=('implicit-user', 'implicit-password')):
            self.assertIsNotNone(diagnostics.verify_uploaded(None, self.server.base + '/redirect-reference', 1))
            self.assertIsNotNone(diagnostics.verify_uploaded(None, self.server.base + '/reference/1', 2))
        self.assertEqual(len(self.server.reference_headers), 3)
        for headers in self.server.reference_headers:
            self.assertNotIn('Authorization', headers)
            self.assertNotIn('Cookie', headers)

    def test_escaped_video_and_upload_secrets_are_redacted_before_json_encoding(self):
        secrets = ['video-"quoted\\key', 'upload-"quoted\\token']
        self.config['api'].update(api_key=secrets[0], upload_api_key=secrets[1])
        self.prompt.write_text(' '.join(secrets), encoding='utf-8')
        urls = [self.server.base + '/reference?token=' + secret for secret in secrets]
        with patch('core.task_manager.ImageUploader.upload_images', return_value=urls), \
                patch.object(ReferenceDiagnostics, 'verify_uploaded', return_value=None):
            worker, logs = self.run_worker()
        self.assertEqual(worker.tasks[0]['status'], 'completed')
        wire = json.loads(next(body for path, _, body in self.server.calls if path == '/videos'))
        self.assertEqual(wire['images'], urls)
        self.assertEqual(wire['prompt'], ' '.join(secrets))
        for prefix in ('请求体: ', '实际提交images: '):
            logged = json.loads(next(message[len(prefix):] for message, level in logs
                                     if level == 'debug' and message.startswith(prefix)))
            for secret in secrets:
                self.assertNotIn(secret, str(logged))
            if isinstance(logged, dict):
                self.assertEqual(logged['prompt'], '[REDACTED] [REDACTED]')
                logged = logged['images']
            self.assertEqual(logged, [self.server.base + '/reference?token=[REDACTED]'] * 2)

    def test_fluent_toggle_filters_debug_and_controls_submitted_count(self):
        manager = ConfigManager(self.root / 'config.json'); manager.save_config()
        window = MainWindow(manager, network_time=False)
        try:
            drawer = window.workspace_page.log_drawer
            self.assertFalse(window.settings_page.debug_mode.isChecked())
            drawer.append_log('hidden-detail', 'debug')
            self.assertNotIn('hidden-detail', drawer.browser.toPlainText())
            window.settings_page.debug_mode.switchButton.setChecked(True)
            drawer.append_log('visible-detail', 'debug')
            drawer.filter_box.setCurrentText('DEBUG')
            self.assertIn('visible-detail', drawer.browser.toPlainText())
            task = dict(prompt_name='example', model='MiniMax-H3', status='processing', images=self.paths, task_id='test-id', submitted_image_count=3)
            card = window.workspace_page.current_task; card.update_task(0, task)
            self.assertIn('已提交3张参考图', card.debug_summary.text())
            self.assertFalse(card.debug_summary.isHidden())
            reopened = ConfigManager(manager.path).load_config()
            self.assertTrue(reopened['diagnostics']['debug_mode'])
            window.settings_page.debug_mode.switchButton.setChecked(False)
            self.assertNotIn('visible-detail', drawer.browser.toPlainText())
            self.assertTrue(card.debug_summary.isHidden())
        finally:
            window.close()
