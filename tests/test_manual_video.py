import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest import mock

import core.manual_video as manual_video
from core.http_client import RequestError
from core.manual_video import (
    ManualVideoManager,
    ManualVideoStore,
    create_veo_task,
    fetch_veo_operation,
)


MP4 = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00' + b'fixture-video'


class _VeoHandler(BaseHTTPRequestHandler):
    requests_seen = []
    poll_count = 0

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length)
        self.__class__.requests_seen.append({
            'path': self.path,
            'headers': {key.lower(): value for key, value in self.headers.items()},
            'json': json.loads(body.decode('utf-8')) if body else {},
        })
        if self.path.endswith(':predictLongRunning'):
            payload = {'name': 'operations/fixture-video'}
        elif self.path.endswith(':fetchPredictOperation'):
            self.__class__.poll_count += 1
            payload = {
                'done': True,
                'response': {'generatedVideos': [{'video': {'uri':
                    f'http://127.0.0.1:{self.server.server_port}/video'}}]},
            }
        else:
            payload = {'error': {'message': 'unknown endpoint'}}
        encoded = json.dumps(payload).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == '/video':
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(MP4)))
            self.end_headers()
            self.wfile.write(MP4)
            return
        self.send_error(404)

    def log_message(self, *_args):
        return


class _OpenAIHandler(BaseHTTPRequestHandler):
    requests_seen = []

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length)
        self.__class__.requests_seen.append((self.command, self.path,
                                             json.loads(body.decode('utf-8')) if body else {}))
        payload = {'id': 'fixture-task'}
        encoded = json.dumps(payload).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.__class__.requests_seen.append((self.command, self.path, {}))
        if self.path == '/v1/video/generations/fixture-task' or self.path == '/v1/videos/fixture-task':
            payload = {'status': 'completed', 'result_url':
                       f'http://127.0.0.1:{self.server.server_port}/result.mp4'}
            encoded = json.dumps(payload).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if self.path == '/result.mp4':
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(MP4)))
            self.end_headers()
            self.wfile.write(MP4)
            return
        self.send_error(404)

    def log_message(self, *_args):
        return


class _FakeApiClient:
    create_calls = []
    active = 0
    peak = 0
    lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        self.closed = False

    def create_task(self, model, prompt, image_urls, params, catalog=None, idempotency_key=None):
        with self.lock:
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
        type(self).create_calls.append((model, prompt, image_urls, params, catalog))
        time.sleep(.02)
        with self.lock:
            type(self).active -= 1
        return 'remote-' + model

    def query_task(self, task_id, model, catalog=None):
        return {'status': 'completed', 'progress': 100, 'result_url': 'fixture://video'}

    def close(self):
        self.closed = True


class _FakeDownloader:
    api_keys = []

    def __init__(self, *args, **kwargs):
        type(self).api_keys.append(kwargs.get('api_key'))

    def download_video(self, url, save_dir, filename, **kwargs):
        target = Path(save_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(MP4)
        return str(target)

    def close(self):
        pass


class ManualVideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ManualVideoStore(self.root / 'manual.sqlite3')
        self.config = SimpleNamespace(
            path=self.root / 'config.json',
            config={
                'api': {'base_url': 'https://relay.example/v1', 'api_key': 'fixture-secret'},
                'paths': {'output': str(self.root / 'output')},
            },
        )
        self.callbacks = []
        self.manager = ManualVideoManager(self.config, self.store,
                                          on_task=lambda item: self.callbacks.append(item))
        self.manager.poll_interval = 0

    def tearDown(self):
        self.manager.close()
        self.store.close()
        self.temp.cleanup()

    def _tasks(self, count=1, model='video-v2', refs=None):
        return self.manager.prepare_tasks(
            'A warm product video with a slow camera move', model,
            ratio='16:9', output_size='1080p', duration=5, count=count, refs=refs or {})

    def test_store_roundtrip_sort_count_and_delete(self):
        self.store.upsert({'local_id': 'old', 'created_at': 1, 'status': 'pending'})
        self.store.upsert({'local_id': 'new', 'created_at': 2, 'status': 'succeeded'})
        self.assertEqual([row['local_id'] for row in self.store.list()], ['new', 'old'])
        self.assertEqual(self.store.count('succeeded'), 1)
        self.assertTrue(self.store.delete('old'))
        self.assertFalse(self.store.delete('missing'))
        self.store.close()
        reopened = ManualVideoStore(self.root / 'manual.sqlite3')
        self.addCleanup(reopened.close)
        self.assertEqual([row['local_id'] for row in reopened.list()], ['new'])

    def test_store_degrades_to_memory(self):
        with mock.patch.object(ManualVideoStore, '_connect', side_effect=OSError('read only')):
            store = ManualVideoStore(self.root / 'disabled.sqlite3')
        self.addCleanup(store.close)
        self.assertFalse(store.persistence_available)
        store.upsert({'local_id': 'memory', 'created_at': 1, 'status': 'pending'})
        self.assertEqual(store.list()[0]['local_id'], 'memory')

    def test_openai_routes_to_api_client_and_persists_video(self):
        _FakeApiClient.create_calls = []
        _FakeDownloader.api_keys = []
        tasks = self._tasks(refs={'images': ['https://img/a.png']})
        with mock.patch('core.manual_video.ApiClient', _FakeApiClient), \
                mock.patch('core.manual_video.VideoDownloader', _FakeDownloader):
            result = self.manager.run_tasks(tasks)[0]
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['protocol'], 'openai')
        self.assertTrue(Path(result['file_path']).is_file())
        self.assertIn('单独视频', result['file_path'])
        call = _FakeApiClient.create_calls[0]
        self.assertEqual(call[0], 'video-v2')
        self.assertEqual(call[1], 'A warm product video with a slow camera move')
        self.assertEqual(call[2], ['https://img/a.png'])
        self.assertEqual(call[3]['aspect_ratio'], '16:9')
        self.assertEqual(call[3]['resolution'], '1080p')
        self.assertEqual(_FakeDownloader.api_keys, ['fixture-secret'])
        self.assertEqual([item['status'] for item in self.callbacks][:2], ['pending', 'running'])
        self.assertIn('downloading', [item['status'] for item in self.callbacks])
        self.assertGreater(result['size_bytes'], 0)
        self.assertGreaterEqual(result['download_ms'], 0)
        self.assertGreater(result['throughput_bps'], 0)

    def test_success_result_starts_streaming_immediately_and_records_download_metrics(self):
        task = self._tasks()[0]
        ready_at = []
        download_started = []

        def complete_generation(*_args):
            ready_at.append(time.monotonic())
            return {'video_url': 'https://relay.example/video.mp4'}

        class _SlowDownloader:
            def __init__(self, *args, **kwargs):
                pass

            def download_video(self, _url, save_dir, filename, **kwargs):
                download_started.append(time.monotonic())
                time.sleep(.2)
                path = Path(save_dir) / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(MP4)
                return str(path)

            def close(self):
                pass

        with mock.patch.object(self.manager, '_run_openai', side_effect=complete_generation), \
                mock.patch('core.manual_video.VideoDownloader', _SlowDownloader):
            result = self.manager.run_tasks([task])[0]

        self.assertEqual(result['status'], 'succeeded')
        self.assertLess(download_started[0] - ready_at[0], .05)
        self.assertGreaterEqual(result['download_ms'], 180)
        self.assertEqual(result['size_bytes'], len(MP4))
        self.assertAlmostEqual(result['throughput_bps'],
                               result['size_bytes'] / (result['download_ms'] / 1000.0),
                               delta=result['size_bytes'] / max(result['download_ms'], 1))
        self.assertEqual(result['video_url'], 'https://relay.example/video.mp4')
        self.assertEqual([item['status'] for item in self.callbacks[-3:]],
                         ['running', 'downloading', 'succeeded'])

    def test_download_failure_retries_twice_without_resubmitting_remote_task(self):
        task = self._tasks()[0]
        attempts = []
        delays = []

        class _FlakyDownloader:
            def __init__(self, *args, **kwargs):
                pass

            def download_video(self, _url, save_dir, filename, **kwargs):
                attempts.append(filename)
                if len(attempts) < 3:
                    raise OSError('temporary stream failure')
                path = Path(save_dir) / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(MP4)
                return str(path)

            def close(self):
                pass

        with mock.patch.object(self.manager, '_run_openai', return_value={
                'video_url': 'https://relay.example/video.mp4'}) as generate, \
                mock.patch('core.manual_video.VideoDownloader', _FlakyDownloader), \
                mock.patch('core.manual_video.time.sleep', side_effect=delays.append):
            result = self.manager.run_tasks([task])[0]

        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(attempts), 3)
        self.assertEqual(delays, [1, 2])
        generate.assert_called_once()
        self.assertEqual(result['video_url'], 'https://relay.example/video.mp4')

    def test_auto_download_reuses_existing_valid_mp4(self):
        task = self._tasks()[0]
        folder = self.root / 'existing-output'
        folder.mkdir()
        existing = folder / 'video_1.mp4'
        existing.write_bytes(MP4)
        with mock.patch.object(self.manager, '_task_output_dir', return_value=folder), \
                mock.patch.object(self.manager, '_run_openai', return_value={
                    'video_url': 'https://relay.example/video.mp4'}), \
                mock.patch('core.manual_video.VideoDownloader') as downloader:
            result = self.manager.run_tasks([task])[0]
        downloader.assert_not_called()
        self.assertEqual(Path(result['file_path']), existing)
        self.assertEqual(result['size_bytes'], len(MP4))

    def test_runtime_video_catalog_is_passed_to_api_client(self):
        catalog = {
            'relay-video-custom': {
                'id': 'relay-video-custom', 'kind': 'video', 'family': 'video-v2',
                'resolutions': ['1080p'], 'ratios': ['16:9'],
                'durations': [5], 'audio': False, 'seed': False,
                'max_images': 3, 'protocol_known': True,
            },
        }
        controller = SimpleNamespace(snapshot_for=mock.Mock(return_value=catalog))
        self.config.model_catalog_controller = controller
        _FakeApiClient.create_calls = []
        with mock.patch('core.manual_video.ApiClient', _FakeApiClient), \
                mock.patch('core.manual_video.VideoDownloader', _FakeDownloader):
            result = self.manager.run_tasks(self._tasks(model='relay-video-custom'))[0]
        self.assertEqual(result['status'], 'succeeded')
        self.assertIs(_FakeApiClient.create_calls[0][4], catalog)
        controller.snapshot_for.assert_called_with('video')

    def test_runtime_video_catalog_is_used_for_model_family_and_parameters(self):
        catalog = {
            'custom-video-model': {
                'id': 'custom-video-model', 'kind': 'video', 'family': 'video-v2',
                'resolutions': ['1080p'], 'protocol_known': True,
            },
        }
        controller = SimpleNamespace(snapshot_for=mock.Mock(return_value=catalog))
        self.config.model_catalog_controller = controller
        _FakeApiClient.create_calls = []
        with mock.patch('core.manual_video.ApiClient', _FakeApiClient), \
                mock.patch('core.manual_video.VideoDownloader', _FakeDownloader):
            result = self.manager.run_tasks(self._tasks(model='custom-video-model'))[0]

        self.assertEqual(result['status'], 'succeeded')
        controller.snapshot_for.assert_called_with('video')
        call = _FakeApiClient.create_calls[0]
        self.assertIs(call[4], catalog)
        self.assertEqual(call[3]['resolution'], '1080p')

    def test_unsupported_openai_endpoint_falls_back_to_gemini_once(self):
        task = self._tasks()[0]
        with mock.patch.object(self.manager, '_run_openai',
                               side_effect=RequestError('unsupported endpoint', 404)), \
                mock.patch.object(self.manager, '_run_veo',
                                  return_value={'video_bytes': MP4}) as run_veo:
            result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['protocol'], 'gemini')
        self.assertTrue(Path(result['file_path']).is_file())
        run_veo.assert_called_once()

    def test_openai_fallback_does_not_retry_auth_or_accepted_task(self):
        task = self._tasks()[0]
        with mock.patch.object(self.manager, '_run_openai',
                               side_effect=RequestError('unauthorized', 401)), \
                mock.patch.object(self.manager, '_run_veo') as run_veo:
            result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'failed')
        run_veo.assert_not_called()

        task = self._tasks()[0]

        def accepted_then_404(record, *_args):
            record['task_id'] = 'accepted-task'
            raise RequestError('not found while polling', 404)

        with mock.patch.object(self.manager, '_run_openai', side_effect=accepted_then_404), \
                mock.patch.object(self.manager, '_run_veo') as run_veo:
            result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['task_id'], 'accepted-task')
        run_veo.assert_not_called()

    def test_local_reference_images_upload_before_submission(self):
        image = self.root / 'reference.png'
        image.write_bytes(b'fixture-image')
        uploader = mock.MagicMock()
        uploader.upload_images.return_value = ['https://upload.example/reference.png']
        _FakeApiClient.create_calls = []
        with mock.patch('core.manual_video.ApiClient', _FakeApiClient), \
                mock.patch('core.manual_video.VideoDownloader', _FakeDownloader), \
                mock.patch('core.image_uploader.ImageUploader.from_config', return_value=uploader) as make_uploader:
            result = self.manager.run_tasks(self._tasks(refs={'images': [str(image)]}))[0]
        self.assertEqual(result['status'], 'succeeded')
        make_uploader.assert_called_once()
        uploader.upload_images.assert_called_once_with([str(image)], check_cancel=mock.ANY)
        self.assertEqual(_FakeApiClient.create_calls[0][2], ['https://upload.example/reference.png'])
        uploader.close.assert_called_once()

    def test_reference_upload_failure_does_not_submit_task(self):
        image = self.root / 'reference.png'
        image.write_bytes(b'fixture-image')
        uploader = mock.MagicMock()
        uploader.upload_images.return_value = [None]
        with mock.patch('core.manual_video.ApiClient', _FakeApiClient), \
                mock.patch('core.image_uploader.ImageUploader.from_config', return_value=uploader):
            result = self.manager.run_tasks(self._tasks(refs={'images': [str(image)]}))[0]
        self.assertEqual(result['status'], 'failed')
        self.assertIn('参考图片上传失败', result['error'])

    def test_veo_uploads_local_reference_and_publishes_operation_id(self):
        _VeoHandler.requests_seen = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), _VeoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.config.config['api']['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
        image = self.root / 'veo-reference.png'
        image.write_bytes(b'fixture-image')
        uploader = mock.MagicMock()
        uploader.upload_images.return_value = ['https://upload.example/veo-reference.png']
        task = self._tasks(model='veo-3', refs={'images': [str(image)]})[0]

        with mock.patch('core.image_uploader.ImageUploader.from_config', return_value=uploader) as make_uploader:
            result = self.manager.run_tasks([task])[0]

        self.assertEqual(result['status'], 'succeeded')
        make_uploader.assert_called_once()
        uploader.upload_images.assert_called_once_with([str(image)], check_cancel=mock.ANY)
        uploader.close.assert_called_once()
        predict = next(request for request in _VeoHandler.requests_seen
                       if request['path'].endswith(':predictLongRunning'))
        self.assertEqual(predict['json']['instances'][0]['images'],
                         ['https://upload.example/veo-reference.png'])
        operation_updates = [item for item in self.callbacks
                             if item.get('status') == 'running'
                             and item.get('task_id') == 'operations/fixture-video']
        self.assertTrue(operation_updates)

    def test_global_cancel_reaches_reference_upload_callback(self):
        image = self.root / 'cancel-reference.png'
        image.write_bytes(b'fixture-image')
        uploader = mock.MagicMock()

        def cancel_during_upload(_paths, check_cancel=None):
            self.manager.cancel()
            check_cancel()

        uploader.upload_images.side_effect = cancel_during_upload
        task = self._tasks(model='veo-3', refs={'images': [str(image)]})[0]
        with mock.patch('core.image_uploader.ImageUploader.from_config', return_value=uploader):
            result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['error'], '任务已取消')
        uploader.close.assert_called_once()

    def test_video_audio_upload_order_payload_and_persistence(self):
        video = self.root / '参考视频.mp4'; video.write_bytes(MP4)
        audio = self.root / '参考音频.wav'; audio.write_bytes(b'fixture-audio')
        refs = {'images': ['https://img/a.png'],
                'videos': [str(video), 'https://media/second.mp4'], 'audios': [str(audio)]}
        task = self._tasks(refs=refs)[0]
        self.assertEqual(self.store.get(task['local_id'])['refs'], refs)
        uploader = mock.MagicMock()
        uploader.upload_file.side_effect = ['https://media/first.mp4', 'https://media/audio.wav']
        _FakeApiClient.create_calls = []
        with mock.patch('core.image_uploader.ImageUploader.from_config', return_value=uploader), \
                mock.patch('core.manual_video.ApiClient', _FakeApiClient), \
                mock.patch('core.manual_video.VideoDownloader', _FakeDownloader):
            result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'succeeded')
        params = _FakeApiClient.create_calls[0][3]
        self.assertEqual(params['videos'], ['https://media/first.mp4', 'https://media/second.mp4'])
        self.assertEqual(params['audios'], ['https://media/audio.wav'])
        self.assertEqual(uploader.upload_file.call_args_list,
                         [mock.call(str(video), kind='videos'), mock.call(str(audio), kind='audios')])

    def test_family_reference_limits_and_v3_twenty_nine_seconds(self):
        for model, image_limit, video_limit, audio_limit, duration in (
            ('MiniMax-H3', 9, 3, 3, 15), ('video-v2', 9, 3, 3, 10),
            ('video-v3', 30, 10, 10, 29)):
            refs = {'images': ['https://img/a.png'] * image_limit,
                    'videos': ['https://media/a.mp4'] * video_limit,
                    'audios': ['https://media/a.wav'] * audio_limit}
            records = self.manager.prepare_tasks('fixture', model, duration=duration, refs=refs)
            self.assertEqual(records[0]['refs'], refs)
            for kind in refs:
                too_many = dict(refs, **{kind: refs[kind] + [refs[kind][0]]})
                with self.assertRaisesRegex(ValueError, '最多'):
                    self.manager.prepare_tasks('fixture', model, duration=duration, refs=too_many)
        with self.assertRaisesRegex(ValueError, '时长'):
            self.manager.prepare_tasks('fixture', 'video-v2', duration=6)
        with self.assertRaisesRegex(ValueError, '链接无效'):
            self._tasks(refs={'videos': ['https://']})

    def test_media_upload_failure_and_cancel_never_create_generation(self):
        path = self.root / '参考.mp4'; path.write_bytes(MP4)
        for error, expected in ((OSError('fixture'), 'failed'), (manual_video.Cancelled(), 'cancelled')):
            task = self._tasks(refs={'videos': [str(path)]})[0]
            uploader = mock.MagicMock(); uploader.upload_file.side_effect = error
            client = mock.MagicMock()
            with mock.patch('core.image_uploader.ImageUploader.from_config', return_value=uploader), \
                    mock.patch('core.manual_video.ApiClient', return_value=client):
                result = self.manager.run_tasks([task])[0]
            self.assertEqual(result['status'], expected)
            client.create_task.assert_not_called()
            uploader.close.assert_called_once()

    def test_http_failures_use_chinese_api_error_classification(self):
        from core.http_client import RequestError
        error = RequestError('HTTP 429: too many requests', 429, 'too many requests')
        self.assertEqual(self.manager._classify_failure(error),
                         ('上游限流（429），请稍后重试或更换模型', 'rate_limit', 429, True))

    def test_reference_count_400_is_chinese_and_never_resubmits(self):
        for kind, label, limit in (('videos', '视频', 3), ('audios', '音频', 5), ('images', '图片', 9)):
            with self.subTest(kind=kind):
                error = RequestError('provider rejected', 400,
                                     '{"error":"%s supports at most %s reference %s"}' % (kind, limit, kind))
                client = mock.MagicMock()
                client.create_task.side_effect = error
                task = self._tasks()[0]
                with mock.patch('core.manual_video.ApiClient', return_value=client):
                    result = self.manager.run_tasks([task])[0]
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['error'], f'上游该模型最多支持 {limit} 个参考{label}')
                self.assertEqual(result['error_category'], 'invalid_parameters')
                self.assertFalse(result['retryable'])
                client.create_task.assert_called_once()
                client.query_task.assert_not_called()

    def test_wan_and_v3_real_payload_limits_and_sizes_match_ui_capabilities(self):
        from core.video_capabilities import capabilities
        from core.model_parameters import validate_task_parameters
        for model, video_limit, audio_limit in (('video-v3', 10, 10),):
            caps = capabilities(model)
            for ratio in caps['ratios']:
                size = caps['output_sizes'][ratio][0]
                refs = {'videos': ['https://fixture.test/reference.mp4'] * video_limit,
                        'audios': ['https://fixture.test/reference.wav'] * audio_limit}
                task = self.manager.prepare_tasks('fixture', model, ratio=ratio, output_size=size,
                                                  duration=29, refs=refs)[0]
                _FakeApiClient.create_calls = []
                with mock.patch('core.manual_video.ApiClient', _FakeApiClient), \
                        mock.patch('core.manual_video.VideoDownloader', _FakeDownloader), \
                        mock.patch('core.image_uploader.ImageUploader.from_config') as uploader:
                    result = self.manager.run_tasks([task])[0]
                self.assertEqual(result['status'], 'succeeded')
                uploader.assert_not_called()
                model_id, prompt, images, params, catalog = _FakeApiClient.create_calls[0]
                payload = validate_task_parameters(model_id, prompt, params, len(images), catalog)
                self.assertEqual(payload['videos'], refs['videos'])
                self.assertEqual(payload['audios'], refs['audios'])
                self.assertEqual(payload['resolution'], '720p')
                self.assertEqual(payload.get('duration', int(payload.get('seconds', 0))), 29)
                invalid = dict(params, videos=params['videos'] + ['https://fixture.test/fourth.mp4'])
                with self.assertRaisesRegex(ValueError, '参考视频'):
                    validate_task_parameters(model_id, prompt, invalid, 0, catalog)
        with self.assertRaisesRegex(ValueError, '画面比例'):
            self.manager.prepare_tasks('fixture', 'video-v3', ratio='7:11', duration=29)
        with self.assertRaisesRegex(ValueError, '输出尺寸'):
            self.manager.prepare_tasks('fixture', 'video-v3', output_size='4K', duration=29)

    def test_catalog_advertised_wan_uses_runtime_limits_without_becoming_builtin(self):
        """Wan is provider-directory data only; never guess it in offline mode."""
        from core.model_catalog import builtin_models
        from core.video_capabilities import capabilities
        from core.model_parameters import validate_task_parameters

        catalog = {
            'wan-3.0': {
                'id': 'wan-3.0', 'name': 'wan-3.0', 'kind': 'video', 'family': '',
                'available': True, 'protocol_known': True,
                'ratios': ['16:9', '9:16'], 'resolutions': ['720p'],
                'durations': [5, 10, 15, 30], 'audio': False, 'seed': False,
                'max_images': 10, 'max_videos': 3, 'max_audios': 5,
            },
        }
        self.config.config['_model_catalog'] = catalog
        self.assertNotIn('wan-3.0', builtin_models())
        caps = capabilities('wan-3.0', catalog)
        self.assertEqual(caps['durations'], (5, 10, 15, 30))
        self.assertEqual((caps['max_images'], caps['max_videos'], caps['max_audios']), (10, 3, 5))

        refs = {
            'images': ['https://img/a.png'] * 10,
            'videos': ['https://media/a.mp4'] * 3,
            'audios': ['https://media/a.wav'] * 5,
        }
        task = self.manager.prepare_tasks('fixture', 'wan-3.0', ratio='16:9',
                                          output_size='720p', duration=30, refs=refs)[0]
        _FakeApiClient.create_calls = []
        with mock.patch('core.manual_video.ApiClient', _FakeApiClient), \
                mock.patch('core.manual_video.VideoDownloader', _FakeDownloader):
            result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'succeeded')
        model, prompt, images, params, passed_catalog = _FakeApiClient.create_calls[0]
        self.assertEqual(model, 'wan-3.0')
        self.assertIs(passed_catalog, catalog)
        self.assertEqual(len(images), 10)
        fields = validate_task_parameters(model, prompt, params, len(images), catalog)
        self.assertEqual(fields['seconds'], '30')
        self.assertEqual(fields['resolution'], '720p')
        self.assertEqual(len(fields['videos']), 3)
        self.assertEqual(len(fields['audios']), 5)
        for kind, limit in (('images', 10), ('videos', 3), ('audios', 5)):
            too_many = dict(refs, **{kind: refs[kind] + [refs[kind][0]]})
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, '最多'):
                self.manager.prepare_tasks('fixture', 'wan-3.0', ratio='16:9',
                                            output_size='720p', duration=30, refs=too_many)

    def test_resolution_mapping_and_veo_inline_video_data(self):
        self.assertEqual(self.manager._api_resolution('video-v2', '1920x1088'), '1080p')
        self.assertEqual(self.manager._api_resolution('MiniMax-H3', '1088x1920'), '1080p')
        encoded = base64.b64encode(MP4).decode('ascii')
        done, progress, url, data = manual_video._operation_result({
            'done': True,
            'response': {'generatedVideos': [{'video': {'bytesBase64Encoded': encoded}}]},
        })
        self.assertTrue(done)
        self.assertEqual(progress, None)
        self.assertIsNone(url)
        self.assertEqual(data, MP4)

    def test_download_task_persists_remote_result_path(self):
        record = dict(local_id='remote-fixture', status='succeeded', task_id='remote-1',
                      video_url='https://relay.example/output.mp4', count_index=1,
                      prompt_name='fixture video', output_dir='', file_path='')
        _FakeDownloader.api_keys = []
        with mock.patch('core.manual_video.VideoDownloader', _FakeDownloader):
            path = self.manager.download_task(record)
        stored = self.store.get('remote-fixture')
        self.assertEqual(stored['file_path'], path)
        self.assertEqual(Path(path).read_bytes(), MP4)
        self.assertEqual(_FakeDownloader.api_keys, ['fixture-secret'])
        self.assertEqual(stored['status'], 'succeeded')
        self.assertEqual(stored['size_bytes'], len(MP4))
        self.assertGreaterEqual(stored['download_ms'], 0)
        self.assertGreater(stored['throughput_bps'], 0)
        self.assertEqual([item['status'] for item in self.callbacks[-2:]],
                         ['downloading', 'succeeded'])

    def test_manual_download_retries_and_publishes_failed_record(self):
        record = dict(local_id='remote-retry', status='failed', task_id='remote-2',
                      video_url='https://relay.example/output.mp4', count_index=1,
                      prompt_name='fixture video', output_dir='', file_path='')
        attempts = []

        class _FlakyDownloader:
            def __init__(self, *args, **kwargs):
                pass

            def download_video(self, _url, save_dir, filename, **kwargs):
                attempts.append(1)
                if len(attempts) < 3:
                    raise OSError('temporary stream failure')
                target = Path(save_dir) / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(MP4)
                return str(target)

            def close(self):
                pass

        with mock.patch('core.manual_video.VideoDownloader', _FlakyDownloader), \
                mock.patch('core.manual_video.time.sleep') as pause:
            path = self.manager.download_task(record)
        self.assertTrue(Path(path).is_file())
        self.assertEqual(len(attempts), 3)
        self.assertEqual(pause.call_args_list, [mock.call(1), mock.call(2)])
        self.assertEqual(self.store.get('remote-retry')['status'], 'succeeded')

    def test_cancel_during_download_does_not_publish_success(self):
        task = self._tasks()[0]
        downloaded_paths = []

        class _CancelAfterWriteDownloader:
            def __init__(inner_self, *args, **kwargs):
                inner_self.check_cancel = kwargs['check_cancel']

            def download_video(inner_self, _url, save_dir, filename, **kwargs):
                target = Path(save_dir) / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(MP4)
                downloaded_paths.append(target)
                self.manager.cancel()
                with self.assertRaises(manual_video.Cancelled):
                    inner_self.check_cancel()
                return str(target)

            def close(inner_self):
                pass

        with mock.patch('core.manual_video.ApiClient', _FakeApiClient), \
                mock.patch('core.manual_video.VideoDownloader', _CancelAfterWriteDownloader):
            result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'cancelled')
        self.assertNotEqual(result['status'], 'succeeded')
        self.assertTrue(downloaded_paths[0].is_file())

    def test_cancelled_batch_event_is_retained_and_new_batch_gets_fresh_event(self):
        first = self._tasks()[0]
        second = self._tasks()[0]
        first_started = threading.Event()
        release_first = threading.Event()
        observed_events = []

        def fake_execute(record, _line, _output_root, cancel_event):
            observed_events.append(self.manager._cancel_event)
            if record['local_id'] == first['local_id']:
                first_started.set()
                release_first.wait(2)
            cancelled = self.manager._is_cancelled(cancel_event)
            return self.manager._finish(
                record, time.monotonic(), 'cancelled' if cancelled else 'succeeded')

        results = {}
        with mock.patch.object(self.manager, '_execute_one', side_effect=fake_execute):
            first_thread = threading.Thread(
                target=lambda: results.setdefault('first', self.manager.run_tasks([first])[0]),
                daemon=True)
            first_thread.start()
            self.assertTrue(first_started.wait(2))
            cancelled_event = self.manager._cancel_event
            self.manager.cancel()
            release_first.set()
            first_thread.join(2)

            self.assertEqual(results['first']['status'], 'cancelled')
            self.assertTrue(cancelled_event.is_set())
            results['second'] = self.manager.run_tasks([second])[0]

        self.assertEqual(results['second']['status'], 'succeeded')
        self.assertEqual(len(observed_events), 2)
        self.assertIsNot(observed_events[1], cancelled_event)
        self.assertFalse(observed_events[1].is_set())

    def test_protocol_and_concurrency_are_bounded_to_two(self):
        _FakeApiClient.active = _FakeApiClient.peak = 0
        with mock.patch('core.manual_video.ApiClient', _FakeApiClient), \
                mock.patch('core.manual_video.VideoDownloader', _FakeDownloader):
            results = self.manager.run_tasks(self._tasks(count=6))
        self.assertTrue(all(row['status'] == 'succeeded' for row in results))
        self.assertLessEqual(_FakeApiClient.peak, 2)

    def test_api_client_selects_video_v1_and_videos_endpoints(self):
        _OpenAIHandler.requests_seen = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), _OpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.config.config['api']['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
        v1 = self._tasks(model='video-v1')[0]
        v2 = self._tasks(model='video-v2')[0]
        results = self.manager.run_tasks([v1, v2])
        self.assertEqual([item['status'] for item in results], ['succeeded', 'succeeded'])
        posts = [path for method, path, _ in _OpenAIHandler.requests_seen if method == 'POST']
        # The manager intentionally runs independent tasks concurrently; the
        # endpoint request order is scheduler-dependent while both routes must
        # be exercised exactly once.
        self.assertCountEqual(posts, ['/v1/video/generations', '/v1/videos'])
        queries = [path for method, path, _ in _OpenAIHandler.requests_seen if method == 'GET'
                   and path.endswith('fixture-task')]
        self.assertCountEqual(
            queries,
            ['/v1/video/generations/fixture-task', '/v1/videos/fixture-task'],
        )

    def test_veo_helpers_send_expected_request_and_parse_operation(self):
        _VeoHandler.requests_seen = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), _VeoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}/v1'
        operation = create_veo_task(base, 'fixture-secret', 'veo-3', 'make a video',
                                    ratio='9:16', duration=5, output_size='1080p')
        self.assertEqual(operation['name'], 'operations/fixture-video')
        payload = fetch_veo_operation(base, 'fixture-secret', 'veo-3', operation['name'])
        self.assertTrue(payload['done'])
        self.assertEqual(_VeoHandler.requests_seen[0]['path'], '/v1/models/veo-3:predictLongRunning')
        request = _VeoHandler.requests_seen[0]
        self.assertEqual(request['headers']['authorization'], 'Bearer fixture-secret')
        self.assertEqual(request['headers']['x-goog-api-key'], 'fixture-secret')
        self.assertEqual(request['json']['parameters']['aspectRatio'], '9:16')
        self.assertEqual(request['json']['parameters']['durationSeconds'], 5)

    def test_non_json_http_error_uses_status_classification(self):
        response = SimpleNamespace(status_code=429, text='upstream quota exceeded')
        response.json = mock.Mock(side_effect=ValueError('not json'))
        client = SimpleNamespace(redact=lambda value: value)
        with self.assertRaises(manual_video.ManualVideoError) as caught:
            manual_video._json_response(client, response, api_key='fixture-secret')
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.category, 'rate_limit')
        self.assertEqual(str(caught.exception), '上游限流（429），请稍后重试或更换模型')

    def test_veo_manager_polls_and_downloads_mp4(self):
        _VeoHandler.requests_seen = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), _VeoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.config.config['api']['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
        task = self._tasks(model='veo-3')[0]
        result = self.manager.run_tasks([task])[0]
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['protocol'], 'gemini')
        self.assertEqual(Path(result['file_path']).read_bytes(), MP4)

    def test_validation_and_cancellation(self):
        with self.assertRaises(ValueError):
            self.manager.prepare_tasks('', 'video-v2')
        with self.assertRaises(ValueError):
            self.manager.prepare_tasks('x', 'video-v2', count=11)
        event = threading.Event()
        event.set()
        task = self._tasks()[0]
        with mock.patch('core.manual_video.ApiClient', _FakeApiClient):
            result = self.manager.run_tasks([task], event)[0]
        self.assertEqual(result['status'], 'cancelled')

    def test_overlapping_batches_preserve_manager_cancellation(self):
        """A completed batch must not clear cancellation for a sibling batch."""
        first_task = self._tasks()[0]
        second_task = self._tasks()[0]
        first_started = threading.Event()
        first_release = threading.Event()
        first_batch_done = threading.Event()
        second_started = threading.Event()
        second_release = threading.Event()
        results = {}

        def fake_execute(record, _line, _output_root, cancel_event):
            started = time.monotonic()
            self.manager._publish(dict(record, status='running'))
            if record['local_id'] == first_task['local_id']:
                first_started.set()
                first_release.wait(2)
            else:
                second_started.set()
                # Let the first run_tasks() return while this batch is still
                # active; the old unconditional clear() raced here.
                first_batch_done.wait(2)
                second_release.wait(2)
            cancelled = self.manager._is_cancelled(cancel_event)
            return self.manager._finish(
                record, started, 'cancelled' if cancelled else 'succeeded',
                '任务已取消' if cancelled else '')

        def run(name, task, done):
            results[name] = self.manager.run_tasks([task])[0]
            done.set()

        with mock.patch.object(self.manager, '_execute_one', side_effect=fake_execute):
            first_thread = threading.Thread(
                target=run, args=('first', first_task, first_batch_done), daemon=True)
            first_thread.start()
            self.assertTrue(first_started.wait(2))

            second_thread = threading.Thread(
                target=run, args=('second', second_task, threading.Event()), daemon=True)
            second_thread.start()
            self.assertTrue(second_started.wait(2))

            self.manager.cancel()
            first_release.set()
            self.assertTrue(first_batch_done.wait(2))
            second_release.set()
            first_thread.join(2)
            second_thread.join(2)

        self.assertEqual(results['first']['status'], 'cancelled')
        self.assertEqual(results['second']['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
