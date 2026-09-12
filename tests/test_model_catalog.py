"""Model discovery uses local HTTP and isolated temporary caches only."""
import importlib
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import requests

from core.api_client import ApiClient
from core.http_client import HttpClient, RequestError


class CatalogHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.calls.append((self.path, self.headers.get('Authorization')))
        status, payload = self.server.routes.get(self.path, (404, {'error': 'missing'}))
        if callable(payload):
            payload = payload()
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CatalogServer:
    def __enter__(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), CatalogHandler)
        self.server.routes = {'/models': (200, {'data': []})}
        self.server.calls = []
        self.server.base = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class FetchModelsTests(unittest.TestCase):
    def client(self, server):
        client = ApiClient(server.base, 'fixture-key', log=lambda *_: None)
        self.addCleanup(client.close)
        self.assertTrue(callable(getattr(client, 'fetch_models', None)), '模型列表发现接口尚未实现')
        return client

    def test_model_request_auth_and_unsupported_detail_stops_further_probes(self):
        with CatalogServer() as server:
            server.routes['/models'] = (200, {'data': [
                {'id': 'MiniMax-H3', 'object': 'model', 'created': 12, 'owned_by': 'fixture',
                 'supported_endpoint_types': ['openai-video'], 'api_key': 'must-not-return'},
                {'id': 'video-v2', 'supported_endpoint_types': ['openai-video']},
            ]})
            server.routes['/models/MiniMax-H3'] = (200, {'error': {'code': 'model_not_found'}})
            records = self.client(server).fetch_models()
            self.assertEqual([record['id'] for record in records], ['MiniMax-H3', 'video-v2'])
            self.assertNotIn('api_key', records[0])
            self.assertEqual(server.calls, [('/models', 'Bearer fixture-key'),
                                           ('/models/MiniMax-H3', 'Bearer fixture-key')])

    def test_detail_capabilities_are_merged_without_renaming_the_wire_id(self):
        with CatalogServer() as server:
            server.routes['/models'] = (200, {'data': [{'id': 'video-v2(渠道甲)'}]})
            server.routes['/models/video-v2%28%E6%B8%A0%E9%81%93%E7%94%B2%29'] = (200, {'data': {
                'id': 'wrong-detail-id', 'name': '快速视频', 'description': '720p，5秒，$0.20/次',
                'capabilities': {'resolutions': ['720p'], 'durations': [5], 'audio': False}}})
            record = self.client(server).fetch_models()[0]
            self.assertEqual(record['id'], 'video-v2(渠道甲)')
            self.assertEqual(record['capabilities']['durations'], [5])

    def test_http_200_error_and_malformed_lists_are_failures_but_empty_is_success(self):
        with CatalogServer() as server:
            client = self.client(server)
            for payload in ({'error': {'message': 'fixture-key rejected'}}, {'message': 'hello'},
                            {'data': {}}, {'data': [None]}, {'data': [{'id': ''}]}, []):
                with self.subTest(payload=payload):
                    server.routes['/models'] = (200, payload)
                    with self.assertRaises(RequestError) as caught:
                        client.fetch_models()
                    self.assertNotIn('fixture-key', str(caught.exception))
            server.routes['/models'] = (200, {'data': []})
            self.assertEqual(client.fetch_models(), [])

    def test_discovery_timeout_overrides_the_generation_timeout(self):
        with CatalogServer() as server:
            client = self.client(server)
            original = client.session.request
            observed = []
            def capture(*args, **kwargs):
                observed.append(kwargs['timeout'])
                return original(*args, **kwargs)
            with patch.object(client.session, 'request', side_effect=capture):
                client.fetch_models()
            self.assertEqual(observed, [10])

    def test_detail_request_count_is_bounded_when_every_detail_is_supported(self):
        with CatalogServer() as server:
            server.routes['/models'] = (200, {'data': [{'id': f'new-video-{n}'} for n in range(100)]})
            for n in range(100):
                server.routes[f'/models/new-video-{n}'] = (200, {'id': f'new-video-{n}', 'description': '720p'})
            self.assertEqual(len(self.client(server).fetch_models()), 100)
            self.assertLessEqual(len(server.calls), 9)


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('core.model_catalog'), '模型目录模块尚未实现')
        self.module = importlib.import_module('core.model_catalog')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / 'config.json'
        self.logs = []

    def catalog(self, base='http://127.0.0.1:9', key='fixture-key'):
        return self.module.ModelCatalog(self.config_path, base, key, log=lambda *args: self.logs.append(args))

    def refresh(self, catalog, records):
        with CatalogServer() as server:
            server.routes['/models'] = (200, {'data': records})
            # Match cache ownership to the local fixture instead of contacting a provider.
            catalog = self.catalog(server.base)
            client = ApiClient(server.base, 'fixture-key', log=lambda *_: None)
            try:
                snapshot = catalog.refresh(client)
            finally:
                client.close()
            return catalog, snapshot

    def test_builtin_limits_and_variant_families_are_conservative(self):
        options = self.module.options_for
        self.assertEqual(options('MiniMax-H3')['resolutions'], ('1080p', '2K', '4K'))
        self.assertEqual(options('video-v2-fast(渠道甲)')['resolutions'], ('720p',))
        self.assertEqual(options('video-v3-480p-特价')['resolutions'], ('480p',))
        self.assertEqual(options('video-v3')['durations'], tuple(range(4, 31)))
        self.assertEqual(self.module.family_for('video-v2-特价'), 'video-v2')
        self.assertEqual(self.module.family_for('seedance2.5'), 'video-v3')
        self.assertEqual(self.module.family_for('video-v20'), '')
        self.assertFalse(options('wan-3.0')['protocol_known'])
        self.assertEqual(options('wan-3.0')['resolutions'], ('720p',))

    def test_no_key_uses_builtin_and_never_contacts_the_client(self):
        catalog = self.catalog(key='')
        class ForbiddenClient:
            def fetch_models(self):
                raise AssertionError('无 key 不应请求上游')
        snapshot = catalog.refresh(ForbiddenClient())
        self.assertIn('MiniMax-H3', snapshot)
        self.assertEqual(catalog.source, 'builtin')
        self.assertFalse((self.config_path.parent / 'models_cache.json').exists())

    def test_fullwidth_channel_names_use_their_base_protocol(self):
        for model, family in [('MiniMax-H3（限时）', 'MiniMax-H3'),
                              ('video-v1（渠道）', 'video-v1'),
                              ('video-v2（特价）', 'video-v2'),
                              ('video-v3（限时）', 'video-v3')]:
            with self.subTest(model=model):
                self.assertEqual(self.module.family_for(model), family)
                self.assertTrue(self.module.options_for(model)['protocol_known'])

    def test_all_21_upstream_models_survive_with_image_and_video_classification(self):
        records = [{'id': f'video-{n}', 'object': 'model', 'created': 1, 'owned_by': 'fixture',
                    'supported_endpoint_types': ['openai-video']} for n in range(11)]
        records += [{'id': f'image-{n}', 'object': 'model', 'created': 1, 'owned_by': 'fixture',
                     'supported_endpoint_types': ['gemini' if n % 2 else 'openai']} for n in range(10)]
        catalog, snapshot = self.refresh(self.catalog(), records)
        self.assertEqual(len(snapshot), 21)
        self.assertEqual(sum(row['kind'] == 'video' for row in snapshot.values()), 11)
        self.assertEqual(sum(row['kind'] == 'image' for row in snapshot.values()), 10)
        self.assertTrue(all(row['pricing_text'] == '计费未提供' for row in snapshot.values()))
        self.assertTrue(all(not row['protocol_known'] for row in snapshot.values()))
        self.assertEqual(catalog.source, 'upstream')
        self.assertFalse(catalog.expired)

    def test_explicit_capabilities_win_over_description_and_family_defaults(self):
        catalog, snapshot = self.refresh(self.catalog(), [{
            'id': 'video-v2-fast(渠道甲)', 'name': '渠道快版', 'description': '1080p，16:9，10秒，每次 ¥0.60',
            'capabilities': {'resolutions': ['720p'], 'ratios': ['9:16'], 'durations': [5],
                             'audio': False, 'seed': True, 'max_images': 2},
            'aliases': ['retired-channel'], 'pricing_text': '$0.25/次', 'token': 'drop-me'}])
        record = snapshot['video-v2-fast(渠道甲)']
        self.assertEqual(record['name'], record['id'])
        self.assertEqual(record['display_name'], '渠道快版')
        self.assertEqual(record['family'], 'video-v2')
        self.assertEqual(record['resolutions'], ['720p'])
        self.assertEqual(record['ratios'], ['9:16'])
        self.assertEqual(record['durations'], [5])
        self.assertFalse(record['audio'])
        self.assertTrue(record['seed'])
        self.assertEqual(record['max_images'], 2)
        self.assertEqual(record['pricing_text'], '$0.25/次')
        self.assertEqual(record['aliases'], ['retired-channel'])
        self.assertEqual(record['capability_source'], 'upstream')
        self.assertNotIn('token', record)
        snapshot[record['id']]['ratios'].append('7:1')
        self.assertEqual(catalog.snapshot()[record['id']]['ratios'], ['9:16'])

    def test_top_level_capabilities_are_consumed_and_nested_values_take_priority(self):
        _, snapshot = self.refresh(self.catalog(), [{'id': 'video-v3', 'resolutions': ['480p'],
            'ratios': ['1:1'], 'durations': [4, 6], 'audio': False,
            'capabilities': {'resolutions': ['720p']}}])
        self.assertEqual(snapshot['video-v3']['resolutions'], ['720p'])
        self.assertEqual(snapshot['video-v3']['ratios'], ['1:1'])
        self.assertEqual(snapshot['video-v3']['durations'], [4, 6])
        self.assertFalse(snapshot['video-v3']['audio'])

    def test_description_extracts_capabilities_and_price_without_guessing_protocol(self):
        _, snapshot = self.refresh(self.catalog(), [{'id': 'future-video', 'kind': 'video',
            'description': '分辨率 720p / 1080p；比例 16:9、9:16；时长 4-8 秒；计费 $0.30/秒'}])
        row = snapshot['future-video']
        self.assertEqual(row['resolutions'], ['720p', '1080p'])
        self.assertEqual(row['ratios'], ['16:9', '9:16'])
        self.assertEqual(row['durations'], [4, 5, 6, 7, 8])
        self.assertIn('$0.30/秒', row['pricing_text'])
        self.assertFalse(row['protocol_known'])
        self.assertEqual(row['capability_source'], 'description')

    def test_dates_and_price_ranges_are_not_video_durations(self):
        _, snapshot = self.refresh(self.catalog(), [{'id': 'video-v2',
            'description': 'Released:2026-09-11; supports 720p; pricing $5-8/video'}])
        self.assertEqual(snapshot['video-v2']['durations'], [5, 10, 15])

    def test_cache_roundtrip_isolated_by_key_and_base_and_never_saves_credentials(self):
        catalog, snapshot = self.refresh(self.catalog(), [{'id': 'video-v2', 'description': 'fixture-key is private'}])
        cache = self.config_path.parent / 'models_cache.json'
        document = json.loads(cache.read_text(encoding='utf-8'))
        self.assertNotIn('fixture-key', cache.read_text(encoding='utf-8'))
        self.assertIsInstance(document['fetched_at'], (int, float))
        self.assertEqual(document['source'], 'upstream')
        reopened = self.catalog(catalog.base_url)
        self.assertEqual(reopened.source, 'cache')
        self.assertEqual(reopened.snapshot(), snapshot)
        self.assertEqual(self.catalog(catalog.base_url, 'another-key').source, 'builtin')
        self.assertEqual(self.catalog(catalog.base_url + '/another').source, 'builtin')
        self.assertEqual(self.catalog(catalog.base_url, '').source, 'builtin')

    def test_offline_refresh_preserves_last_good_snapshot_and_reports_failure(self):
        catalog, before = self.refresh(self.catalog(), [{'id': 'video-v2'}])
        cache = self.config_path.parent / 'models_cache.json'
        previous = cache.read_bytes()
        class OfflineClient:
            def fetch_models(self):
                raise RequestError('offline fixture-key')
        after = catalog.refresh(OfflineClient())
        self.assertEqual(after, before)
        self.assertEqual(cache.read_bytes(), previous)
        self.assertTrue(catalog.last_error)
        self.assertNotIn('fixture-key', catalog.last_error)
        self.assertTrue(any('使用离线模型缓存' in message for message, _ in self.logs))

    def test_successful_empty_list_removes_all_models_and_survives_reload(self):
        with CatalogServer() as server:
            client = ApiClient(server.base, 'fixture-key', log=lambda *_: None)
            self.addCleanup(client.close)
            catalog = self.catalog(server.base)
            self.assertEqual(catalog.refresh(client), {})
            self.assertEqual(catalog.source, 'upstream')
            self.assertEqual(self.catalog(server.base).snapshot(), {})
            self.assertEqual(self.catalog(server.base).source, 'cache')

    def test_atomic_replace_failure_retains_old_disk_and_memory_state(self):
        catalog, before = self.refresh(self.catalog(), [{'id': 'video-v2'}])
        cache = self.config_path.parent / 'models_cache.json'
        previous = cache.read_bytes()
        class NewClient:
            def fetch_models(self):
                return [{'id': 'video-v3'}]
        with patch('core.model_catalog.os.replace', side_effect=OSError('fixture disk locked')):
            self.assertEqual(catalog.refresh(NewClient()), before)
        self.assertEqual(cache.read_bytes(), previous)
        self.assertTrue(catalog.last_error)
        self.assertEqual(list(self.config_path.parent.glob('*.tmp')), [])

    def test_corrupt_and_expired_cache_handling(self):
        cache = self.config_path.parent / 'models_cache.json'
        cache.write_text('{invalid', encoding='utf-8')
        self.assertEqual(self.catalog().source, 'builtin')
        catalog, _ = self.refresh(self.catalog(), [{'id': 'video-v2'}])
        document = json.loads(cache.read_text(encoding='utf-8'))
        document['fetched_at'] = time.time() - 25 * 3600
        cache.write_text(json.dumps(document), encoding='utf-8')
        reopened = self.catalog(catalog.base_url)
        self.assertEqual(reopened.source, 'cache')
        self.assertTrue(reopened.expired)
        document['models']['video-v2']['durations'] = ['bad']
        cache.write_text(json.dumps(document), encoding='utf-8')
        self.assertEqual(self.catalog(catalog.base_url).source, 'builtin')


if __name__ == '__main__':
    unittest.main()
