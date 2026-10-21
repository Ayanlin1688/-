import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from core.config_manager import ConfigManager
from core.ecommerce.analysis import EcommerceAnalysisError
from core.ecommerce.image_client import EcommerceImageError, ImageResult
from core.ecommerce.manager import EcommerceImageManager
from core.ecommerce.persistence import EcommerceImageStore
from core.ecommerce.platforms import get_platform


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
TEST_KEY = 'offline-fixture-credential-never-send'


def fake_analysis(*args, **kwargs):
    d, g = args[5:7]
    return ([dict(type=kind, index=seq, prompt=f'Studio product {kind} {seq}', desc=f'商品展示{seq}')
             for kind, count in (('carousel', d), ('detail', g)) for seq in range(1, count + 1)], '离线思考')


def fake_image(*args, **kwargs):
    return ImageResult(PNG, args[2], '1024x1024', args[5], 'auto', '/images/generations',
                       image_tokens=8, warnings=['离线示例'])


class EcommerceManagerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = EcommerceImageStore(self.root / 'history.sqlite3')
        # Fail closed if any tested branch accidentally reaches a real client.
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('禁止真实联网'))
        network.start()
        self.addCleanup(network.stop)
        self.analysis = Mock(side_effect=fake_analysis)
        self.image = Mock(side_effect=fake_image)
        self.logs, self.groups, self.items = [], [], []
        self.manager = self.make_manager()

    def make_manager(self, **updates):
        options = dict(base_url='offline-fixture', api_key=TEST_KEY, output_root=self.root / 'output',
                       store=self.store, analysis_func=self.analysis, image_func=self.image,
                       on_log=lambda message, level: self.logs.append((message, level)),
                       on_group=self.groups.append, on_item=self.items.append)
        options.update(updates)
        return EcommerceImageManager(**options)

    def start(self, **updates):
        options = dict(platforms=['douyin'], product_name='茶杯', product_info='耐热陶瓷',
                       language='zh', carousel_count=2, detail_count=1,
                       carousel_ratio='1:1', detail_ratio='3:4', tier='1K')
        options.update(updates)
        return self.manager.start_group(**options)

    def build_pool_manager(self, *, analysis_model='chosen-analysis', image_model='chosen-image',
                           chat_pool=None, image_pool=None):
        config = ConfigManager(self.root / 'pool-config.json')
        config.config['api'].update(base_url='offline-fixture', api_key=TEST_KEY)
        config.config['paths']['output'] = str(self.root / 'pool-output')
        config.config['ecommerce'].update(analysis_model=analysis_model, image_model=image_model)
        config.config['chat_model_pool'] = chat_pool or {'enabled': False, 'models': []}
        config.config['image_model_pool'] = image_pool or {'enabled': False, 'models': []}
        manager = EcommerceImageManager.build_from_config(config, store=self.store)
        manager.analysis_func = self.analysis
        manager.image_func = self.image
        manager.on_log = lambda message, level: self.logs.append((message, level))
        return manager

    def manifest(self, group_id):
        group = self.store.get_group(group_id)
        data = json.loads((Path(group['output_dir']) / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(data, group['manifest'])
        self.assertEqual(data['status'], group['status'])
        self.assertEqual(data['totals'], group['totals'])
        return data

    def test_success_writes_all_files_and_matching_manifest(self):
        group_id = self.start()
        group = self.store.get_group(group_id)
        self.assertEqual(group['status'], 'succeeded')
        directory = Path(group['output_dir'])
        self.assertEqual({p.name for p in directory.iterdir()}, {'轮1.png', '轮2.png', '详1.png', 'manifest.json'})
        for record in self.store.list_items(group_id):
            self.assertEqual(record['status'], 'succeeded')
            self.assertTrue(Path(record['file_path']).is_absolute())
            self.assertEqual(Path(record['file_path']).read_bytes(), PNG)
            self.assertEqual(record['size'], '1024x1024')
            self.assertEqual(record['image_tokens'], 8)
        manifest = self.manifest(group_id)
        self.assertEqual(manifest['product_info'], '耐热陶瓷')
        self.assertEqual(manifest['totals']['succeeded'], 3)
        self.assertEqual(manifest['totals']['total'], 3)
        self.assertEqual(len(manifest['items']), 3)
        self.assertEqual({item['delivery'] for item in manifest['items']}, {'b64'})
        self.assertEqual(self.analysis.call_count, 1)
        self.assertEqual(self.image.call_count, 3)
        self.assertEqual(self.groups[0]['status'], 'analyzing')
        self.assertIn('generating', [g['status'] for g in self.groups])
        self.assertEqual(self.groups[-1]['status'], 'succeeded')
        self.assertEqual({i['status'] for i in self.items}, {'pending', 'running', 'succeeded'})
        for call in self.image.call_args_list:
            self.assertEqual(call.args[:3], ('offline-fixture', TEST_KEY, 'gpt-image-2.5-sunburst'))
            self.assertEqual(call.args[4], '1:1' if 'carousel' in call.args[3] else '3:4')
        self.assertNotIn(TEST_KEY, json.dumps(group, ensure_ascii=False))

    def test_manifest_records_url_delivery_and_200_errors_cannot_switch(self):
        self.image.side_effect = lambda *args, **kwargs: ImageResult(
            PNG, args[2], '1024x1024', args[5], 'auto', '/images/generations',
            delivery='url')
        group_id = self.start(carousel_count=1, detail_count=0)
        self.assertEqual(self.manifest(group_id)['items'][0]['delivery'], 'url')
        self.assertFalse(self.manager._can_switch(EcommerceImageError('read failed', 200)))
        self.assertFalse(self.manager._can_switch(EcommerceImageError('redirect response', 201)))
        self.assertTrue(self.manager._can_switch(EcommerceImageError('rejected', 400)))
        self.assertTrue(self.manager._can_switch(EcommerceImageError('connection failed')))

    def test_distinct_chat_and_image_lines_thinking_and_redaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = ConfigManager(Path(temporary) / 'config.json')
            config.config = dict(
                api={'base_url': 'https://active.example/v1', 'api_key': 'active-secret'},
                line_routing={'video': 'active', 'chat': 'chat-station', 'image': 'image-station'},
                stations=[
                    {'id': 'chat-station', 'enabled': True,
                     'base_url': 'https://chat.example', 'api_key': 'chat-secret'},
                    {'id': 'image-station', 'enabled': True,
                     'base_url': 'https://image.example/v1', 'api_key': 'image-secret'},
                ],
                paths={'output': str(Path(temporary) / 'output')},
                ecommerce={'analysis_model': 'config-analysis', 'image_model': 'config-image'},
                task_strategy={'max_concurrency': 1})
            manager = EcommerceImageManager.build_from_config(
                config, store=self.store,
                overrides={'analysis_model': 'override-analysis', 'image_model': 'override-image'})
            manager.analysis_func = self.analysis
            manager.image_func = self.image
            group_id = manager.start_group(
                ['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K', thinking=True)
        analysis_call = self.analysis.call_args
        self.assertEqual(analysis_call.args[:3],
                         ('https://chat.example/v1', 'chat-secret', 'override-analysis'))
        self.assertTrue(analysis_call.kwargs['thinking'])
        self.assertTrue(callable(analysis_call.kwargs['on_reasoning']))
        image_call = self.image.call_args
        self.assertEqual(image_call.args[:3],
                         ('https://image.example/v1', 'image-secret', 'override-image'))
        self.assertTrue(self.store.get_group(group_id)['config']['thinking'])
        self.assertNotIn('chat-secret', json.dumps(self.store.get_group(group_id), ensure_ascii=False))
        self.assertNotIn('image-secret', json.dumps(self.store.get_group(group_id), ensure_ascii=False))
        self.assertNotIn('active-secret', json.dumps(self.store.get_group(group_id), ensure_ascii=False))
        for secret in ('chat-secret', 'image-secret'):
            self.assertNotIn(secret, manager._clean('failure: ' + secret))

    def test_disabled_model_pools_use_only_the_selected_models(self):
        manager = self.build_pool_manager()
        manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        self.assertEqual(self.analysis.call_args.args[2], 'chosen-analysis')
        self.assertEqual(self.image.call_args.args[2], 'chosen-image')
        self.assertEqual(manager.analysis_model_candidates, [])
        self.assertEqual(manager.image_model_candidates, [])

    def test_selected_models_are_first_and_pool_candidates_are_deduplicated(self):
        manager = self.build_pool_manager(
            chat_pool={'enabled': True, 'models': [
                {'name': 'fallback-analysis', 'enabled': True},
                {'name': 'chosen-analysis', 'enabled': True},
                {'name': 'disabled-analysis', 'enabled': False},
                {'name': 'fallback-analysis', 'enabled': True},
            ]},
            image_pool={'enabled': True, 'models': [
                {'name': 'fallback-image', 'enabled': True},
                {'name': 'chosen-image', 'enabled': True},
            ]})
        self.assertEqual(manager._analysis_candidates(), ['chosen-analysis', 'fallback-analysis'])
        self.assertEqual(manager._image_candidates('chosen-image'), ['chosen-image', 'fallback-image'])

    def test_build_from_config_overrides_remain_first_with_enabled_pools(self):
        manager = self.build_pool_manager(
            chat_pool={'enabled': True, 'models': [
                {'name': 'pool-analysis', 'enabled': True},
                {'name': 'disabled-analysis', 'enabled': False},
            ]},
            image_pool={'enabled': True, 'models': [
                {'name': 'pool-image', 'enabled': True},
            ]})
        manager.analysis_model = 'form-analysis'
        manager.image_model = 'form-image'
        self.assertEqual(manager._analysis_candidates(), ['form-analysis', 'pool-analysis'])
        self.assertEqual(manager._image_candidates('form-image'), ['form-image', 'pool-image'])

    def test_resume_image_candidate_uses_saved_model_then_current_choice(self):
        manager = self.build_pool_manager(image_model='current-image')
        self.assertEqual(manager._image_candidates('saved-image'), ['saved-image', 'current-image'])

    def test_analysis_retries_once_after_http_failure_and_logs_model_switch(self):
        analysis_models = []

        def fail_once(*args, **kwargs):
            analysis_models.append(args[2])
            if len(analysis_models) == 1:
                raise EcommerceAnalysisError('HTTP 503: unavailable', 503)
            return fake_analysis(*args, **kwargs)

        self.analysis.side_effect = fail_once
        manager = self.build_pool_manager(chat_pool={
            'enabled': True, 'models': [{'name': 'fallback-analysis', 'enabled': True}]})
        group_id = manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        self.assertEqual(self.store.get_group(group_id)['status'], 'succeeded')
        self.assertEqual(analysis_models, ['chosen-analysis', 'fallback-analysis'])
        self.assertTrue(any('切换 fallback-analysis' in message for message, _level in self.logs))

    def test_analysis_stream_failure_after_http_200_never_switches(self):
        self.analysis.side_effect = EcommerceAnalysisError('流式中断', 200)
        manager = self.build_pool_manager(chat_pool={
            'enabled': True, 'models': [{'name': 'fallback-analysis', 'enabled': True}]})
        with self.assertRaises(EcommerceAnalysisError) as caught:
            manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        self.assertEqual(caught.exception.status_code, 200)
        self.assertEqual(self.analysis.call_count, 1)
        self.assertEqual(self.store.list_groups(limit=1)[0]['status'], 'failed')
        self.image.assert_not_called()

    def test_analysis_connection_failure_allows_one_fallback(self):
        analysis_models = []

        def fail_connection_once(*args, **kwargs):
            analysis_models.append(args[2])
            if len(analysis_models) == 1:
                raise EcommerceAnalysisError('connection failed')
            return fake_analysis(*args, **kwargs)

        self.analysis.side_effect = fail_connection_once
        manager = self.build_pool_manager(chat_pool={
            'enabled': True, 'models': [{'name': 'fallback-analysis', 'enabled': True}]})
        group_id = manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        self.assertEqual(self.store.get_group(group_id)['status'], 'succeeded')
        self.assertEqual(analysis_models, ['chosen-analysis', 'fallback-analysis'])

    def test_image_retries_503_and_persists_the_model_that_succeeded(self):
        self.image.side_effect = [EcommerceImageError('HTTP 503: unavailable', 503), fake_image]
        manager = self.build_pool_manager(image_pool={
            'enabled': True, 'models': [{'name': 'fallback-image', 'enabled': True}]})
        group_id = manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        item = self.store.list_items(group_id)[0]
        self.assertEqual([call.args[2] for call in self.image.call_args_list],
                         ['chosen-image', 'fallback-image'])
        self.assertEqual(item['model'], 'fallback-image')
        self.assertEqual(self.manifest(group_id)['items'][0]['model'], 'fallback-image')

    def test_image_resume_can_fall_back_to_current_choice_when_pool_is_disabled(self):
        group_id = self.start(carousel_count=1, detail_count=0)
        item = self.store.list_items(group_id)[0]
        item.update(status='failed', error='offline', model='saved-image')
        self.store.upsert_item(item)
        manager = self.build_pool_manager(image_model='current-image')
        manager.image_func.reset_mock()
        manager.image_func.side_effect = [EcommerceImageError('HTTP 503', 503), fake_image]
        manager.resume_group(group_id)
        self.assertEqual([call.args[2] for call in manager.image_func.call_args_list],
                         ['saved-image', 'current-image'])
        self.assertEqual(self.store.list_items(group_id)[0]['model'], 'current-image')

    def test_image_write_failure_does_not_retry_a_second_model(self):
        self.image.side_effect = fake_image
        manager = self.build_pool_manager(image_pool={
            'enabled': True, 'models': [{'name': 'fallback-image', 'enabled': True}]})
        from core.ecommerce import manager as manager_module
        original_write = manager_module._atomic_write

        def fail_image_write(path, content):
            if Path(path).name != 'manifest.json':
                raise OSError('offline disk failure')
            return original_write(path, content)

        with patch.object(manager_module, '_atomic_write', side_effect=fail_image_write):
            group_id = manager.start_group(
                ['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        self.assertEqual(self.image.call_count, 1)
        self.assertEqual(self.store.get_group(group_id)['status'], 'failed')
        self.assertEqual(self.store.list_items(group_id)[0]['status'], 'failed')

    def test_reasoning_log_is_throttled_and_bounded(self):
        def analysis(*args, **kwargs):
            callback = kwargs['on_reasoning']
            callback('思考片段' * 100)
            callback('同秒后续片段')
            return fake_analysis(*args, **kwargs)

        manager = self.make_manager(analysis_func=analysis)
        manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        messages = [message for message, level in self.logs if message.startswith('[思考中] ')]
        self.assertEqual(len(messages), 1)
        self.assertLessEqual(len(messages[0]) - len('[思考中] '), 200)
        self.assertEqual(next(level for message, level in self.logs if message.startswith('[思考中] ')), 'info')

    def test_resume_and_regenerate_keep_image_line_and_item_model(self):
        calls = []

        def image(*args, **kwargs):
            calls.append(args[:3])
            if len(calls) == 1:
                raise RuntimeError('offline first attempt failed')
            return fake_image(*args, **kwargs)

        manager = self.make_manager(image_func=image, analysis_base_url='chat-route-A',
                                    analysis_api_key='chat-key-A', image_base_url='image-route-B',
                                    image_api_key='image-key-B', image_model='saved-image-model')
        group_id = manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        item = self.store.list_items(group_id)[0]
        manager.image_model = 'later-config-model'
        manager.resume_group(group_id)
        manager.regenerate_items(group_id, [(item['kind'], item['seq'])])
        self.assertEqual(calls, [('image-route-B', 'image-key-B', 'saved-image-model')] * 3)
        self.assertEqual(self.analysis.call_count, 1)

    def test_concurrency_is_real_and_bounded(self):
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active, peak = 0, 0

        def image(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=10)
                return fake_image(*args, **kwargs)
            finally:
                with lock:
                    active -= 1
        self.manager = self.make_manager(concurrency=3, image_func=image)
        group_id = self.start()
        self.assertEqual(peak, 3)
        self.assertEqual(self.store.item_counts(group_id)['succeeded'], 3)

    def test_background_conflict_has_no_side_effects(self):
        with self.assertRaisesRegex(ValueError, '白底'):
            self.start(platforms=['amazon', 'xiaohongshu'])
        self.analysis.assert_not_called()
        self.image.assert_not_called()
        self.assertEqual(self.store.list_groups(), [])
        self.assertFalse((self.root / 'output').exists())

    def test_invalid_detail_ratio_has_no_side_effects(self):
        with self.assertRaises(ValueError):
            self.start(detail_ratio='8:7')
        self.analysis.assert_not_called()
        self.image.assert_not_called()
        self.assertEqual(self.store.list_groups(), [])
        self.assertFalse((self.root / 'output').exists())

    def test_ratio_conflicts_warn_but_use_explicit_ratios(self):
        first = get_platform('douyin')
        second = deepcopy(first)
        second.update(id='fixture', name='测试平台', defaultCarouselRatio='4:5',
                      defaultDetailRatio='1:1' if first['defaultDetailRatio'] != '1:1' else '9:16')
        group_id = self.start(platforms=[first, second])
        self.assertEqual(self.store.get_group(group_id)['status'], 'succeeded')
        self.assertEqual(sum(level == 'warning' for _, level in self.logs), 2)
        self.assertEqual({i['ratio'] for i in self.store.list_items(group_id)}, {'1:1', '3:4'})

    def test_directory_uses_group_prefix_for_empty_product_name(self):
        group_id = self.start(product_name='')
        self.assertTrue(Path(self.store.get_group(group_id)['output_dir']).name.startswith(group_id[:12] + '_'))

    def test_directory_sanitizes_chinese_name_and_avoids_same_minute_collision(self):
        first = self.start(product_name='茶/杯\\:*?"<>|')
        second = self.start(product_name='茶/杯\\:*?"<>|')
        dirs = [Path(self.store.get_group(g)['output_dir']) for g in (first, second)]
        self.assertNotEqual(dirs[0], dirs[1])
        for directory in dirs:
            self.assertTrue(directory.name.startswith('茶杯_'))
            self.assertIsNone(re.search(r'[\\/:*?"<>|]', directory.name))
            self.assertEqual((directory / '轮1.png').read_bytes(), PNG)

    def test_style_suffix_and_order_are_persisted_and_used(self):
        products = [self.root / '产品1.png', self.root / '产品2.png']
        style = self.root / '风格.png'
        group_id = self.start(product_image_paths=products, style_image_path=style)
        expected = [str(p.resolve()) for p in products + [style]]
        for call in self.image.call_args_list:
            self.assertIn('Reference image order', call.args[3])
            self.assertIn('Images 1-2', call.args[3])
            self.assertIn('Image 3 (the last image)', call.args[3])
            self.assertEqual(call.kwargs['reference_paths'], expected)
        for item in self.store.list_items(group_id):
            self.assertEqual(item['reference_paths'], expected)

    def test_product_only_suffix_is_used_and_persisted_for_every_item(self):
        products = [self.root / '产品1.png', self.root / '产品2.png']
        group_id = self.start(product_image_paths=products)
        expected_paths = [str(path.resolve()) for path in products]
        for call in self.image.call_args_list:
            self.assertTrue(call.args[3].endswith('Images 1-2 are all product references.'))
            self.assertIn('branding and text', call.args[3])
            self.assertNotIn('style reference', call.args[3])
            self.assertEqual(call.kwargs['reference_paths'], expected_paths)
        for item in self.store.list_items(group_id):
            self.assertTrue(item['prompt'].endswith('Images 1-2 are all product references.'))
            self.assertEqual(item['reference_paths'], expected_paths)

    def test_style_only_uses_one_style_reference_and_no_references_keep_prompt(self):
        style = self.root / '风格.png'
        self.start(style_image_path=style)
        for call in self.image.call_args_list:
            self.assertIn('Image 1 (the only reference image) is style reference only', call.args[3])
            self.assertEqual(call.kwargs['reference_paths'], [str(style.resolve())])
        self.image.reset_mock()
        self.start()
        self.assertTrue(all('Reference image order' not in c.args[3]
                            for c in self.image.call_args_list))

    def test_invalid_counts_ratios_tier_or_reference_counts_have_no_side_effects(self):
        for options in (dict(carousel_count=-1), dict(carousel_count=0, detail_count=0),
                        dict(carousel_ratio='8:7'), dict(tier='8K'),
                        dict(product_image_paths=['p'] * 7), dict(style_image_path=['a', 'b']),
                        dict(product_image_paths='one.png')):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.start(**options)
        self.analysis.assert_not_called()
        self.image.assert_not_called()
        self.assertEqual(self.store.list_groups(), [])
        self.assertFalse((self.root / 'output').exists())

    def test_one_failure_does_not_stop_others_and_errors_are_redacted(self):
        def image(*args, **kwargs):
            if args[3].endswith('carousel 2'):
                raise RuntimeError('fixture failed ' + TEST_KEY)
            return fake_image(*args, **kwargs)
        self.image.side_effect = image
        group_id = self.start()
        group = self.store.get_group(group_id)
        self.assertEqual(group['status'], 'partial')
        self.assertEqual(self.store.item_counts(group_id)['failed'], 1)
        manifest = self.manifest(group_id)
        failed = [i for i in manifest['items'] if i['status'] == 'failed']
        self.assertEqual([(i['kind'], i['seq']) for i in failed], [('carousel', 2)])
        self.assertIn('[REDACTED]', failed[0]['error'])
        self.assertNotIn(TEST_KEY, json.dumps([group, self.logs, self.items], ensure_ascii=False))
        self.assertEqual(self.image.call_count, 3)

    def test_all_failed_is_failed_without_automatic_retries(self):
        self.image.side_effect = RuntimeError('offline failure')
        group_id = self.start()
        self.assertEqual(self.store.get_group(group_id)['status'], 'failed')
        self.assertEqual(self.manifest(group_id)['totals']['failed'], 3)
        self.assertEqual(self.image.call_count, 3)

    def test_analysis_failure_is_persisted_and_raises_typed_redacted_error(self):
        self.analysis.side_effect = EcommerceAnalysisError('analysis ' + TEST_KEY, 503, 'body ' + TEST_KEY)
        with self.assertRaises(EcommerceAnalysisError) as caught:
            self.start()
        error = caught.exception
        self.assertEqual(error.status_code, 503)
        self.assertNotIn(TEST_KEY, str(error) + error.body_excerpt)
        self.assertIn('[REDACTED]', str(error))
        group = self.store.list_groups()[0]
        self.assertEqual(group['status'], 'failed')
        self.assertNotIn(TEST_KEY, group['error'])
        self.assertEqual(self.store.list_items(group['group_id']), [])
        self.image.assert_not_called()

    def test_injected_analysis_still_has_to_satisfy_count_and_index_contract(self):
        self.analysis.return_value = ([], '')
        self.analysis.side_effect = None
        with self.assertRaisesRegex(EcommerceAnalysisError, '期望'):
            self.start()
        self.image.assert_not_called()
        self.assertEqual(self.store.list_groups()[0]['status'], 'failed')

    def test_resume_skips_successful_file_and_reruns_interrupted_pending(self):
        group_id = self.start()
        records = self.store.list_items(group_id)
        for record, status in zip(records[1:], ('running', 'pending')):
            Path(record['file_path']).unlink()
            self.store.upsert_item(dict(record, status=status))
        self.image.reset_mock()
        self.analysis.reset_mock()
        self.manager = self.make_manager(store=EcommerceImageStore(self.store.path), image_model='changed-current-model')
        self.manager.resume_group(group_id)
        calls = [c.args[3] for c in self.image.call_args_list]
        self.assertEqual(sorted(calls), sorted(i['prompt'] for i in records[1:]))
        self.assertTrue(all(c.args[2] == 'gpt-image-2.5-sunburst' for c in self.image.call_args_list))
        self.analysis.assert_not_called()
        self.assertEqual(self.store.get_group(group_id)['status'], 'succeeded')
        self.manifest(group_id)
        self.image.reset_mock()
        self.manager.resume_group(group_id)
        self.image.assert_not_called()

    def test_resume_retries_failed_and_missing_or_empty_success_files(self):
        group_id = self.start()
        records = self.store.list_items(group_id)
        self.store.upsert_item(dict(records[0], status='failed'))
        Path(records[1]['file_path']).unlink()
        Path(records[2]['file_path']).write_bytes(b'')
        self.image.reset_mock()
        self.manager.resume_group(group_id)
        self.assertEqual(self.image.call_count, 3)
        self.assertEqual(self.manifest(group_id)['totals']['succeeded'], 3)

    def test_regeneration_repeats_only_requested_items_even_when_succeeded(self):
        group_id = self.start()
        records = self.store.list_items(group_id)
        unselected = deepcopy(records[1])
        self.image.reset_mock()
        self.manager.regenerate_items(group_id, [('carousel', 1), records[2]['item_id'], ('carousel', 1)])
        self.assertEqual(self.image.call_count, 2)
        self.assertEqual({c.args[3] for c in self.image.call_args_list}, {records[0]['prompt'], records[2]['prompt']})
        self.assertEqual(self.store.list_items(group_id)[1], unselected)
        self.manifest(group_id)

    def test_regeneration_validates_every_key_before_requests(self):
        group_id = self.start()
        self.image.reset_mock()
        with self.assertRaises(ValueError):
            self.manager.regenerate_items(group_id, [('carousel', 1), 'different-group:detail:1'])
        self.image.assert_not_called()

    def test_pre_cancelled_group_skips_every_image_request(self):
        cancel = threading.Event()
        cancel.set()
        group_id = self.start(cancel_event=cancel)
        self.image.assert_not_called()
        self.assertEqual(self.store.get_group(group_id)['status'], 'cancelled')
        self.assertEqual(self.manifest(group_id)['totals']['skipped'], 3)
        self.assertEqual({i['status'] for i in self.store.list_items(group_id)}, {'skipped'})
        self.manager.resume_group(group_id)
        self.image.assert_not_called()  # skipped tasks require an explicit regeneration.

    def test_cancel_during_first_request_skips_tasks_not_started(self):
        cancel = threading.Event()

        def image(*args, **kwargs):
            cancel.set()
            return fake_image(*args, **kwargs)
        self.image.side_effect = image
        self.manager = self.make_manager(concurrency=1)
        group_id = self.start(cancel_event=cancel)
        self.assertEqual(self.image.call_count, 1)
        counts = self.manifest(group_id)['totals']
        self.assertEqual(counts['succeeded'], 1)
        self.assertEqual(counts['skipped'], 2)

    def test_same_group_cannot_run_concurrently_across_managers(self):
        entered, release = threading.Event(), threading.Event()

        def image(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError('test did not release image worker')
            return fake_image(*args, **kwargs)
        self.image.side_effect = image
        self.manager = self.make_manager(concurrency=1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.start)
            try:
                self.assertTrue(entered.wait(timeout=10))
                group_id = self.store.list_groups()[0]['group_id']
                other = self.make_manager(store=EcommerceImageStore(self.store.path))
                with self.assertRaisesRegex(ValueError, '正在运行'):
                    other.resume_group(group_id)
                with self.assertRaisesRegex(ValueError, '正在运行'):
                    other.regenerate_items(group_id, [('carousel', 1)])
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10), group_id)
        self.assertEqual(self.image.call_count, 3)

    def test_unavailable_database_still_outputs_with_explicit_availability(self):
        path = self.root / 'corrupt.sqlite3'
        path.write_bytes(b'invalid sqlite')
        self.store = EcommerceImageStore(path)
        self.manager = self.make_manager()
        group_id = self.start()
        self.assertEqual(self.store.get_group(group_id)['status'], 'succeeded')
        self.assertFalse(self.manifest(group_id)['persistence_available'])
        self.assertTrue(any('数据库不可用' in message for message, _ in self.logs))

    def test_empty_output_is_failure_and_failed_atomic_replace_preserves_old_file(self):
        self.image.side_effect = lambda *a, **k: ImageResult(b'', a[2], '1024x1024', '1K', 'auto', 'fixture')
        group_id = self.start(carousel_count=1, detail_count=0)
        self.assertEqual(self.manifest(group_id)['totals']['failed'], 1)
        self.assertFalse((Path(self.store.get_group(group_id)['output_dir']) / '轮1.png').exists())
        self.image.side_effect = fake_image
        self.manager.resume_group(group_id)
        item = self.store.list_items(group_id)[0]
        from core.ecommerce import manager as manager_module
        original_replace = manager_module.os.replace

        def reject_image(source, target):
            if str(target).endswith('.png'):
                raise PermissionError('replace refused')
            return original_replace(source, target)
        with patch('core.ecommerce.manager.os.replace', side_effect=reject_image):
            self.manager.regenerate_items(group_id, [('carousel', 1)])
        self.assertEqual(Path(item['file_path']).read_bytes(), PNG)
        self.assertEqual(self.store.list_items(group_id)[0]['status'], 'failed')
        self.assertFalse(list(Path(item['file_path']).parent.glob('*.tmp')))

    def test_callback_exceptions_do_not_change_success_or_mutate_records(self):
        def callback(record):
            record['status'] = 'modified-by-ui'
            raise RuntimeError('callback error')
        self.manager = self.make_manager(on_group=callback, on_item=callback)
        group_id = self.start()
        self.assertEqual(self.manifest(group_id)['status'], 'succeeded')

    def test_manifest_failure_can_be_recovered_without_another_image_request(self):
        from core.ecommerce import manager as manager_module
        original_write = manager_module._atomic_write

        def reject_manifest(path, content):
            if Path(path).name == 'manifest.json':
                raise PermissionError('fixture denied ' + TEST_KEY)
            return original_write(path, content)
        with patch('core.ecommerce.manager._atomic_write', side_effect=reject_manifest):
            group_id = self.start()
        group = self.store.get_group(group_id)
        self.assertEqual(group['status'], 'failed')
        self.assertEqual(group['manifest']['status'], 'failed')
        self.assertNotIn(TEST_KEY, group['error'])
        self.assertEqual(self.store.item_counts(group_id)['succeeded'], 3)
        self.image.reset_mock()
        self.manager.resume_group(group_id)
        self.image.assert_not_called()
        self.assertEqual(self.manifest(group_id)['status'], 'succeeded')

    def test_output_directory_failure_is_recorded_before_any_image_request(self):
        group_id = self.start()
        directory = self.store.get_group(group_id)['output_dir']
        blocked = self.root / 'blocked-output'
        blocked.write_bytes(b'file instead of directory')
        self.store.upsert_group(dict(group_id=group_id, output_dir=str(blocked)))
        self.image.reset_mock()
        self.manager.resume_group(group_id)
        self.image.assert_not_called()
        self.assertEqual(self.store.get_group(group_id)['status'], 'failed')
        self.assertIn('输出目录不可用', self.store.get_group(group_id)['error'])
        self.store.upsert_group(dict(group_id=group_id, output_dir=directory))
        self.manager.resume_group(group_id)
        self.image.assert_not_called()
        self.assertEqual(self.manifest(group_id)['status'], 'succeeded')

    def test_missing_group_or_analysis_result_cannot_be_resumed(self):
        with self.assertRaises(ValueError):
            self.manager.resume_group('missing')
        self.store.upsert_group(dict(group_id='no-items'))
        with self.assertRaises(ValueError):
            self.manager.resume_group('no-items')
        self.image.assert_not_called()


class EcommerceConfigTests(unittest.TestCase):
    def test_old_configuration_merges_all_defaults_and_preserves_existing_values(self):
        expected = dict(analysis_model='gemini-3.8-flash', image_model='gpt-image-2.5-sunburst',
                        default_tier='1K', default_carousel_ratio='1:1', default_detail_ratio='3:4',
                        default_carousel_count=3, default_detail_count=4, language='zh', max_concurrency=5)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.json'
            old = dict(api={'base_url': 'offline-fixture', 'api_key': ''},
                       paths={'output': str(Path(temporary) / 'existing-output')},
                       workspace={'duration': 13}, custom={'preserve': [1, 2]},
                       migrations={'r6_unattended_defaults': True, 'history_to_sqlite': True})
            path.write_text(json.dumps(old), encoding='utf-8')
            manager = ConfigManager(path)
            self.assertEqual(manager.load_config()['ecommerce'], expected)
            manager.save_config()
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(saved['ecommerce'], expected)
            self.assertEqual(saved['workspace']['duration'], 13)
            self.assertEqual(saved['custom'], old['custom'])
            self.assertEqual(saved['paths']['output'], old['paths']['output'])
            self.assertIsNone(manager._ecommerce_store)
            self.assertEqual(manager.ecommerce_store().path, path.with_name('history.sqlite3'))
            self.assertIs(manager.ecommerce_store(), manager.ecommerce_store())
            self.assertEqual(manager.history_store().path, manager.ecommerce_store().path)

    def test_build_from_config_reads_overrides_and_concurrency_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = ConfigManager(Path(temporary) / 'config.json')
            config.config = dict(api={'base_url': 'offline-base', 'api_key': TEST_KEY},
                                 paths={'output': str(Path(temporary) / 'primary')},
                                 output_dir=str(Path(temporary) / 'alias'),
                                 ecommerce={'analysis_model': 'analysis-override', 'image_model': 'image-override'},
                                 task_strategy={'max_concurrency': 2})
            manager = EcommerceImageManager.build_from_config(config)
            self.assertEqual(manager.output_root.name, 'primary')
            self.assertEqual((manager.base_url, manager.api_key), ('offline-base', TEST_KEY))
            self.assertEqual((manager.analysis_model, manager.image_model), ('analysis-override', 'image-override'))
            self.assertEqual(manager.concurrency, 2)
            config.config['paths']['output'] = ''
            config.config['ecommerce']['max_concurrency'] = 3
            explicit = EcommerceImageStore(Path(temporary) / 'different.sqlite3')
            manager = EcommerceImageManager.build_from_config(config, store=explicit)
            self.assertIs(manager.store, explicit)
            self.assertEqual(manager.output_root.name, 'alias')
            self.assertEqual(manager.concurrency, 3)

    def test_legacy_constructor_falls_back_to_shared_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = EcommerceImageStore(Path(temporary) / 'history.sqlite3')
            manager = EcommerceImageManager('same-route', 'same-key', temporary, store)
            self.assertEqual((manager.analysis_base_url, manager.analysis_api_key), ('same-route', 'same-key'))
            self.assertEqual((manager.image_base_url, manager.image_api_key), ('same-route', 'same-key'))


if __name__ == '__main__':
    unittest.main()
