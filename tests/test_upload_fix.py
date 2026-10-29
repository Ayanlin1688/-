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
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_media_upload_preserves_chinese_filename_bytes_and_mime(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, kind, mime in (('片段.MOV', 'videos', 'video/quicktime'),
                                     ('旁白.m4a', 'audios', 'audio/mp4')):
                path = Path(directory) / name
                path.write_bytes(b'original-media-bytes')
                captured = []
                def send(method, url, **kwargs):
                    filename, stream, content_type = kwargs['files']['files']
                    captured.append((filename, stream.read(), content_type))
                    return response(200, {'url': 'https://fixture.test/media'})
                session = Mock(); session.request.side_effect = send
                client = ImageUploader('https://fixture.test/upload', session=session)
                with patch('core.image_uploader.normalized_reference_path') as normalize:
                    self.assertEqual(client.upload_file(path, kind=kind), 'https://fixture.test/media')
                normalize.assert_not_called()
                self.assertEqual(captured, [(name, path.read_bytes(), mime)])
                client.close()

    def test_media_upload_uses_long_timeout_for_video_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, kind, expected in (('片段.mp4', 'videos', (15, 300)),
                                         ('旁白.wav', 'audios', (15, 120))):
                path = Path(directory) / name
                path.write_bytes(b'media')
                calls = []

                def send(method, url, **kwargs):
                    calls.append(kwargs['timeout'])
                    return response(200, {'url': 'https://fixture.test/media'})

                session = Mock(); session.request.side_effect = send
                client = ImageUploader('https://fixture.test/upload', session=session)
                self.assertEqual(client.upload_file(path, kind=kind), 'https://fixture.test/media')
                self.assertEqual(calls, [expected])
                client.close()

    def test_media_upload_rejections_do_not_retry_or_accept_invalid_urls(self):
        from core.http_client import RequestError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '片段.mp4'; path.write_bytes(b'media')
            for status in (400, 401, 403, 413, 415, 422, 429):
                session = Mock(); session.request.return_value = response(status, {'error': 'rejected'})
                client = ImageUploader('https://fixture.test/upload', session=session)
                with self.assertRaises(RequestError):
                    client.upload_file(path, kind='videos')
                self.assertEqual(session.request.call_count, 1)
                client.close()
            for url in ('file:///private.mp4', 'https:', '', 'javascript:alert(1)'):
                session = Mock(); session.request.return_value = response(200, {'url': url})
                client = ImageUploader('https://fixture.test/upload', session=session)
                with self.assertRaises(RequestError):
                    client.upload_file(path, kind='videos')
                self.assertEqual(session.request.call_count, 1)
                client.close()

    def test_media_upload_http_errors_are_classified_in_chinese(self):
        from core.http_client import RequestError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '片段.mp4'; path.write_bytes(b'media')
            for status, payload, expected in (
                (401, {'error': 'unauthorized'}, '参考素材上传鉴权失败：上传 Token 与生成 API Key 不同，请在设置中填写图床上传 Token'),
                (403, {'error': 'forbidden'}, '上传鉴权失败'),
                (413, {'error': 'payload too large'}, '200MB'),
                (503, {'error': 'service unavailable'}, '服务暂时不可用'),
            ):
                session = Mock()
                session.request.side_effect = [response(status, payload)] * (2 if status == 503 else 1)
                client = ImageUploader('https://fixture.test/upload', session=session)
                with self.subTest(status=status), self.assertRaises(RequestError) as caught:
                    client.upload_file(path, kind='videos')
                self.assertIn(expected, str(caught.exception))
                self.assertEqual(session.request.call_count, 2 if status == 503 else 1)
                client.close()

    def test_media_upload_network_retry_once_and_cancel_during_backoff(self):
        from core.http_client import Cancelled, RequestError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '旁白.wav'; path.write_bytes(b'media')
            client = ImageUploader('https://fixture.test/upload')
            with patch.object(client, '_upload_once', side_effect=[RequestError('network', retry_safe=True), 'https://fixture.test/media']) as upload, patch('core.image_uploader.time.sleep'):
                self.assertEqual(client.upload_file(path, kind='audios'), 'https://fixture.test/media')
                self.assertEqual(upload.call_count, 2)
            with patch.object(client, '_upload_once', side_effect=RequestError('network', retry_safe=True)) as upload, patch('core.image_uploader.time.sleep'):
                with self.assertRaises(RequestError):
                    client.upload_file(path, kind='audios')
                self.assertEqual(upload.call_count, 2)
            client.check_cancel = Mock(side_effect=[None, Cancelled()])
            with patch.object(client, '_upload_once', side_effect=RequestError('network', retry_safe=True)) as upload:
                with self.assertRaises(Cancelled):
                    client.upload_file(path, kind='audios')
                self.assertEqual(upload.call_count, 1)
            client.close()

    def test_media_upload_retries_server_error_once_then_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '片段.mp4'; path.write_bytes(b'media')
            session = Mock()
            captured = []

            def send(method, url, **kwargs):
                captured.append((kwargs['headers'].get('Authorization'),
                                 kwargs['files']['files'][1].read()))
                return (response(503, {'error': 'temporary'}) if len(captured) == 1
                        else response(200, {'url': 'https://fixture.test/media'}))

            session.request.side_effect = send
            client = ImageUploader('https://fixture.test/upload', 'fixture-key', session=session)
            with patch('core.image_uploader.time.sleep'):
                self.assertEqual(client.upload_file(path, kind='videos'), 'https://fixture.test/media')
            self.assertEqual(session.request.call_count, 2)
            self.assertEqual(captured, [('Bearer fixture-key', b'media')] * 2)
            client.close()

    def test_media_upload_retries_a_requests_timeout_only_once(self):
        from core.http_client import RequestError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '片段.mp4'; path.write_bytes(b'media')
            session = Mock()
            session.request.side_effect = [requests.exceptions.ReadTimeout('read timeout'),
                                           response(200, {'url': 'https://fixture.test/media'})]
            client = ImageUploader('https://fixture.test/upload', 'fixture-key', session=session)
            with patch('core.image_uploader.time.sleep'):
                self.assertEqual(client.upload_file(path, kind='videos'), 'https://fixture.test/media')
            self.assertEqual(session.request.call_count, 2)
            self.assertEqual([call.kwargs['timeout'] for call in session.request.call_args_list],
                             [(15, 300), (15, 300)])
            client.close()

            session = Mock()
            session.request.side_effect = [requests.exceptions.ReadTimeout('read timeout'),
                                           requests.exceptions.ReadTimeout('read timeout')]
            client = ImageUploader('https://fixture.test/upload', session=session)
            with patch('core.image_uploader.time.sleep'), self.assertRaises(RequestError):
                client.upload_file(path, kind='videos')
            self.assertEqual(session.request.call_count, 2)
            client.close()

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

    def test_media_upload_does_not_retry_four_xx_auth_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '中文图片.png'; path.write_bytes(b'image-contents')
            calls = []
            def send(method, url, **kwargs):
                calls.append((method, url, kwargs['headers'], kwargs['timeout'], kwargs['files']['files'][1].read()))
                return response(401, {'error': 'upload token required'})
            session = Mock(); session.request.side_effect = send
            logs = []
            client = ImageUploader('https://configured/upload', 'upload-secret', session=session, log=lambda *args: logs.append(args))
            from core.http_client import RequestError
            with self.assertRaises(RequestError) as caught:
                client.upload_image(path)
            self.assertEqual(str(caught.exception),
                             '参考素材上传鉴权失败：上传 Token 与生成 API Key 不同，请在设置中填写图床上传 Token')
            self.assertEqual(len(calls), 1)
            _, url, headers, timeout, data = calls[0]
            self.assertEqual(url, 'https://configured/upload')
            self.assertEqual(headers['Authorization'], 'Bearer upload-secret')
            self.assertNotIn('Content-Type', headers)  # requests supplies the multipart boundary.
            self.assertEqual(timeout, (15, 60))
            self.assertEqual(data, b'image-contents')
            self.assertTrue(all('upload-secret' not in message for message, _ in logs))
            self.assertNotIn('unauthorized', str(caught.exception).casefold())

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


    def test_oversized_reference_is_normalized_before_upload(self):
        from PyQt5.QtGui import QImage
        from core.image_uploader import fit_reference_image, MAX_REFERENCE_RATIO, MAX_REFERENCE_SIDE
        image = QImage(8000, 1000, QImage.Format_RGB32)
        image.fill(0xFF224466)
        fitted, changed = fit_reference_image(image)
        self.assertTrue(changed)
        self.assertLessEqual(max(fitted.width(), fitted.height()), MAX_REFERENCE_SIDE)
        self.assertGreaterEqual(min(fitted.width(), fitted.height()), 256)
        ratio = fitted.width() / fitted.height()
        self.assertGreaterEqual(ratio, 0.4)
        self.assertLessEqual(ratio, MAX_REFERENCE_RATIO + 0.001)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'big.png'
            self.assertTrue(image.save(str(path), 'PNG'))
            captured = []

            def send(method, url, **kwargs):
                captured.append(kwargs['files']['files'][1].read())
                return response(200, {'url': 'https://image/big.jpg'})

            session = Mock()
            session.request.side_effect = send
            self.assertEqual(ImageUploader('https://image/upload', 'key', session=session).upload_image(path), 'https://image/big.jpg')
            uploaded = QImage.fromData(captured[0])
            self.assertFalse(uploaded.isNull())
            self.assertLessEqual(max(uploaded.width(), uploaded.height()), MAX_REFERENCE_SIDE)
            self.assertLessEqual(uploaded.width() / uploaded.height(), MAX_REFERENCE_RATIO + 0.02)
            self.assertNotEqual(captured[0], path.read_bytes())
