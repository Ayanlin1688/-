"""Model discovery uses local HTTP and isolated temporary caches only."""
import importlib
import importlib.util
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests

from core.api_client import ApiClient
from core.http_client import HttpClient, RequestError
from core import model_catalog


# Fixed offline examples from the documented families, including billing variants.
_CLASSIFICATION_MODELS = {
    'video': [
        'MiniMax-H3', 'video-v2', 'video-v2-fast', 'video-v2-特价版',
        'video-v2-480P', 'video-v2-720P', 'video-v3', 'video-v3-480p',
        'video-v3-480P（按条计费）', 'video-v3-720P（按条计费）',
        'video-v3-特价版', 'wan-3.0',
    ],
    'image': [
        'gpt-image-2', 'gpt-image-2-cf', 'gpt-image-2.5-flare',
        'gpt-image-2.5-flare-cf', 'gpt-image-2.5-sunburst',
        'gpt-image-2.5-sunburst-cf', 'Nano Banana 2', 'Nano Banana 2.1',
        'Nano Banana Pro', 'grok-imagine-image', 'grok-imagine-image-edit',
    ],
    'chat': [
        'claude-haiku-4-5', 'claude-sonnet-4-5', 'claude-sonnet-4-6',
        'claude-sonnet-4-6-thinking', 'claude-sonnet-5', 'claude-sonnet-5.5',
        'claude-opus-4.5', 'claude-opus-4.6', 'claude-opus-4.7',
        'claude-opus-5', 'claude-opus-5.5', 'claude-fable-5',
        'gpt-5.6-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6.1-sol',
        'codex-auto-review', 'gemini-3-flash-preview', 'gemini-3.1-pro-high',
        'gemini-3.1-pro-preview', 'gemini-3.5-flash-lite',
        'gemini-3.6-flash-high', 'gemini-3.7-flash-high', 'gemini-3.8-flash',
        'gemini-4-argon', 'grok-4.5', 'grok-4.6', 'grok-4.7',
        'claude-haiku-4-5-pack', 'claude-sonnet-4-5-pack',
        'claude-sonnet-4-6-pack', 'claude-sonnet-4-6-thinking-pack',
        'claude-sonnet-5-pack', 'claude-sonnet-5.5-pack',
        'claude-opus-4.5-pack', 'claude-opus-4.6-pack', 'claude-opus-4.7-pack',
        'claude-opus-5-pack', 'claude-opus-5.5-pack', 'claude-fable-5-pack',
        'gemini-3-flash-preview-pack', 'gemini-3.1-pro-high-pack',
        'gemini-3.1-pro-preview-pack', 'gemini-3.5-flash-lite-pack',
        'gemini-3.6-flash-high-pack', 'gemini-3.7-flash-high-pack',
        'gemini-3.8-flash-pack',
    ],
}


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


class ModelClassificationTests(unittest.TestCase):
    def test_endpoint_metadata_is_preserved_without_changing_family_rules(self):
        for endpoints in (['openai-video'], ['video'], ['videos'], ['video-generation']):
            row = model_catalog._record_from_upstream({'id': 'future-motion', 'supported_endpoint_types': endpoints})
            self.assertEqual(row['supported_endpoint_types'], endpoints)
            self.assertEqual(row['kind'], 'video')
            self.assertEqual(row['family'], '')
            self.assertFalse(row['protocol_known'])
            self.assertTrue(model_catalog._valid_record(row))
        row = model_catalog._record_from_upstream({'id': 'future-motion', 'endpoint_types': ['openai-video']})
        self.assertEqual(row['supported_endpoint_types'], ['openai-video'])
        self.assertEqual(model_catalog._record_from_upstream({'id': 'future-motion'})['kind'], 'unknown')
        self.assertEqual(model_catalog._record_from_upstream({'id': 'future-motion'})['supported_endpoint_types'], [])
        self.assertFalse(model_catalog._valid_record(dict(row, supported_endpoint_types=[42])))

    def test_explicit_unknown_kind_still_uses_video_endpoint_metadata(self):
        row = model_catalog._record_from_upstream({
            'id': 'future-motion', 'kind': 'unknown',
            'supported_endpoint_types': ['openai-video'],
        })
        self.assertEqual(row['kind'], 'video')
        self.assertFalse(row['protocol_known'])
        self.assertEqual(row['supported_endpoint_types'], ['openai-video'])

    def test_70_offline_models_have_expected_kind_and_pack_availability(self):
        cases = [(model, kind) for kind, models in _CLASSIFICATION_MODELS.items() for model in models]
        self.assertEqual(len(cases), 70)
        self.assertEqual(len({model for model, _ in cases}), 70)
        for model, kind in cases:
            with self.subTest(model=model):
                self.assertEqual(model_catalog.classify_model(model), kind)
                record = model_catalog._record_from_upstream({'id': model})
                self.assertEqual(record['kind'], kind)
                self.assertEqual(record['available'], not model.endswith('-pack'))
                self.assertTrue(model_catalog._valid_record(record))

    def test_regex_fallbacks_cover_chat_image_and_video_families(self):
        cases = {
            'chat': ['deepseek-v4-pro', 'claude-sonnet-5', 'qwen-max', 'grok-4.9',
                     'GPT-OSS-120B', 'glm-5', 'kimi-k2', 'moonshot-v1', 'llama-4',
                     'codex-latest', 'o1', 'o3-mini', 'o9-preview'],
            'image': ['DALL-E-3', 'dalle-3', 'FLUX-dev', 'midjourney-v7',
                      'stable-diffusion-xl', 'seedream-4', 'jimeng-v4',
                      'ideogram-v3', 'qwen-image-edit', 'wanx-2.1', 'cogview-4',
                      'imagen-4', 'NANO BANANA PRO'],
            'video': ['video-v1', 'video-v2（按秒计费）', 'MiniMax-H3（特价版）',
                      'grok-imagine-1.5-video', 'seedance-2.5', 'sd2.5'],
            'unknown': ['some-random-video-xyz', 'video-v20', 'fluxology',
                        'notimagen', 'random-qwen-imageextra', ''],
        }
        for kind, models in cases.items():
            for model in models:
                with self.subTest(model=model):
                    self.assertEqual(model_catalog.classify_model(model), kind)

    def test_id_rules_precede_endpoint_fallbacks_and_endpoint_order_is_stable(self):
        cases = [
            ('video-v3', ['chat'], 'video'),
            ('gpt-image-2', ['chat', 'openai-video'], 'image'),
            ('gemini-3.8-flash', ['images'], 'chat'),
            ('unknown-model', ['chat/completions', 'images', 'video'], 'chat'),
            ('unknown-model', ['images', 'video'], 'image'),
            ('unknown-model', ['openai-video'], 'video'),
            ('unknown-model', 'CHAT', 'chat'),
            ('unknown-model', ['llm'], 'chat'),
            ('unknown-model', ['image'], 'image'),
            ('unknown-model', ['unsupported'], 'unknown'),
        ]
        for model, endpoints, kind in cases:
            with self.subTest(model=model, endpoints=endpoints):
                self.assertEqual(model_catalog.classify_model(model, endpoints), kind)

    def test_chat_and_image_records_ignore_video_capabilities_and_validate(self):
        for row in ({'id': 'provider-text', 'supported_endpoint_types': ['chat']},
                    {'id': 'gpt-image-2'},
                    {'id': 'gemini-3.8-flash'}):
            with self.subTest(row=row):
                record = model_catalog._record_from_upstream(dict(row,
                    description='1080p, 16:9, 5 seconds',
                    capabilities={'resolutions': ['4K'], 'ratios': ['9:16'],
                                  'durations': [10], 'audio': True, 'seed': True, 'max_images': 9}))
                self.assertTrue(model_catalog._valid_record(record))
                self.assertTrue(record['protocol_known'])
                for field in ('resolutions', 'ratios', 'durations'):
                    self.assertEqual(record[field], [])
                self.assertFalse(record['audio'])
                self.assertFalse(record['seed'])
                self.assertEqual(record['max_images'], 0)

    def test_explicit_chat_aliases_are_normalized_and_other_explicit_kinds_survive(self):
        for field in ('kind', 'type'):
            for explicit in ('chat', 'llm', 'text', ' TEXT '):
                with self.subTest(field=field, explicit=explicit):
                    record = model_catalog._record_from_upstream({'id': 'custom-model', field: explicit})
                    self.assertEqual(record['kind'], 'chat')
                    self.assertTrue(model_catalog._valid_record(record))
        for kind in ('video', 'image', 'unknown'):
            with self.subTest(kind=kind):
                self.assertEqual(model_catalog._record_from_upstream(
                    {'id': 'custom-model', 'kind': kind})['kind'], kind)

    def test_pack_models_keep_classification_and_video_protocol_but_are_unavailable(self):
        for model in ('video-v3', 'seedance2.5', 'wan-3.0', 'grok-imagine-1.5-video',
                      'gpt-image-2', 'Nano Banana Pro', 'gemini-3.8-flash', 'deepseek-chat'):
            with self.subTest(model=model):
                base = model_catalog._record_from_upstream({'id': model})
                pack = model_catalog._record_from_upstream({'id': model + '-PACK'})
                self.assertEqual(pack['kind'], base['kind'])
                self.assertFalse(pack['available'])
                for field in ('family', 'resolutions', 'ratios', 'durations', 'protocol_known'):
                    self.assertEqual(pack[field], base[field])

    def test_builtins_list_only_verified_video_records_and_add_complete_non_video_records(self):
        records = model_catalog.builtin_models()
        video_ids = ['video-v1', 'video-v2', 'video-v2-fast', 'video-v3',
                     'MiniMax-H3', 'grok-imagine-1.5-video']
        self.assertEqual([model for model, row in records.items() if row['kind'] == 'video'], video_ids)
        self.assertFalse(any('seedance' in model.lower() for model in records))
        self.assertEqual(model_catalog.classify_model('seedance-2.5'), 'video')
        self.assertEqual(model_catalog.family_for('seedance-2.5'), 'video-v3')
        for model in video_ids:
            with self.subTest(model=model):
                row = records[model]
                self.assertEqual(row['family'], model_catalog._base_family(model))
                for field, value in model_catalog._defaults(row['family'], model).items():
                    self.assertEqual(row[field], value)
                self.assertTrue(model_catalog._valid_record(row))
        for model, kind, family in [
                ('deepseek-flash', 'chat', 'deepseek'), ('deepseek-v4-pro', 'chat', 'deepseek'),
                ('deepseek-chat', 'chat', 'deepseek'), ('deepseek-reasoner', 'chat', 'deepseek'),
                ('gemini-3.8-flash', 'chat', 'gemini'),
                ('gpt-image-2.5-sunburst', 'image', 'gpt-image-sunburst'),
                ('gpt-image-2', 'image', 'gpt-image')]:
            with self.subTest(model=model):
                row = records[model]
                self.assertEqual((row['kind'], row['family']), (kind, family))
                self.assertEqual(set(row), set(model_catalog._FIELDS))
                self.assertTrue(model_catalog._valid_record(row))
                self.assertTrue(row['protocol_known'])
                self.assertTrue(row['available'])
                for field in ('resolutions', 'ratios', 'durations'):
                    self.assertEqual(row[field], ())
                self.assertEqual((row['audio'], row['seed'], row['max_images']), (False, False, 0))


class ModelCatalogTests(unittest.TestCase):
    def test_endpoint_metadata_roundtrip_and_old_cache_without_field(self):
        catalog = self.catalog()
        client = Mock()
        client.fetch_models.return_value = [{'id': 'future-motion', 'supported_endpoint_types': ['openai-video']}]
        snapshot = catalog.refresh(client)
        self.assertEqual(self.catalog().snapshot(), snapshot)
        document = json.loads(catalog.cache_path.read_text(encoding='utf-8'))
        document['models']['future-motion'].pop('supported_endpoint_types')
        catalog.cache_path.write_text(json.dumps(document), encoding='utf-8')
        reopened = self.catalog()
        self.assertEqual(reopened.source, 'cache')
        self.assertEqual(reopened.snapshot()['future-motion']['kind'], 'video')
        self.assertEqual(reopened.snapshot()['future-motion'].get('supported_endpoint_types', []), [])

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('core.model_catalog'), '模型目录模块尚未实现')
        self.module = importlib.import_module('core.model_catalog')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / 'config.json'
        self.logs = []

    def test_normalize_model_id_strips_display_labels_without_damaging_custom_ids(self):
        cases = (
            ('deepseek-v4-pro · DeepSeek-V4-Pro', None, 'deepseek-v4-pro'),
            ('Nano Banana 2.1 · Nano Banana 2.1', None, 'Nano Banana 2.1'),
            ('gemini-4-argon · Gemini 4 Argon', None, 'gemini-4-argon'),
            ('gpt-5.6-sol', None, 'gpt-5.6-sol'),
            ('tenant model · Friendly name', None, 'tenant model · Friendly name'),
            ('tenant model · Friendly name', {'tenant model'}, 'tenant model'),
            ('display · wire', {'display · wire'}, 'display · wire'),
        )
        for value, known_ids, expected in cases:
            with self.subTest(value=value, known_ids=known_ids):
                self.assertEqual(self.module.normalize_model_id(value, known_ids), expected)

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
        self.assertEqual(options('MiniMax-H3')['resolutions'], ('768p', '1080p', '2K', '4K'))
        self.assertEqual(options('video-v2-fast(渠道甲)')['resolutions'], ('720p',))
        self.assertEqual(options('video-v3-480p-特价')['resolutions'], ('480p',))
        self.assertEqual(options('video-v3')['durations'], tuple(range(4, 31)))
        self.assertEqual(self.module.family_for('video-v2-特价'), 'video-v2')
        self.assertEqual(self.module.family_for('seedance2.5'), 'video-v3')
        self.assertEqual(self.module.family_for('video-v20'), '')
        self.assertFalse(options('wan-3.0')['protocol_known'])
        self.assertEqual(options('wan-3.0')['resolutions'], ())
        self.assertNotIn('wan-3.0', self.module.builtin_models())

    def test_no_key_uses_builtin_and_never_contacts_the_client(self):
        catalog = self.catalog(key='')
        class ForbiddenClient:
            def fetch_models(self):
                raise AssertionError('无 key 不应请求上游')
        snapshot = catalog.refresh(ForbiddenClient())
        self.assertIn('MiniMax-H3', snapshot)
        self.assertIn('deepseek-chat', snapshot)
        self.assertIn('deepseek-reasoner', snapshot)
        self.assertEqual(catalog.source, 'builtin')
        self.assertEqual(list(self.config_path.parent.glob('models_cache*.json')), [])

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
        self.assertTrue(all(not row['protocol_known'] for row in snapshot.values() if row['kind'] == 'video'))
        self.assertTrue(all(row['protocol_known'] for row in snapshot.values() if row['kind'] == 'image'))
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
        cache = catalog.cache_path
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
        cache = catalog.cache_path
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
        cache = catalog.cache_path
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
        document = json.loads(catalog.cache_path.read_text(encoding='utf-8'))
        document['fetched_at'] = time.time() - 25 * 3600
        catalog.cache_path.write_text(json.dumps(document), encoding='utf-8')
        reopened = self.catalog(catalog.base_url)
        self.assertEqual(reopened.source, 'cache')
        self.assertTrue(reopened.expired)
        document['models']['video-v2']['durations'] = ['bad']
        catalog.cache_path.write_text(json.dumps(document), encoding='utf-8')
        self.assertEqual(self.catalog(catalog.base_url).source, 'builtin')

    def test_cache_paths_hash_each_normalized_identity_and_are_dynamic(self):
        catalog = self.catalog(base=' https://api.kkone.vip/v1/ ', key=' fixture-key ')
        self.assertEqual(catalog.base_url, 'https://api.kkone.vip/v1')
        base_hash = hashlib.sha256(catalog.base_url.encode('utf-8')).hexdigest()[:8]
        key_hash = hashlib.sha256(b'fixture-key').hexdigest()[:8]
        self.assertEqual(catalog.cache_path.name, f'models_cache_{base_hash}{key_hash}.json')
        first = catalog.cache_path
        catalog.api_key = 'other-fixture-key'
        second = catalog.cache_path
        catalog.base_url += '/another'
        third = catalog.cache_path
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(first.parent, self.config_path.parent)
        self.assertNotIn('fixture-key', first.name)

    def test_two_lines_and_two_keys_keep_independent_disk_snapshots(self):
        identities = [
            ('http://127.0.0.1:9/v1', 'fixture-key', 'video-v2'),
            ('http://127.0.0.1:10/v1', 'fixture-key', 'gemini-3.8-flash'),
            ('http://127.0.0.1:9/v1', 'another-fixture-key', 'gpt-image-2'),
        ]
        paths = []
        for base, key, model in identities:
            catalog = self.catalog(base, key)
            client = Mock()
            client.fetch_models.return_value = [{'id': model}]
            self.assertEqual(set(catalog.refresh(client)), {model})
            paths.append(catalog.cache_path)
        self.assertEqual(len(set(paths)), 3)
        self.assertFalse((self.config_path.parent / 'models_cache.json').exists())
        for base, key, model in identities:
            with self.subTest(base=base, model=model):
                reopened = self.catalog(base, key)
                self.assertEqual(reopened.source, 'cache')
                self.assertEqual(set(reopened.snapshot()), {model})

    def test_matching_legacy_cache_is_migrated_once_and_left_byte_identical(self):
        catalog = self.catalog()
        client = Mock()
        client.fetch_models.return_value = [{'id': 'video-v2'}]
        snapshot = catalog.refresh(client)
        document = json.loads(catalog.cache_path.read_text(encoding='utf-8'))
        catalog.cache_path.unlink()
        legacy = self.config_path.parent / 'models_cache.json'
        legacy.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        previous = legacy.read_bytes()
        migrated = self.catalog()
        self.assertEqual(migrated.source, 'cache')
        self.assertEqual(migrated.snapshot(), snapshot)
        self.assertEqual(json.loads(migrated.cache_path.read_text(encoding='utf-8')), document)
        self.assertEqual(legacy.read_bytes(), previous)
        legacy.write_text('{invalid legacy after migration', encoding='utf-8')
        with patch.object(self.module.ModelCatalog, '_atomic_write') as write:
            self.assertEqual(self.catalog().snapshot(), snapshot)
        write.assert_not_called()

    def test_wrong_identity_or_invalid_legacy_cache_is_never_migrated(self):
        catalog = self.catalog()
        client = Mock()
        client.fetch_models.return_value = [{'id': 'video-v2'}]
        catalog.refresh(client)
        document = json.loads(catalog.cache_path.read_text(encoding='utf-8'))
        catalog.cache_path.unlink()
        legacy = self.config_path.parent / 'models_cache.json'
        variants = [
            dict(document, base_url=document['base_url'] + '/another'),
            dict(document, key_fingerprint=hashlib.sha256(b'another-fixture-key').hexdigest()),
            dict(document, source='builtin'), dict(document, unexpected=True),
            dict(document, models={'bad': {'id': 'bad'}}),
        ]
        for invalid in variants:
            with self.subTest(fields=set(invalid)):
                legacy.write_text(json.dumps(invalid), encoding='utf-8')
                previous = legacy.read_bytes()
                reopened = self.catalog()
                self.assertEqual(reopened.source, 'builtin')
                self.assertFalse(reopened.cache_path.exists())
                self.assertEqual(legacy.read_bytes(), previous)

    def test_migration_write_failure_keeps_readable_legacy_snapshot(self):
        catalog = self.catalog()
        client = Mock()
        client.fetch_models.return_value = [{'id': 'video-v2'}]
        snapshot = catalog.refresh(client)
        previous = catalog.cache_path.read_bytes()
        catalog.cache_path.unlink()
        legacy = self.config_path.parent / 'models_cache.json'
        legacy.write_bytes(previous)
        with patch.object(self.module.ModelCatalog, '_atomic_write', side_effect=OSError('read-only fixture')):
            reopened = self.catalog()
        self.assertEqual(reopened.source, 'cache')
        self.assertEqual(reopened.snapshot(), snapshot)
        self.assertFalse(reopened.cache_path.exists())
        self.assertEqual(legacy.read_bytes(), previous)

    def test_identity_cache_takes_precedence_without_modifying_legacy(self):
        catalog = self.catalog()
        client = Mock()
        client.fetch_models.return_value = [{'id': 'video-v2'}]
        catalog.refresh(client)
        legacy = self.config_path.parent / 'models_cache.json'
        legacy.write_bytes(catalog.cache_path.read_bytes())
        previous = legacy.read_bytes()
        client.fetch_models.return_value = [{'id': 'video-v3'}]
        catalog.refresh(client)
        self.assertEqual(legacy.read_bytes(), previous)
        self.assertEqual(set(self.catalog().snapshot()), {'video-v3'})
        catalog.cache_path.write_text('{invalid current cache', encoding='utf-8')
        self.assertEqual(self.catalog().source, 'builtin')
        self.assertEqual(legacy.read_bytes(), previous)

    def test_pack_records_survive_refresh_save_and_reload_but_stay_unavailable(self):
        client = Mock()
        client.fetch_models.return_value = [{'id': 'gemini-3.8-flash'},
                                           {'id': 'gemini-3.8-flash-pack'}]
        catalog = self.catalog()
        snapshot = catalog.refresh(client, persist=False)
        self.assertEqual(len(snapshot), 2)
        self.assertFalse(catalog.cache_path.exists())
        catalog.save()
        reopened = self.catalog()
        self.assertEqual(reopened.snapshot(), snapshot)
        self.assertTrue(snapshot['gemini-3.8-flash']['available'])
        self.assertFalse(snapshot['gemini-3.8-flash-pack']['available'])

    def test_deepseek_host_merges_missing_models_without_overwriting_provider_metadata(self):
        catalog = self.catalog(base='https://api.deepseek.com/v1')
        client = Mock()
        client.fetch_models.return_value = [
            {'id': 'deepseek-flash', 'name': 'provider-flash', 'description': 'flash metadata'},
            {'id': 'deepseek-v4-pro', 'name': 'provider-v4', 'description': 'v4 metadata'},
            {'id': 'deepseek-chat', 'name': 'provider-chat', 'description': 'chat metadata'},
        ]
        for _ in range(2):
            snapshot = catalog.refresh(client)
            self.assertEqual(set(snapshot), {'deepseek-flash', 'deepseek-v4-pro', 'deepseek-chat', 'deepseek-reasoner'})
            self.assertEqual(snapshot['deepseek-flash']['display_name'], 'provider-flash')
            self.assertEqual(snapshot['deepseek-v4-pro']['display_name'], 'provider-v4')
            self.assertEqual(snapshot['deepseek-flash']['description'], 'flash metadata')
            self.assertEqual(snapshot['deepseek-v4-pro']['description'], 'v4 metadata')
            self.assertTrue(all(snapshot[name]['kind'] == 'chat' for name in (
                'deepseek-flash', 'deepseek-v4-pro', 'deepseek-chat', 'deepseek-reasoner')))
            self.assertTrue(all(snapshot[name]['family'] == 'deepseek' for name in (
                'deepseek-flash', 'deepseek-v4-pro', 'deepseek-chat', 'deepseek-reasoner')))
            self.assertTrue(all(self.module._valid_record(row) for row in snapshot.values()))
        self.assertEqual(self.catalog(catalog.base_url).snapshot(), snapshot)
        client.fetch_models.return_value = []
        self.assertEqual(set(catalog.refresh(client)), {'deepseek-flash', 'deepseek-v4-pro', 'deepseek-chat', 'deepseek-reasoner'})

    def test_deepseek_cache_with_missing_defaults_is_merged_on_load(self):
        catalog = self.catalog(base='https://api.deepseek.com/v1')
        client = Mock()
        client.fetch_models.return_value = []
        catalog.refresh(client)
        document = json.loads(catalog.cache_path.read_text(encoding='utf-8'))
        document['models'] = {}
        catalog.cache_path.write_text(json.dumps(document), encoding='utf-8')
        reopened = self.catalog(catalog.base_url)
        self.assertEqual(reopened.source, 'cache')
        self.assertEqual(set(reopened.snapshot()), {'deepseek-flash', 'deepseek-v4-pro', 'deepseek-chat', 'deepseek-reasoner'})


if __name__ == '__main__':
    unittest.main()
