import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from core.api_client import ApiClient
from core.image_uploader import ImageUploader, DEFAULT_UPLOAD_URL


def response(status, payload):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


class UploadFixTests(unittest.TestCase):
    def test_connection_probe_is_a_decodable_png(self):
        from PyQt5.QtGui import QImage
        captured = []
        def send(method, url, **kwargs):
            captured.append(kwargs['files']['files'][1].read())
            return response(200, {'files': [{'url': 'https://image/probe.png'}]})
        session = Mock(); session.request.side_effect = send
        result = ImageUploader('https://image/upload', 'key', session=session).test_connection()
        self.assertTrue(result['ok'])
        image = QImage.fromData(captured[0], 'PNG')
        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (64, 64))
        self.assertEqual(image.pixelColor(30, 30).name(), '#5e6ad2')

    def test_default_provider_receives_dedicated_token_header_and_bearer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '参考.png'; path.write_bytes(b'image')
            session = Mock(); session.request.return_value = response(200, {'url': 'https://image/result.png'})
            manager = Mock(); manager.config = {'api': {'api_key': 'video-key', 'upload_api_key': 'upload-token', 'upload_url': DEFAULT_UPLOAD_URL}}
            ImageUploader.from_config(manager, session=session).upload_image(path)
            headers = session.request.call_args.kwargs['headers']
            self.assertEqual(headers['X-Upload-Token'], 'upload-token')
            self.assertEqual(headers['Authorization'], 'Bearer upload-token')
            self.assertNotIn('video-key', str(headers))

    def test_auth_timeout_multipart_retry_reopens_full_chinese_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '中文图片.png'; path.write_bytes(b'image-contents')
            calls = []
            def send(method, url, **kwargs):
                calls.append((method, url, kwargs['headers'], kwargs['timeout'], kwargs['files']['files'][1].read()))
                return response(401, {'error': 'upload token required'}) if len(calls) == 1 else response(200, {'data': {'url': 'https://image/result.png'}})
            session = Mock(); session.request.side_effect = send
            logs = []
            client = ImageUploader('https://configured/upload', 'upload-secret', session=session, log=lambda *args: logs.append(args))
            with patch('core.image_uploader.time.sleep') as sleep:
                self.assertEqual(client.upload_image(path), 'https://image/result.png')
                self.assertAlmostEqual(sum(c.args[0] for c in sleep.call_args_list), 2)
            self.assertEqual(len(calls), 2)
            for _, url, headers, timeout, data in calls:
                self.assertEqual(url, 'https://configured/upload')
                self.assertEqual(headers['Authorization'], 'Bearer upload-secret')
                self.assertNotIn('Content-Type', headers)  # requests supplies the multipart boundary.
                self.assertEqual(timeout, (15, 60))
                self.assertEqual(data, b'image-contents')
            self.assertTrue(any('HTTP 401' in message and 'upload token required' in message for message, _ in logs))

    def test_four_response_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '图.png'; path.touch()
            url = 'https://image/result.png'
            for payload in ({'files': [{'url': url}]}, {'data': {'files': [{'url': url}]}}, {'url': url}, {'data': {'url': url}}):
                session = Mock(); session.request.return_value = response(200, payload)
                self.assertEqual(ImageUploader('https://image/upload', 'key', session=session).upload_image(path), url)

    def test_two_connection_results_are_independent(self):
        session = Mock(); session.request.return_value = response(500, {'error': 'video unavailable'})
        client = ApiClient('https://video/v1', 'video-key', session=session)
        uploader = Mock(); uploader.test_connection.return_value = dict(ok=True, message='uploaded')
        results = client.test_connection(uploader)
        self.assertFalse(results['ok'])
        self.assertFalse(results['video']['ok'])
        self.assertTrue(results['upload']['ok'])
        uploader.test_connection.assert_called_once()
