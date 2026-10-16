"""Download acceleration and integrity checks against local, unpaid HTTP fixtures."""
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

from core.http_client import Cancelled
from core.video_downloader import DOWNLOAD_ATTEMPTS, DOWNLOAD_CHUNK_SIZE, PARALLEL_MIN_BYTES, VideoDownloader


VIDEO = (bytes(range(251)) * (PARALLEL_MIN_BYTES // 251 + 1))[:PARALLEL_MIN_BYTES] + b'last uneven segment'
CHANGED_VIDEO = VIDEO.translate(bytes((value + 73) % 256 for value in range(256)))


class RangeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.server.heads.append(self.path)
        self.send_error(405)

    def do_GET(self):
        try:
            self.respond()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def respond(self):
        server = self.server
        range_header = self.headers.get('Range', '')
        bounded = bool(range_header and not range_header.endswith('-'))
        with server.lock:
            server.calls.append(dict(range=range_header, headers=dict(self.headers), path=self.path))
            if not range_header:
                server.full_gets += 1
            full_gets = server.full_gets
        mode = server.mode
        data = server.data
        etag = '"version-one"'
        if mode in {'changed-etag', 'changed-size'} and (bounded or full_gets > 1):
            data = CHANGED_VIDEO if mode == 'changed-etag' else CHANGED_VIDEO + b'new tail'
            etag = '"version-two"'
        if mode.startswith('resume-change') and range_header:
            data = bytes((value + 1) % 256 for value in data)
            etag = '"version-two"'
        if mode.startswith('resume-change') and not range_header and full_gets > 1:
            data = bytes((value + 1) % 256 for value in data)
            etag = '"version-two"'
        if mode == 'weak-etag':
            etag = 'W/"version-one"'
        start, end = 0, len(data) - 1
        status = 200
        if range_header and mode not in {'ignore-range', 'resume-change-ignore'}:
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', range_header)
            if not match:
                self.send_error(400)
                return
            start = int(match.group(1))
            end = min(int(match.group(2)), end) if match.group(2) else end
            status = 206
        elif mode == 'initial-206' and not range_header:
            status = 206
        if bounded and mode == 'range-forbidden':
            status = 403
        body = data[start:end + 1]
        encoding = ''
        if mode == 'gzip' or (bounded and mode == 'encoded-range'):
            encoding = 'gzip'
            body = gzip.compress(body)
        if bounded:
            server.ranges_started.set()
            if server.barrier is not None:
                server.barrier.wait(timeout=5)
            if mode == 'prefix-disconnect' and start == len(data) // 4:
                if not server.prefix_ready.wait(timeout=5):
                    raise AssertionError('Verified first segment never completed')
        self.send_response(status)
        self.send_header('Content-Type', 'video/mp4')
        if mode not in {'no-range', 'initial-206'}:
            self.send_header('Accept-Ranges', 'bytes')
        if mode not in {'no-etag', 'resume-no-etag'}:
            self.send_header('ETag', etag)
        self.send_header('Set-Cookie', 'download=fixture; Path=/')
        if encoding:
            self.send_header('Content-Encoding', encoding)
        if status == 206 and not (bounded and mode == 'missing-content-range'):
            range_start, range_end, total = start, end, len(data)
            if bounded and mode == 'wrong-start':
                range_start += 1
            if bounded and mode == 'wrong-end':
                range_end -= 1
            if bounded and mode == 'wrong-total':
                total += 1
            if not bounded and range_header and mode == 'resume-wrong-start':
                range_start = max(0, start - 1)
            value = f'bytes {range_start}-{range_end}/{total}'
            if bounded and mode == 'wildcard-total':
                value = f'bytes {start}-{end}/*'
            self.send_header('Content-Range', value)
        if not (bounded and mode in {'extra-body', 'short-without-length'}):
            declared_length = len(body) + (1 if bounded and mode == 'wrong-length' else 0)
            self.send_header('Content-Length', str(declared_length))
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        if bounded and mode == 'cancel':
            server.release.wait(timeout=5)
        if mode == 'first-byte':
            self.wfile.write(body[:65536])
            self.wfile.flush()
            server.early_release = server.release.wait(timeout=2)
            self.wfile.write(body[65536:])
            return
        truncated = (mode == 'always-truncated'
                     or (mode in {'resume', 'resume-no-etag', 'resume-change-206', 'resume-change-ignore', 'resume-wrong-start'}
                         and not range_header and full_gets == 1)
                     or (bounded and mode == 'prefix-disconnect' and start == len(data) // 4))
        if truncated:
            self.wfile.write(body[:65536])
        elif bounded and mode in {'short-body', 'short-without-length'}:
            self.wfile.write(body[:1000])
        elif bounded and mode == 'extra-body':
            self.wfile.write(body + b'unexpected bytes')
        else:
            self.wfile.write(body)


class LocalRangeServer:
    def __init__(self, mode='range', data=VIDEO):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), RangeHandler)
        self.server.daemon_threads = False
        self.server.mode = mode
        self.server.data = data
        self.server.calls = []
        self.server.heads = []
        self.server.full_gets = 0
        self.server.lock = threading.Lock()
        self.server.ranges_started = threading.Event()
        self.server.prefix_ready = threading.Event()
        self.server.release = threading.Event()
        self.server.barrier = None
        self.server.early_release = False
        self.server.url = f'http://127.0.0.1:{self.server.server_port}/video?signature=local-signed-secret'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server

    def __exit__(self, *args):
        self.server.release.set()
        self.server.prefix_ready.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = {'Content-Length': str(len(body)), 'Content-Type': 'video/mp4', **(headers or {})}
        self.chunk_sizes = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size):
        self.chunk_sizes.append(chunk_size)
        yield b''
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, kwargs))
        return next(self.responses)

    def close(self):
        pass


class DownloadAccelerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.logs = []
        self.client = VideoDownloader(api_key='provider-key-must-not-leak', log=lambda message, level: self.logs.append(message))
        self.addCleanup(self.client.close)

    def assert_complete(self, path, data):
        self.assertEqual(path.read_bytes(), data)
        record = json.loads(self.client._ownership_path(path).read_text(encoding='utf-8'))
        self.assertEqual(record['size_bytes'], len(data))
        self.assertEqual(record['sha256'], hashlib.sha256(data).hexdigest())
        self.assert_no_temporary()

    def assert_no_temporary(self):
        self.assertFalse(list(self.folder.glob('*.part')))
        self.assertFalse(list(self.folder.glob('.storyboard-ranges-*')))

    def download(self, server):
        return Path(self.client.download_video(server.url, self.folder, 'result.mp4'))

    def test_parallel_ranges_merge_uneven_payload_in_order_with_isolated_sessions(self):
        clients = []
        create_client = self.client._segment_client

        def record_client():
            client = create_client()
            clients.append(client)
            return client

        self.client.session.headers['X-Download-Marker'] = 'kept'
        self.client.session.cookies.set('custom', 'cookie')
        self.client.session.trust_env = False
        with LocalRangeServer() as server, patch.object(self.client, '_segment_client', side_effect=record_client):
            server.barrier = threading.Barrier(4)
            path = self.download(server)
            self.assert_complete(path, VIDEO)
            self.assertEqual(server.heads, [])
            self.assertEqual(server.full_gets, 1)
            ranges = sorted((call for call in server.calls if call['range']), key=lambda call: int(call['range'][6:].split('-')[0]))
            self.assertEqual(len(ranges), 4)
            expected = [f'bytes={len(VIDEO) * index // 4}-{len(VIDEO) * (index + 1) // 4 - 1}' for index in range(4)]
            self.assertEqual([call['range'] for call in ranges], expected)
            for call in server.calls:
                self.assertNotIn('Authorization', call['headers'])
                self.assertEqual(call['headers']['Accept-Encoding'], 'identity')
                self.assertEqual(call['headers']['X-Download-Marker'], 'kept')
            for call in ranges:
                self.assertEqual(call['headers']['If-Range'], '"version-one"')
                self.assertIn('download=fixture', call['headers']['Cookie'])
                self.assertIn('custom=cookie', call['headers']['Cookie'])
            self.assertEqual(len({id(client.session) for client in clients}), 4)
            self.assertTrue(all(client.session is not self.client.session for client in clients))
            self.assertTrue(all(not client.session.trust_env for client in clients))
        self.assertTrue(any('启用分段下载：4 段' in message for message in self.logs))

    def test_worker_count_is_configurable(self):
        self.client.parallel_workers = 3
        with LocalRangeServer() as server:
            server.barrier = threading.Barrier(3)
            self.assert_complete(self.download(server), VIDEO)
            self.assertEqual(len([call for call in server.calls if call['range']]), 3)

    def test_disabled_acceleration_uses_one_get(self):
        self.client.acceleration = False
        with LocalRangeServer() as server:
            self.assert_complete(self.download(server), VIDEO)
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(server.heads, [])

    def test_small_file_uses_one_get_without_head(self):
        data = VIDEO[:300000]
        with LocalRangeServer(data=data) as server:
            self.assert_complete(self.download(server), data)
            self.assertEqual([call['range'] for call in server.calls], [''])
            self.assertEqual(server.heads, [])

    def test_no_range_advertisement_falls_back_without_probe(self):
        with LocalRangeServer('no-range') as server:
            self.assert_complete(self.download(server), VIDEO)
            self.assertEqual(len(server.calls), 1)
        self.assertTrue(any('未声明字节范围支持' in message for message in self.logs))

    def test_missing_or_weak_etag_keeps_single_stream(self):
        for mode in ('no-etag', 'weak-etag'):
            with self.subTest(mode=mode), LocalRangeServer(mode) as server:
                self.assert_complete(self.download(server), VIDEO)
                self.assertEqual(len(server.calls), 1)

    def test_unsolicited_valid_206_can_enable_parallel_without_advertisement(self):
        with LocalRangeServer('initial-206') as server, patch.object(self.client, '_parallel_eligible', wraps=self.client._parallel_eligible) as eligible:
            self.assert_complete(self.download(server), VIDEO)
            self.assertEqual(len([call for call in server.calls if call['range']]), 4)
            self.assertNotIn('Accept-Ranges', eligible.call_args_list[0].args[0].headers)

    def test_ignored_range_discards_segments_and_restarts_single_stream(self):
        with LocalRangeServer('ignore-range') as server:
            self.assert_complete(self.download(server), VIDEO)
            self.assertEqual(server.full_gets, 2)
            self.assertFalse(any(call['range'].endswith('-') for call in server.calls))
        self.assertTrue(any('退回单流' in message and '未返回 206' in message for message in self.logs))

    def test_changed_etag_cannot_publish_mixed_versions(self):
        with LocalRangeServer('changed-etag') as server:
            self.assert_complete(self.download(server), CHANGED_VIDEO)
            self.assertEqual(server.full_gets, 2)

    def test_changed_total_cannot_publish_mixed_versions(self):
        with LocalRangeServer('changed-size') as server:
            self.assert_complete(self.download(server), CHANGED_VIDEO + b'new tail')
            self.assertEqual(server.full_gets, 2)

    def test_invalid_range_headers_fall_back_to_single_stream(self):
        modes = ('wrong-start', 'wrong-end', 'wrong-total', 'missing-content-range', 'wildcard-total', 'wrong-length')
        for mode in modes:
            with self.subTest(mode=mode), LocalRangeServer(mode) as server:
                self.assert_complete(self.download(server), VIDEO)
                self.assertEqual(server.full_gets, 2)

    def test_invalid_range_bodies_and_status_fall_back(self):
        modes = ('short-body', 'short-without-length', 'extra-body', 'encoded-range', 'range-forbidden')
        for mode in modes:
            with self.subTest(mode=mode), LocalRangeServer(mode) as server:
                self.assert_complete(self.download(server), VIDEO)
                self.assertEqual(server.full_gets, 2)

    def test_transport_failure_preserves_verified_contiguous_prefix(self):
        download_segment = self.client._download_segment

        def report_prefix(url, path, start, *args):
            result = download_segment(url, path, start, *args)
            if start == 0:
                server.prefix_ready.set()
            return result

        with LocalRangeServer('prefix-disconnect') as server, patch.object(self.client, '_download_segment', side_effect=report_prefix):
            self.assert_complete(self.download(server), VIDEO)
            resumed = [call for call in server.calls if call['range'].endswith('-')]
            self.assertEqual(len(resumed), 1)
            self.assertEqual(resumed[0]['range'], f'bytes={len(VIDEO) // 4}-')
            self.assertEqual(resumed[0]['headers']['If-Range'], '"version-one"')
            self.assertEqual(server.full_gets, 1)
        self.assertTrue(any(f'保留连续已验证前缀 {len(VIDEO) // 4} 字节' in message for message in self.logs))

    def test_64k_disconnect_resumes_with_large_read_size(self):
        data = VIDEO[:300000]
        with LocalRangeServer('resume', data) as server:
            self.assert_complete(self.download(server), data)
            self.assertEqual([call['range'] for call in server.calls], ['', 'bytes=65536-'])
            self.assertEqual(server.calls[-1]['headers']['If-Range'], '"version-one"')

    def test_legacy_resume_without_etag_is_preserved(self):
        data = VIDEO[:300000]
        with LocalRangeServer('resume-no-etag', data) as server, patch.object(self.client, '_parallel_eligible', wraps=self.client._parallel_eligible) as eligible:
            self.assert_complete(self.download(server), data)
            self.assertEqual(server.calls[-1]['range'], 'bytes=65536-')
            self.assertTrue(all('If-Range' not in call['headers'] for call in server.calls))
            self.assertTrue(all('ETag' not in call.args[0].headers for call in eligible.call_args_list))

    def test_resume_changed_etag_restarts_without_mixing(self):
        data = VIDEO[:300000]
        with LocalRangeServer('resume-change-206', data) as server:
            self.assert_complete(self.download(server), bytes((value + 1) % 256 for value in data))
            self.assertEqual([call['range'] for call in server.calls], ['', 'bytes=65536-', ''])

    def test_resume_ignored_range_restarts_and_truncates_partial(self):
        data = VIDEO[:300000]
        with LocalRangeServer('resume-change-ignore', data) as server:
            self.assert_complete(self.download(server), bytes((value + 1) % 256 for value in data))
            self.assertEqual([call['range'] for call in server.calls], ['', 'bytes=65536-'])

    def test_resume_wrong_start_restarts_instead_of_appending(self):
        data = VIDEO[:300000]
        with LocalRangeServer('resume-wrong-start', data) as server:
            self.assert_complete(self.download(server), data)
            self.assertEqual([call['range'] for call in server.calls], ['', 'bytes=65536-', ''])

    def test_retry_budget_and_existing_unowned_output_are_preserved(self):
        original = self.folder / 'result.mp4'
        original.write_bytes(b'user-owned-output')
        with LocalRangeServer('always-truncated', VIDEO[:1000000]) as server:
            with self.assertRaises(Exception):
                self.download(server)
            self.assertEqual(len(server.calls), DOWNLOAD_ATTEMPTS)
        self.assertEqual(original.read_bytes(), b'user-owned-output')
        self.assertEqual(list(self.folder.iterdir()), [original])

    def test_parallel_cancel_propagates_without_fallback_or_publication(self):
        with LocalRangeServer('cancel') as server:
            def check_cancel():
                if server.ranges_started.is_set():
                    server.release.set()
                    raise Cancelled()
            self.client.check_cancel = check_cancel
            with self.assertRaises(Cancelled):
                self.download(server)
            self.assertEqual(server.full_gets, 1)
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertFalse(any(thread.name.startswith('video-range') for thread in threading.enumerate()))

    def test_cancel_during_merge_preserves_owned_output_and_manifest(self):
        with LocalRangeServer() as server:
            path = self.download(server)
            record = self.client._ownership_path(path).read_bytes()
            self.client.overwrite_existing = True
            cancel = threading.Event()
            write_chunk = self.client._write_chunk

            def write_and_cancel(*args):
                write_chunk(*args)
                cancel.set()

            def check_cancel():
                if cancel.is_set():
                    raise Cancelled()

            self.client.check_cancel = check_cancel
            with patch.object(self.client, '_write_chunk', side_effect=write_and_cancel), self.assertRaises(Cancelled):
                self.download(server)
            self.assert_complete(path, VIDEO)
            self.assertEqual(self.client._ownership_path(path).read_bytes(), record)

    def test_parallel_owned_file_skip_and_tamper_detection(self):
        with LocalRangeServer() as server:
            path = self.download(server)
            count = len(server.calls)
            self.assertEqual(self.download(server), path)
            self.assertEqual(len(server.calls), count)
            modified = b'X' * len(VIDEO)
            path.write_bytes(modified)
            recovered = self.download(server)
            self.assertNotEqual(recovered, path)
            self.assert_complete(recovered, VIDEO)
            self.assertEqual(path.read_bytes(), modified)

    def test_first_byte_arrives_before_remaining_body_and_metrics_hide_signed_url(self):
        with LocalRangeServer('first-byte', VIDEO[:DOWNLOAD_CHUNK_SIZE]) as server:
            def record_log(message, level):
                self.logs.append(message)
                if '下载首字节' in message:
                    server.release.set()
            self.client.log = record_log
            self.assert_complete(self.download(server), server.data)
            self.assertTrue(server.early_release)
        self.assertEqual(sum('下载首字节' in message for message in self.logs), 1)
        self.assertTrue(any('总耗时' in message and '吞吐量' in message and 'MB/s' in message for message in self.logs))
        self.assertTrue(any('Range 探测' in message for message in self.logs))
        self.assertNotIn('local-signed-secret', '\n'.join(self.logs))

    def test_connection_error_logs_redact_signed_query(self):
        session = requests.Session()
        self.addCleanup(session.close)
        self.client.session.close()
        self.client.session = session
        with patch.object(session, 'request', side_effect=requests.ConnectionError('failed https://local.invalid/video?signature=local-signed-secret')):
            with self.assertRaises(Exception):
                self.client.download_video('https://local.invalid/video?signature=local-signed-secret', self.folder, 'error.mp4')
        self.assertNotIn('local-signed-secret', '\n'.join(self.logs))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_fake_response_keeps_iter_content_contract_and_skips_extra_requests(self):
        response = FakeResponse(VIDEO, headers={'Accept-Ranges': 'bytes', 'ETag': '"version-one"'})
        session = FakeSession([response])
        client = VideoDownloader(session=session, log=lambda *args: None)
        self.addCleanup(client.close)
        path = Path(client.download_video('https://unused.invalid/video', self.folder, 'fake.mp4'))
        self.assert_complete(path, VIDEO)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(response.chunk_sizes, [DOWNLOAD_CHUNK_SIZE])
        self.assertTrue(response.closed)

    def test_fake_truncated_response_can_resume(self):
        data = VIDEO[:300000]
        first = FakeResponse(data[:65536], headers={'Content-Length': str(len(data))})
        second = FakeResponse(data[65536:], status=206, headers={'Content-Range': f'bytes 65536-{len(data) - 1}/{len(data)}'})
        session = FakeSession([first, second])
        client = VideoDownloader(session=session, log=lambda *args: None)
        self.addCleanup(client.close)
        path = Path(client.download_video('https://unused.invalid/video', self.folder, 'fake-resume.mp4'))
        self.assert_complete(path, data)
        self.assertEqual(session.calls[-1][1]['headers']['Range'], 'bytes=65536-')

    def test_compressed_single_stream_keeps_decoded_integrity(self):
        with LocalRangeServer('gzip') as server:
            self.assert_complete(self.download(server), VIDEO)
            self.assertEqual(len(server.calls), 1)

    def test_invalid_worker_counts_fail_before_opening_sessions(self):
        for workers in (0, 1, 9, True, 2.5, '4'):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                VideoDownloader(parallel_workers=workers)


if __name__ == '__main__':
    unittest.main()
