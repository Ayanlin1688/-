"""Connection probes use only loopback HTTP fixtures."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from core.http_client import RequestError
from core.image_uploader import DEFAULT_UPLOAD_URL
from core.line_probe import probe_line, probe_upload


class ProbeFixture(BaseHTTPRequestHandler):
    models = []
    chat_statuses = {}
    model_status = 200
    upload_status = 200
    chat_calls = []
    upload_calls = 0

    def log_message(self, *_args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith('/models'):
            type(self).requests.append(('GET', self.path, dict(self.headers), None))
            field = 'models' if '/v1beta/' in self.path else 'data'
            self._send(self.model_status, {field: self.models})
        else:
            self._send(404, {'error': {'message': 'not found'}})

    def do_POST(self):
        type(self).upload_calls += 1
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length)
        type(self).requests.append(('POST', self.path, dict(self.headers), json.loads(body) if body.startswith(b'{') else None))
        if self.path.endswith(':generateContent'):
            model = self.path.split('/models/', 1)[1].split(':')[0]
            type(self).chat_calls.append(model)
            status = type(self).chat_statuses.get(model, 200)
            payload = ({'candidates': [{'content': {'parts': [{'text': '好的'}]}}]}
                       if status == 200 else {'error': {'code': status, 'message': 'service unavailable'}})
            self._send(status, payload)
        elif self.path.endswith('/chat/completions'):
            payload = json.loads(body)
            type(self).chat_calls.append(payload['model'])
            status = type(self).chat_statuses.get(payload['model'], 200)
            if status == 200:
                self._send(status, {'choices': [{'message': {'content': '好的'}}]})
            else:
                self._send(status, {'error': {'message': 'model_not_found'}})
        else:
            self._send(self.upload_status, {'files': [{'url': 'http://127.0.0.1/file.png'}]})


class LineProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), ProbeFixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}/v1'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        ProbeFixture.requests = []
        ProbeFixture.models = [
            {'id': 'video-v3'}, {'id': 'gemini-3.8-flash'}, {'id': 'gpt-image-2'},
            {'id': 'claude-haiku-4-5-pack'},
        ]
        ProbeFixture.chat_statuses = {}
        ProbeFixture.model_status = 200
        ProbeFixture.upload_status = 200
        ProbeFixture.chat_calls = []
        ProbeFixture.upload_calls = 0

    def test_models_classify_counts_and_image_probe_does_not_generate(self):
        result = probe_line(self.base, 'fixture-api-key', 'image')
        self.assertTrue(result['ok'])
        self.assertEqual(result['counts'], {'video': 1, 'chat': 1, 'image': 1})
        self.assertIn('未实际出图', result['message'])
        self.assertEqual(ProbeFixture.chat_calls, [])

    def test_chat_tries_allowlist_in_order_and_reports_hit_model(self):
        ProbeFixture.chat_statuses = {'claude-haiku-4-5': 404, 'gemini-3.8-flash': 503}
        snapshot = {
            'gpt-5.6-sol': {'id': 'gpt-5.6-sol', 'kind': 'chat'},
            'gemini-3.8-flash': {'id': 'gemini-3.8-flash', 'kind': 'chat'},
            'claude-haiku-4-5': {'id': 'claude-haiku-4-5', 'kind': 'chat'},
            'gpt-image-2': {'id': 'gpt-image-2', 'kind': 'image'},
        }
        result = probe_line(self.base, 'fixture-api-key', 'chat', models=snapshot)
        self.assertTrue(result['ok'])
        self.assertEqual(ProbeFixture.chat_calls,
                         ['claude-haiku-4-5', 'gemini-3.8-flash', 'gpt-5.6-sol'])
        self.assertEqual(result['model'], 'gpt-5.6-sol')
        self.assertIn('gpt-5.6-sol', result['message'])

    def test_auth_and_rate_limit_are_classified(self):
        for status in (401, 403):
            ProbeFixture.model_status = status
            result = probe_line(self.base, 'fixture-api-key', 'video')
            self.assertFalse(result['ok'])
            self.assertIn('密钥无效', result['message'])
        ProbeFixture.model_status = 200
        ProbeFixture.chat_statuses = {'claude-haiku-4-5': 429}
        result = probe_line(self.base, 'fixture-api-key', 'chat', models=[
            {'id': 'claude-haiku-4-5', 'kind': 'chat'}])
        self.assertFalse(result['ok'])
        self.assertIn('上游限流', result['message'])
        self.assertEqual(ProbeFixture.chat_calls, ['claude-haiku-4-5'])

    def test_chat_auth_failure_does_not_try_another_model(self):
        ProbeFixture.chat_statuses = {'claude-haiku-4-5': 403}
        result = probe_line(self.base, 'fixture-api-key', 'chat', models=[
            {'id': 'claude-haiku-4-5', 'kind': 'chat'},
            {'id': 'gemini-3.8-flash', 'kind': 'chat'},
        ])
        self.assertFalse(result['ok'])
        self.assertEqual(result['status_code'], 403)
        self.assertIn('密钥无效', result['message'])
        self.assertEqual(ProbeFixture.chat_calls, ['claude-haiku-4-5'])

    def test_deepseek_hostname_uses_fixed_probe_model(self):
        original = urlsplit
        def fake_split(value):
            parsed = original(value)
            if value == self.base:
                return SimpleNamespace(hostname='api.deepseek.com')
            return parsed
        with patch('core.line_probe.urlsplit', side_effect=fake_split):
            result = probe_line(self.base, 'fixture-api-key', 'chat', models=[
                {'id': 'gemini-3.8-flash', 'kind': 'chat'}])
        self.assertTrue(result['ok'])
        self.assertEqual(ProbeFixture.chat_calls, ['deepseek-flash'])

    def test_preferred_model_overrides_deepseek_default_probe_model(self):
        original = urlsplit
        def fake_split(value):
            parsed = original(value)
            if value == self.base:
                return SimpleNamespace(hostname='api.deepseek.com')
            return parsed
        with patch('core.line_probe.urlsplit', side_effect=fake_split):
            result = probe_line(self.base, 'fixture-api-key', 'chat',
                                preferred_model='deepseek-v4-pro')
        self.assertTrue(result['ok'])
        self.assertEqual(ProbeFixture.chat_calls, ['deepseek-v4-pro'])

    def test_preferred_image_model_is_validated_without_image_request(self):
        result = probe_line(self.base, 'fixture-api-key', 'image',
                            preferred_model='gpt-image-2')
        self.assertTrue(result['ok'])
        self.assertEqual(result['model'], 'gpt-image-2')
        self.assertEqual(ProbeFixture.upload_calls, 0)
        missing = probe_line(self.base, 'fixture-api-key', 'image',
                             preferred_model='missing-image')
        self.assertFalse(missing['ok'])
        self.assertIn('未找到该图片模型', missing['message'])

    def test_timeout_is_reported_without_secret(self):
        from core.line_probe import HttpClient
        original_request = HttpClient.request
        calls = 0
        def timeout_after_models(client, method, url, **kwargs):
            nonlocal calls
            calls += 1
            if url.endswith('/models'):
                return original_request(client, method, url, **kwargs)
            raise RequestError('Read timed out for fixture-api-key')
        with patch.object(HttpClient, 'request', timeout_after_models):
            result = probe_line(self.base, 'fixture-api-key', 'chat', models=[
                {'id': 'claude-haiku-4-5', 'kind': 'chat'}])
        self.assertFalse(result['ok'])
        self.assertIn('连接中转站超时', result['message'])
        self.assertNotIn('fixture-api-key', result['message'])
        self.assertEqual(calls, 2)

    def test_gemini_model_list_and_generate_content_contract(self):
        ProbeFixture.models = [{'name': 'models/gemini-flash', 'displayName': 'Gemini Flash'}]
        base = self.base.replace('/v1', '/v1beta')
        result = probe_line(base, 'fixture-api-key', 'chat')
        self.assertTrue(result['ok'])
        self.assertEqual(result['model'], 'gemini-flash')
        self.assertEqual(result['counts'], {'video': 0, 'chat': 1, 'image': 0})
        self.assertEqual([call[1] for call in ProbeFixture.requests],
                         ['/v1beta/models', '/v1beta/models/gemini-flash:generateContent'])
        for _, _, headers, _ in ProbeFixture.requests:
            self.assertEqual(headers['Authorization'], 'Bearer fixture-api-key')
            self.assertEqual(headers['x-goog-api-key'], 'fixture-api-key')
        self.assertEqual(ProbeFixture.requests[-1][3], {
            'contents': [{'role': 'user', 'parts': [{'text': '只回复两个字：好的'}]}]})

    def test_gemini_http_auth_failure_is_visible_and_final(self):
        ProbeFixture.models = [{'name': 'models/gemini-flash'}]
        ProbeFixture.chat_statuses = {'gemini-flash': 401}
        result = probe_line(self.base.replace('/v1', '/v1beta'), 'fixture-api-key', 'chat')
        self.assertFalse(result['ok'])
        self.assertIn('密钥无效', result['message'])
        self.assertEqual(ProbeFixture.chat_calls, ['gemini-flash'])

    def test_ssl_failure_at_model_list_and_chat_is_one_chinese_message(self):
        from core.line_probe import HttpClient
        original = HttpClient.request
        for at_models in (True, False):
            with self.subTest(at_models=at_models):
                calls = []
                def fail_ssl(client, method, url, **kwargs):
                    calls.append(url)
                    if not at_models and url.endswith('/models'):
                        return original(client, method, url, **kwargs)
                    raise RequestError('HTTPSConnectionPool Max retries SSLError UNEXPECTED_EOF fixture-api-key')
                with patch.object(HttpClient, 'request', fail_ssl):
                    result = probe_line(self.base, 'fixture-api-key', 'chat')
                self.assertFalse(result['ok'])
                self.assertIn('SSL 连接被中断', result['message'])
                self.assertNotIn('HTTPSConnectionPool', result['message'])
                self.assertNotIn('fixture-api-key', result['message'])
                self.assertEqual(len(calls), 1 if at_models else 2)

    def test_upload_token_misplaced_default_url_and_missing_token_warning(self):
        misplaced = probe_upload({'upload_url': 'vup_fixture-token'})
        self.assertFalse(misplaced['ok'])
        self.assertIn('上传 Token', misplaced['message'])
        misplaced_sk = probe_upload({'upload_url': 'sk-fixture-upload-token'})
        self.assertFalse(misplaced_sk['ok'])
        self.assertIn('上传 Token', misplaced_sk['message'])
        skipped = probe_upload({'upload_url': '', 'upload_api_key': ''})
        self.assertTrue(skipped['ok'])
        self.assertEqual(skipped['level'], 'warning')
        self.assertEqual(skipped['url'], DEFAULT_UPLOAD_URL)
        self.assertIn('跳过', skipped['message'])
        self.assertEqual(ProbeFixture.upload_calls, 0)

    def test_upload_uses_only_loopback_fixture_and_rejects_invalid_url(self):
        result = probe_upload({'upload_url': self.base + '/upload', 'upload_api_key': 'fixture-upload-token'})
        self.assertTrue(result['ok'])
        self.assertEqual(ProbeFixture.upload_calls, 1)
        invalid = probe_upload({'upload_url': 'vup_fixture-token', 'upload_api_key': 'fixture-upload-token'})
        self.assertFalse(invalid['ok'])
        self.assertIn('有效的 http:// 或 https://', invalid['message'])


if __name__ == '__main__':
    unittest.main()
