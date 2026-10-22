import json
import unittest

import requests

from core.api_errors import classify_http_error, classify_connection_error


class ApiErrorTests(unittest.TestCase):
    def test_multilingual_content_rejections_never_retry_or_switch(self):
        for code in (400, 422):
            for word in ('nudity', 'nude', 'desnudez', 'sexual', 'erotic', 'erótico',
                         'sexualidad', 'safety', 'content policy', 'policy',
                         'invalid_request_error', 'upstream_text_reply', '敏感', '审核', '违规', '色情'):
                with self.subTest(code=code, word=word):
                    result = classify_http_error(code, json.dumps({'error': {'message': word}}, ensure_ascii=False))
                    self.assertEqual(result, dict(
                        message='内容被上游安全审核拒绝，请修改提示词或参考图后重新生成',
                        category='content_policy', retryable=False, switchable=False))

    def test_auth_model_rate_limit_and_upstream_categories(self):
        for code, body, category, retry, switch in (
                (401, '', 'authentication', False, False), (403, '', 'authentication', False, False),
                (404, 'model_not_found', 'model_not_found', False, True),
                (429, '', 'rate_limit', True, True),
                *((code, '', 'upstream', True, True) for code in (500, 502, 503, 504)),
                (409, 'upstream_error', 'upstream', True, True),
                (400, 'invalid size', 'http', False, False)):
            with self.subTest(code=code, body=body):
                result = classify_http_error(code, body)
                self.assertEqual((result['category'], result['retryable'], result['switchable']),
                                 (category, retry, switch))

    def test_other_http_errors_extract_bounded_safe_message(self):
        credential = 'sk-' + 'offline-fixture-not-real'
        upload = 'vup_' + 'offline-fixture-not-real'
        body = json.dumps({'error': {'message': f'{credential} {upload} Bearer private-value ' + 'x' * 300}})
        message = classify_http_error(400, body)['message']
        self.assertNotIn(credential, message)
        self.assertNotIn(upload, message)
        self.assertNotIn('private-value', message)
        self.assertNotIn('{', message)
        self.assertLessEqual(len(message), len('HTTP 400：') + 120)

    def test_non_string_and_non_message_payloads_are_safe(self):
        for body in (None, {}, [], 15, '{broken', '{"error":{"message":{}}}'):
            result = classify_http_error(400, body)
            self.assertFalse(result['retryable'])
            self.assertTrue(result['message'].startswith('HTTP 400：'))

    def test_connection_messages_classify_without_echoing_secrets(self):
        for error, word in (
                (requests.exceptions.ConnectTimeout('fixture-secret'), '超时'),
                ('Read timed out fixture-secret', '超时'),
                (requests.exceptions.SSLError('fixture-secret'), 'SSL'),
                ('UNEXPECTED_EOF fixture-secret', 'SSL'),
                ('SSL_C_SYSCALL fixture-secret', 'SSL'),
                ('handshake fixture-secret', 'SSL'),
                (requests.exceptions.ProxyError('fixture-secret'), '无法连接')):
            result = classify_connection_error(error)
            self.assertIn(word, result['message'])
            self.assertNotIn('fixture-secret', result['message'])
            self.assertEqual((result['category'], result['retryable'], result['switchable']),
                             ('connection', True, True))
