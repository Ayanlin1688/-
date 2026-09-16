"""Regression coverage for paid task output ownership using local HTTP only."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from core.video_downloader import VideoDownloader
from core.config_manager import DEFAULT_CONFIG
from core.task_manager import TaskWorker
from test_http_clients import LocalServer


class DownloadHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        data = ('video bytes: ' + self.path).encode('ascii')
        self.send_response(200)
        if self.path == '/html':
            self.send_header('Content-Type', 'text/html; charset=utf-8')
        elif self.path == '/json':
            self.send_header('Content-Type', 'application/problem+json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if self.server.barrier is not None:
            self.server.barrier.wait(timeout=5)
        self.wfile.write(data)


class DownloadOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), DownloadHandler)
        self.server.paths = []
        self.server.barrier = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.downloader = VideoDownloader(log=lambda *args: None)

    def tearDown(self):
        self.downloader.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_different_results_with_same_filename_keep_both_videos(self):
        first = Path(self.downloader.download_video(self.base + '/first', self.folder, '001_01.mp4'))
        second = Path(self.downloader.download_video(self.base + '/second', self.folder, '001_01.mp4'))
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b'video bytes: /first')
        self.assertEqual(second.read_bytes(), b'video bytes: /second')

    def test_unowned_existing_file_is_preserved_and_not_reported_as_new(self):
        original = self.folder / '001_01.mp4'
        original.write_bytes(b'user-owned-video')
        result = Path(self.downloader.download_video(self.base + '/new', self.folder, original.name))
        self.assertNotEqual(result, original)
        self.assertEqual(original.read_bytes(), b'user-owned-video')
        self.assertEqual(result.read_bytes(), b'video bytes: /new')

    def test_same_result_can_resume_without_another_download(self):
        first = self.downloader.download_video(self.base + '/first', self.folder, '001_01.mp4')
        second = self.downloader.download_video(self.base + '/first', self.folder, '001_01.mp4')
        self.assertEqual(first, second)
        self.assertEqual(self.server.paths, ['/first'])

    def test_same_paid_task_resumes_even_when_signed_url_changes(self):
        first = self.downloader.download_video(self.base + '/first', self.folder, '001_01.mp4',
                                               generation_key=('account', 'task-one'))
        second = self.downloader.download_video(self.base + '/renewed', self.folder, '001_01.mp4',
                                                generation_key=('account', 'task-one'))
        self.assertEqual(first, second)
        self.assertEqual(self.server.paths, ['/first'])

    def test_distinct_paid_tasks_sharing_url_do_not_share_output(self):
        first = self.downloader.download_video(self.base + '/shared', self.folder, '001_01.mp4',
                                               generation_key=('account', 'task-one'))
        second = self.downloader.download_video(self.base + '/shared', self.folder, '001_01.mp4',
                                                generation_key=('account', 'task-two'))
        self.assertNotEqual(first, second)
        self.assertEqual(self.server.paths, ['/shared', '/shared'])

    def test_modified_owned_video_is_preserved_and_recovered_to_new_file(self):
        first = Path(self.downloader.download_video(self.base + '/first', self.folder, '001_01.mp4'))
        altered = b'x' * first.stat().st_size
        first.write_bytes(altered)
        second = Path(self.downloader.download_video(self.base + '/first', self.folder, '001_01.mp4'))
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), altered)
        self.assertEqual(second.read_bytes(), b'video bytes: /first')

    def test_overwrite_setting_cannot_replace_a_different_generation(self):
        first = Path(self.downloader.download_video(self.base + '/first', self.folder, '001_01.mp4',
                                                    generation_key=('account', 'task-one')))
        self.downloader.overwrite_existing = True
        second = Path(self.downloader.download_video(self.base + '/second', self.folder, '001_01.mp4',
                                                     generation_key=('account', 'task-two')))
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b'video bytes: /first')
        self.assertEqual(second.read_bytes(), b'video bytes: /second')

    def test_html_and_json_error_responses_are_not_saved_as_videos(self):
        for endpoint in ['/html', '/json']:
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(IOError, 'Content-Type'):
                    self.downloader.download_video(self.base + endpoint, self.folder, endpoint[1:] + '.mp4')
                self.assertFalse(list(self.folder.glob('*.mp4')))
                self.assertFalse(list(self.folder.glob('*.part')))

    def test_broken_output_symlink_cannot_escape_output_folder(self):
        with tempfile.TemporaryDirectory() as outside:
            link = self.folder / '001_01.mp4'
            try:
                link.symlink_to(Path(outside) / 'missing.mp4')
            except OSError as error:
                self.skipTest(f'Symlink unavailable: {error}')
            with self.assertRaises(ValueError):
                self.downloader._destination(self.folder.resolve(), link.name, 'identity')

    def test_simultaneous_same_name_downloads_do_not_discard_either_result(self):
        self.server.barrier = threading.Barrier(2)

        def download(suffix):
            client = VideoDownloader(log=lambda *args: None)
            try:
                return Path(client.download_video(self.base + suffix, self.folder, '001_01.mp4'))
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            paths = list(pool.map(download, ['/first', '/second']))
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual([path.read_bytes() for path in paths],
                         [b'video bytes: /first', b'video bytes: /second'])


class TaskOutputOwnershipTests(unittest.TestCase):
    def test_changed_prompt_creates_new_task_and_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as directory, LocalServer() as server:
            root = Path(directory)
            prompts = root / 'prompts'
            prompts.mkdir()
            prompt = prompts / '01.txt'
            prompt.write_text('Original product camera movement', encoding='utf-8')
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['paths'] = dict(prompts=str(prompts), images='', output=str(root / 'output'))
            config['api'].update(base_url=server.base, api_key='local-test')
            config['task_strategy'].update(max_concurrency=1, auto_retry=False, unmatched_prompt='仍提交文生视频')
            config['workspace']['poll_interval'] = .01
            first = TaskWorker(config, [])
            first.run()
            self.assertEqual(first.tasks[0]['status'], 'completed', first.tasks[0])
            first_path = Path(first.tasks[0]['result_path'])
            first_bytes = first_path.read_bytes()
            prompt.write_text('Updated product camera movement', encoding='utf-8')
            second = TaskWorker(config, first.tasks)
            second.run()
            self.assertEqual(second.tasks[0]['status'], 'completed', second.tasks[0])
            self.assertNotEqual(first.tasks[0]['task_id'], second.tasks[0]['task_id'])
            self.assertNotEqual(first_path, Path(second.tasks[0]['result_path']))
            self.assertEqual(first_path.read_bytes(), first_bytes)
            self.assertEqual(sum(path == '/download' for path in server.gets), 2)


class RefreshDownloadHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.posts.append(self.path)
        self.send_response(500)
        self.end_headers()

    def do_GET(self):
        self.server.gets.append(self.path)
        if self.path == '/videos/existing-paid-task':
            destination = '/expired' if self.server.keep_expired else '/fresh'
            data = json.dumps(dict(status='completed', result_url=self.server.base + destination)).encode('utf-8')
            status = 200
        else:
            data = b'renewed-video-bytes'
            status = self.server.expired_status if self.path == '/expired' else 200
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ExpiredDownloadRecoveryTests(unittest.TestCase):
    def run_case(self, status, keep_expired=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompts = root / 'prompts'
            prompts.mkdir()
            (prompts / '01.txt').write_text('Existing paid product video', encoding='utf-8')
            server = ThreadingHTTPServer(('127.0.0.1', 0), RefreshDownloadHandler)
            server.base = f'http://127.0.0.1:{server.server_port}'
            server.expired_status = status
            server.keep_expired = keep_expired
            server.gets, server.posts = [], []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                config = copy.deepcopy(DEFAULT_CONFIG)
                config['paths'] = dict(prompts=str(prompts), images='', output=str(root / 'output'))
                config['api'].update(base_url=server.base, api_key='local-test')
                config['task_strategy'].update(max_concurrency=1, auto_retry=True, max_retries=1,
                                               retry_interval=0, unmatched_prompt='仍提交文生视频')
                worker = TaskWorker(config, [])
                worker._scan()
                worker.tasks[0].update(task_id='existing-paid-task', result_url=server.base + '/expired')
                worker._execute(0, worker.tasks[0]['model'])
                task = worker.tasks[0]
                data = Path(task['result_path']).read_bytes() if task.get('result_path') else None
                return task, server.gets, server.posts, data
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_401_and_403_refresh_only_existing_task_and_download_new_url(self):
        for status in (401, 403):
            with self.subTest(status=status):
                task, gets, posts, data = self.run_case(status)
                self.assertEqual(task['status'], 'completed', task)
                self.assertEqual(task['task_id'], 'existing-paid-task')
                self.assertEqual(gets, ['/expired', '/videos/existing-paid-task', '/fresh'])
                self.assertEqual(posts, [])
                self.assertEqual(data, b'renewed-video-bytes')

    def test_other_http_errors_use_existing_retry_limit_without_url_refresh(self):
        task, gets, posts, _ = self.run_case(500)
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['task_id'], 'existing-paid-task')
        self.assertEqual(gets, ['/expired', '/expired'])
        self.assertEqual(posts, [])

    def test_repeated_expiry_stops_at_retry_limit_and_preserves_task_id(self):
        task, gets, posts, _ = self.run_case(403, keep_expired=True)
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['task_id'], 'existing-paid-task')
        self.assertEqual(gets, ['/expired', '/videos/existing-paid-task', '/expired'])
        self.assertEqual(posts, [])
        self.assertFalse(task['result_url'])


class ActiveTaskGateRecoveryTests(unittest.TestCase):
    def test_throttle_recovery_keeps_user_pause_visible(self):
        worker = TaskWorker(copy.deepcopy(DEFAULT_CONFIG), [])
        states = []
        worker.pause_changed.connect(states.append)
        worker.control.set('paused', True)
        worker._gate_recovered()
        self.assertEqual(states, [True])

    def test_cooldown_expiry_admits_next_task_while_existing_task_is_processing(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['task_strategy']['max_concurrency'] = 5
        config['_submission_probe_seconds'] = .05
        worker = TaskWorker(config, [])
        worker.tasks = [dict(model='video-v3'), dict(model='video-v3')]
        paused = threading.Event()
        second_started = threading.Event()
        first_finished = threading.Event()
        started_before_finish = []

        def execute(index, model):
            if index == 0:
                worker.tasks[index]['status'] = 'processing'
                for _ in range(3):
                    worker.gate.failed(429)
                paused.set()
                second_started.wait(timeout=2)
                first_finished.set()
            else:
                started_before_finish.append(not first_finished.is_set())
                second_started.set()

        class PausedBeforeNextAdmission(ThreadPoolExecutor):
            def submit(self, function, *args, **kwargs):
                future = super().submit(function, *args, **kwargs)
                if args[0] == 0:
                    if not paused.wait(timeout=2):
                        raise AssertionError('First task did not set the shared throttle')
                return future

        worker._execute = execute
        with patch('core.task_manager.ThreadPoolExecutor', PausedBeforeNextAdmission):
            worker._concurrent_batch([0, 1])
        self.assertEqual(started_before_finish, [True])
        self.assertFalse(worker.gate.paused)


if __name__ == '__main__':
    unittest.main()
