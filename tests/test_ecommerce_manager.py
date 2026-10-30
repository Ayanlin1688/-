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
        self.retry_wait = patch.object(EcommerceImageManager, '_wait_analysis_retry')
        self.wait_mock = self.retry_wait.start()
        self.addCleanup(self.retry_wait.stop)
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

    def test_default_image_dispatch_sends_two_and_seven_nano_banana_refs_as_gemini_json(self):
        models = ('Nano Banana 2', 'Nano Banana 2.1', 'Nano Banana Pro')
        catalog = {'image': {model: {
            'id': model,
            'supported_endpoint_types': ['openai-image', 'gemini'],
        } for model in models}}
        manager = self.make_manager(
            image_func=None,
            model_catalog=catalog,
        )
        response = Mock(status_code=200, text='', content=b'{}')
        response.json.return_value = {'candidates': [{'content': {'parts': [
            {'inlineData': {'mimeType': 'image/png',
                            'data': base64.b64encode(PNG).decode('ascii')}}]}}]}

        with patch('core.manual_image.HttpClient.request', autospec=True,
                   return_value=response) as request, \
                patch('core.ecommerce.manager.generate_image') as openai:
            for model in models:
                for count in (2, 7):
                    refs = []
                    for index in range(count):
                        path = self.root / f'{model.replace(" ", "-")}-ref-{count}-{index}.png'
                        path.write_bytes(PNG)
                        refs.append(str(path))
                    result, used_model = manager._request_image(dict(
                        model=model, prompt='fixture image', ratio='1:1', tier='1K',
                        reference_paths=refs))
                    self.assertEqual(result.png_bytes, PNG)
                    self.assertEqual(used_model, model)

                    call = request.call_args
                    self.assertEqual(call.args[-2], 'POST')
                    self.assertEqual(
                        call.args[-1],
                        f"offline-fixture/models/{model.replace(' ', '%20')}:generateContent")
                    body = call.kwargs['json']
                    self.assertEqual(len(body['contents'][0]['parts']), count + 1)
                    self.assertEqual(body['contents'][0]['parts'][0]['text'], 'fixture image')
                    self.assertEqual(
                        [part['inlineData']['data'] for part in body['contents'][0]['parts'][1:]],
                        [base64.b64encode(PNG).decode('ascii')] * count)
                    self.assertNotIn('files', call.kwargs)
                    self.assertNotIn('data', call.kwargs)
            self.assertEqual(request.call_count, len(models) * 2)
            openai.assert_not_called()

    def test_gemini_protocol_failure_does_not_fall_back_to_openai_multipart(self):
        manager = self.make_manager(
            image_func=None,
            model_catalog={'image': {'Nano Banana 2': {
                'id': 'Nano Banana 2',
                'supported_endpoint_types': ['openai-image', 'gemini'],
            }}},
        )
        response = Mock(status_code=400, text='unsupported model', content=b'unsupported model')
        with patch('core.manual_image.HttpClient.request', autospec=True,
                   return_value=response) as request, \
                patch('core.ecommerce.manager.generate_image') as openai:
            with self.assertRaises(EcommerceImageError) as caught:
                manager._request_image(dict(
                    model='Nano Banana 2', prompt='fixture image', ratio='1:1', tier='1K',
                    reference_paths=[]))
        self.assertEqual(request.call_count, 1)
        self.assertIn(':generateContent', request.call_args.args[-1])
        openai.assert_not_called()
        self.assertTrue(any('\u4e00' <= char <= '\u9fff' for char in str(caught.exception)))

    def test_default_image_dispatch_keeps_gpt_image_and_grok_on_openai_client(self):
        models = ('gpt-image-2.5-sunburst', 'grok-imagine-image')
        manager = self.make_manager(
            image_func=None,
            model_catalog={'image': {model: {
                'id': model,
                'supported_endpoint_types': ['openai-image', 'gemini'],
            } for model in models}},
        )
        for model in models:
            with self.subTest(model=model), \
                    patch('core.ecommerce.manager.generate_image', return_value=ImageResult(
                        PNG, model, '1024x1024', '1K', 'auto', '/images/edits')) as openai, \
                    patch('core.manual_image.generate_gemini_image') as gemini:
                result, used_model = manager._request_image(dict(
                    model=model, prompt='fixture image', ratio='1:1', tier='1K',
                    reference_paths=['one.png']))
            self.assertEqual(result.png_bytes, PNG)
            self.assertEqual(used_model, model)
            self.assertEqual(openai.call_args.args[:3], ('offline-fixture', TEST_KEY, model))
            self.assertEqual(openai.call_args.kwargs['reference_paths'], ['one.png'])
            gemini.assert_not_called()

    def test_manifest_records_url_delivery_and_200_errors_cannot_switch(self):
        self.image.side_effect = lambda *args, **kwargs: ImageResult(
            PNG, args[2], '1024x1024', args[5], 'auto', '/images/generations',
            delivery='url')
        group_id = self.start(carousel_count=1, detail_count=0)
        self.assertEqual(self.manifest(group_id)['items'][0]['delivery'], 'url')
        self.assertFalse(self.manager._can_switch(EcommerceImageError('read failed', 200)))
        self.assertFalse(self.manager._can_switch(EcommerceImageError('redirect response', 201)))
        self.assertFalse(self.manager._can_switch(EcommerceImageError('rejected', 400)))
        self.assertFalse(self.manager._can_switch(EcommerceImageError('connection failed')))

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

    def test_disabled_auto_failover_uses_only_the_selected_models(self):
        manager = self.build_pool_manager(
            chat_pool={'enabled': True, 'auto_failover': False, 'models': [
                {'name': 'fallback-analysis', 'enabled': True}]},
            image_pool={'enabled': True, 'auto_failover': False, 'models': [
                {'name': 'fallback-image', 'enabled': True}]})
        self.assertEqual(manager._analysis_candidates(), ['chosen-analysis'])
        self.assertEqual(manager._image_candidates('chosen-image'), ['chosen-image'])

    def test_candidate_chain_is_ordered_and_bounded_to_three_models(self):
        manager = self.build_pool_manager(
            chat_pool={'enabled': True, 'models': [
                {'name': f'chat-{index}', 'enabled': True} for index in range(1, 5)]},
            image_pool={'enabled': True, 'models': [
                {'name': f'image-{index}', 'enabled': True} for index in range(1, 5)]})
        self.assertEqual(manager._analysis_candidates(),
                         ['chosen-analysis', 'chat-1', 'chat-2'])
        self.assertEqual(manager._image_candidates('chosen-image'),
                         ['chosen-image', 'image-1', 'image-2'])

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

    def test_analysis_retries_same_model_after_http_failure(self):
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
        self.assertEqual(analysis_models, ['chosen-analysis', 'chosen-analysis'])
        self.wait_mock.assert_called_once_with(2, None)

    def test_analysis_503_retries_twice_then_succeeds(self):
        error = EcommerceAnalysisError('upstream_error', 503)
        self.analysis.side_effect = [error, error, fake_analysis([], [], [], [], [], 1, 0)]
        group_id = self.start(carousel_count=1, detail_count=0)
        self.assertEqual(self.store.get_group(group_id)['status'], 'succeeded')
        self.assertEqual(self.analysis.call_count, 3)
        self.assertEqual([call.args[0] for call in self.wait_mock.call_args_list], [2, 4])
        self.assertEqual(len({call.args[2] for call in self.analysis.call_args_list}), 1)

    def test_analysis_upstream_failure_does_not_switch_models(self):
        manager = self.make_manager(analysis_model='primary', analysis_model_candidates=['fallback'])
        error = EcommerceAnalysisError('upstream_error', 503)
        self.analysis.side_effect = [error, error, error, fake_analysis([], [], [], [], [], 1, 0)]
        with self.assertRaises(EcommerceAnalysisError):
            manager.start_group(['douyin'], '商品', '信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        self.assertEqual([call.args[2] for call in self.analysis.call_args_list],
                         ['primary', 'primary', 'primary'])
        self.assertFalse(any('切换 fallback' in message for message, _ in self.logs))

    def test_analysis_policy_never_retries_or_switches(self):
        for status, body in ((400, 'upstream_text_reply nudity'), (422, 'desnudez')):
            with self.subTest(status=status):
                self.analysis.reset_mock()
                self.wait_mock.reset_mock()
                self.analysis.side_effect = EcommerceAnalysisError('raw error', status, body)
                manager = self.make_manager(analysis_model_candidates=['fallback'])
                with self.assertRaises(EcommerceAnalysisError):
                    manager.start_group(['douyin'], '商品', '信息', 'zh', 1, 0, '1:1', '3:4', '1K')
                self.assertEqual(self.analysis.call_count, 1)
                self.wait_mock.assert_not_called()
                self.image.assert_not_called()

    def test_analysis_503_and_401_do_not_switch_models(self):
        manager = self.make_manager(analysis_model='first',
                                    analysis_model_candidates=['second', 'third'])
        self.analysis.side_effect = [
            EcommerceAnalysisError('HTTP 503', 503),
            EcommerceAnalysisError('HTTP 503', 503),
            EcommerceAnalysisError('HTTP 503', 503),
            EcommerceAnalysisError('HTTP 401', 401),
            fake_analysis([], [], [], [], [], 1, 0),
        ]
        with self.assertRaises(EcommerceAnalysisError):
            manager.start_group(['douyin'], '商品', '信息', 'zh', 1, 0,
                                '1:1', '3:4', '1K')
        self.assertEqual([call.args[2] for call in self.analysis.call_args_list],
                         ['first', 'first', 'first'])
        self.assertEqual(self.wait_mock.call_count, 2)
        self.assertEqual(sum('切换' in message for message, _ in self.logs), 0)

    def test_all_analysis_auth_failures_use_a_chinese_permission_summary(self):
        manager = self.make_manager(analysis_model='first',
                                    analysis_model_candidates=['second', 'third'])
        self.analysis.side_effect = [
            EcommerceAnalysisError('HTTP 401', 401),
            EcommerceAnalysisError('HTTP 401', 401),
            EcommerceAnalysisError('HTTP 401', 401),
        ]
        with self.assertRaises(EcommerceAnalysisError) as caught:
            manager.start_group(['douyin'], '商品', '信息', 'zh', 1, 0,
                                '1:1', '3:4', '1K')
        self.assertEqual([call.args[2] for call in self.analysis.call_args_list], ['first'])
        self.wait_mock.assert_not_called()
        self.assertIn('密钥无效或没有该模型的调用权限', caught.exception.message)
        self.assertIn('first', caught.exception.message)
        self.assertNotIn('second、third', caught.exception.message)
        self.assertNotIn('{', caught.exception.message)

    def test_image_skips_each_401_and_succeeds_on_a_later_enabled_model(self):
        manager = self.build_pool_manager(image_pool={
            'enabled': True, 'models': [
                {'name': 'image-second', 'enabled': True},
                {'name': 'image-third', 'enabled': True},
            ]})
        self.image.side_effect = [
            EcommerceImageError('HTTP 401', 401),
            EcommerceImageError('HTTP 403', 403),
            fake_image('', '', 'image-third', '', '1:1', '1K'),
        ]
        group_id = manager.start_group(['douyin'], '商品', '信息', 'zh', 1, 0,
                                       '1:1', '3:4', '1K')
        self.assertEqual(self.store.get_group(group_id)['status'], 'failed')
        self.assertEqual([call.args[2] for call in self.image.call_args_list], ['chosen-image'])
        self.assertEqual(sum('切换' in message for message, _ in self.logs), 0)

    def test_all_image_auth_failures_use_a_chinese_permission_summary(self):
        manager = self.build_pool_manager(image_pool={
            'enabled': True, 'models': [
                {'name': 'image-second', 'enabled': True},
                {'name': 'image-third', 'enabled': True},
            ]})
        self.image.side_effect = [
            EcommerceImageError('HTTP 401', 401),
            EcommerceImageError('HTTP 403', 403),
            EcommerceImageError('HTTP 401', 401),
        ]
        group_id = manager.start_group(['douyin'], '商品', '信息', 'zh', 1, 0,
                                       '1:1', '3:4', '1K')
        item = self.store.list_items(group_id)[0]
        self.assertEqual(self.store.get_group(group_id)['status'], 'failed')
        self.assertIn('密钥无效或没有该模型的调用权限', item['error'])
        self.assertIn('chosen-image', item['error'])
        self.assertNotIn('image-second', item['error'])
        self.assertEqual([call.args[2] for call in self.image.call_args_list], ['chosen-image'])

    def test_analysis_cancel_interrupts_retry_wait(self):
        self.retry_wait.stop()
        event = Mock()
        event.is_set.return_value = False
        event.wait.return_value = True
        self.analysis.side_effect = EcommerceAnalysisError('upstream_error', 503)
        group_id = self.start(cancel_event=event)
        self.assertEqual(self.store.get_group(group_id)['status'], 'cancelled')
        self.assertEqual(self.analysis.call_count, 1)
        event.wait.assert_called_once_with(0.1)
        self.image.assert_not_called()
        self.assertEqual(self.manifest(group_id)['totals']['cancelled'], 3)

    def test_analysis_retry_wait_checks_cancel_every_tenth_second(self):
        self.retry_wait.stop()
        event = Mock()
        event.wait.return_value = False
        EcommerceImageManager._wait_analysis_retry(2, event)
        self.assertEqual(event.wait.call_count, 20)
        self.assertTrue(all(call.args == (0.1,) for call in event.wait.call_args_list))

    def test_analysis_final_summary_is_chinese_and_has_no_json(self):
        manager = self.make_manager(analysis_model='primary', analysis_model_candidates=['fallback'])
        self.analysis.side_effect = EcommerceAnalysisError(
            'raw', 503, '{"error":{"message":"Upstream service temporarily unavailable","type":"upstream_error"}}')
        with self.assertRaises(EcommerceAnalysisError) as caught:
            manager.start_group(['douyin'], '商品', '信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        message = caught.exception.message
        self.assertIn('已重试 2 次', message)
        self.assertIn('primary', message)
        self.assertNotIn('fallback', message)
        self.assertIn('上游服务暂时不可用', message)
        self.assertNotIn('{', message)
        self.assertNotIn('upstream_error', message)
        self.assertEqual(message.count('AI 分析失败：'), 1)
        self.assertEqual(self.analysis.call_count, 3)

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
                raise EcommerceAnalysisError('无法连接到中转站，请检查网络、代理或 API 地址',
                                             category='connection', retryable=True, switchable=True)
            return fake_analysis(*args, **kwargs)

        self.analysis.side_effect = fail_connection_once
        manager = self.build_pool_manager(chat_pool={
            'enabled': True, 'models': [{'name': 'fallback-analysis', 'enabled': True}]})
        group_id = manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        self.assertEqual(self.store.get_group(group_id)['status'], 'succeeded')
        self.assertEqual(analysis_models, ['chosen-analysis', 'chosen-analysis'])

    def test_analysis_protocol_error_never_retries_or_switches(self):
        message = 'AI 流式响应帧不是合法 JSON'
        for status in (200, None):
            with self.subTest(status=status):
                self.analysis.reset_mock()
                self.wait_mock.reset_mock()
                self.logs.clear()
                self.analysis.side_effect = EcommerceAnalysisError(message, status)
                manager = self.make_manager(analysis_model='primary', analysis_model_candidates=['fallback'])
                with self.assertRaises(EcommerceAnalysisError) as caught:
                    manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
                self.assertEqual(self.analysis.call_count, 1)
                self.assertEqual(self.analysis.call_args.args[2], 'primary')
                self.wait_mock.assert_not_called()
                self.image.assert_not_called()
                self.assertFalse(any('切换 fallback' in text for text, _ in self.logs))
                group = self.groups[-1]
                self.assertEqual(group['status'], 'failed')
                self.assertEqual(self.store.get_group(group['group_id'])['status'], 'failed')
                self.assertIn(message, group['error'])
                self.assertNotIn('无法连接到中转站', group['error'])
                self.assertIn(message, caught.exception.message)
                self.assertNotIn('无法连接到中转站', caught.exception.message)
                self.assertFalse(caught.exception.retryable)
                self.assertFalse(caught.exception.switchable)

    def test_image_retries_503_and_persists_the_model_that_succeeded(self):
        self.image.side_effect = [EcommerceImageError('HTTP 503: unavailable', 503), fake_image]
        manager = self.build_pool_manager(image_pool={
            'enabled': True, 'models': [{'name': 'fallback-image', 'enabled': True}]})
        group_id = manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        item = self.store.list_items(group_id)[0]
        self.assertEqual([call.args[2] for call in self.image.call_args_list], ['chosen-image'])
        self.assertEqual(item['model'], 'chosen-image')
        self.assertEqual(self.manifest(group_id)['items'][0]['model'], 'chosen-image')

    def test_image_resume_can_fall_back_to_current_choice_when_pool_is_disabled(self):
        group_id = self.start(carousel_count=1, detail_count=0)
        item = self.store.list_items(group_id)[0]
        item.update(status='failed', error='offline', model='saved-image')
        self.store.upsert_item(item)
        manager = self.build_pool_manager(image_model='current-image')
        manager.image_func.reset_mock()
        manager.image_func.side_effect = [EcommerceImageError('HTTP 503', 503), fake_image]
        manager.resume_group(group_id)
        self.assertEqual([call.args[2] for call in manager.image_func.call_args_list], ['saved-image'])
        self.assertEqual(self.store.list_items(group_id)[0]['model'], 'saved-image')

    def test_image_write_failure_does_not_retry_a_second_model(self):
        self.image.side_effect = fake_image
        manager = self.build_pool_manager(image_pool={
            'enabled': True, 'models': [{'name': 'fallback-image', 'enabled': True}]})
        from core.ecommerce import manager as manager_module
        original_write = manager_module._atomic_write

        def fail_image_write(path, content, *, durable=False):
            if Path(path).name != 'manifest.json':
                raise OSError('offline disk failure')
            return original_write(path, content, durable=durable)

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

    def test_three_styles_order_persistence_reopen_and_regeneration(self):
        products = [self.root / f'product-{n}.png' for n in range(6)]
        styles = [self.root / f'style-{n}.png' for n in range(3)]
        expected = [str(p.resolve()) for p in products + styles]
        group_id = self.start(product_image_paths=products, style_image_paths=styles,
                              carousel_count=1, detail_count=0)
        self.assertEqual(self.analysis.call_args.kwargs['reference_paths'], expected)
        self.assertEqual(self.image.call_args.kwargs['reference_paths'], expected)
        self.assertIn('Images 7 and 8 determine composition', self.image.call_args.args[3])
        self.assertIn('Image 9 is auxiliary content and text information only', self.image.call_args.args[3])
        self.store = EcommerceImageStore(self.root / 'history.sqlite3')
        self.manager = self.make_manager()
        config = self.store.get_group(group_id)['config']
        self.assertEqual(config['style_image_paths'], expected[6:])
        self.assertEqual(config['product_image_paths'], expected[:6])
        self.image.reset_mock()
        self.manager.regenerate_items(group_id, [self.store.list_items(group_id)[0]['item_id']])
        self.assertEqual(self.image.call_args.kwargs['reference_paths'], expected)

    def test_legacy_single_style_record_reopens_and_regenerates_without_analysis(self):
        group_id = self.start(product_image_paths=['p.png'], style_image_path='style.png',
                              carousel_count=1, detail_count=0)
        group = self.store.get_group(group_id)
        references = group['config']['reference_paths']
        for key in ('style_image_paths', 'product_image_paths'):
            group['config'].pop(key)
        self.store.upsert_group(group)
        self.manager = self.make_manager()
        self.analysis.reset_mock()
        self.image.reset_mock()
        self.manager.regenerate_items(group_id, [self.store.list_items(group_id)[0]['item_id']])
        self.analysis.assert_not_called()
        self.assertEqual(self.image.call_count, 1)
        self.assertEqual(self.image.call_args.kwargs['reference_paths'], references)

    def test_analysis_multimage_rejection_stops_after_reduced_attempt_even_if_retryable(self):
        self.analysis.side_effect = EcommerceAnalysisError('多图限制', 400, 'too many images', retryable=True)
        with self.assertRaises(EcommerceAnalysisError):
            self.start(style_image_paths=['a', 'b', 'c'], carousel_count=1, detail_count=0)
        self.assertEqual(self.analysis.call_count, 2)
        self.wait_mock.assert_not_called()
        self.image.assert_not_called()
        self.assertEqual(self.store.list_groups()[0]['status'], 'failed')

    def test_four_styles_rejected_before_store_or_requests(self):
        with self.assertRaisesRegex(ValueError, '风格图最多3张'):
            self.start(style_image_paths=['a', 'b', 'c', 'd'])
        self.assertEqual(self.store.list_groups(), [])
        self.analysis.assert_not_called()
        self.image.assert_not_called()

    def test_multimage_400_falls_back_once_for_analysis_and_generation(self):
        products, styles = ['product.png'], ['style1.png', 'style2.png', 'style3.png']
        self.analysis.side_effect = [EcommerceAnalysisError('多图限制', 400, 'too many images'),
                                     fake_analysis(None, None, None, None, None, 1, 0)]
        self.image.side_effect = [EcommerceImageError('多图限制', 400, 'unsupported image count'),
                                  fake_image(None, None, 'model', None, None, '1K')]
        group_id = self.start(product_image_paths=products, style_image_paths=styles,
                              carousel_count=1, detail_count=0)
        full = [str(Path(p).resolve()) for p in products + styles]
        for client in (self.analysis, self.image):
            self.assertEqual(client.call_count, 2)
            self.assertEqual(client.call_args_list[0].kwargs['reference_paths'], full)
            self.assertEqual(client.call_args_list[1].kwargs['reference_paths'], full[:2])
            self.assertIn('最后 1 张为风格参考图', client.call_args_list[1].args[4 if client is self.analysis else 3])
        self.assertEqual(self.store.get_group(group_id)['status'], 'succeeded')
        self.assertIn(('中转站限制多图，本次仅使用第1张风格图', 'warning'), self.logs)

    def test_multimage_fallback_not_repeated_and_unrelated_errors_never_reduce(self):
        for status, body, calls in ((400, 'too many images', 2),
                                    (400, 'content policy unsupported images', 1),
                                    (401, 'unsupported images', 1),
                                    (429, 'too many images', 1),
                                    (200, 'unsupported image count', 1),
                                    (400, 'unsupported image model', 1)):
            with self.subTest(status=status, body=body):
                self.image.reset_mock()
                self.image.side_effect = EcommerceImageError('请求被拒绝', status, body)
                self.start(style_image_paths=['a', 'b', 'c'], carousel_count=1, detail_count=0)
                self.assertEqual(self.image.call_count, calls)

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

    def test_worker_exception_settles_running_items_without_another_request(self):
        self.manager = self.make_manager(concurrency=1)
        generate = self.manager._generate

        def crash_after_request(group, item, cancel_event):
            if item['kind'] == 'carousel' and item['seq'] == 2:
                self.manager._item(dict(item, status='running'))
                self.manager._request_image(item)
                raise RuntimeError('worker failure ' + TEST_KEY)
            return generate(group, item, cancel_event)

        with patch.object(self.manager, '_generate', side_effect=crash_after_request):
            group_id = self.start()
        items = self.store.list_items(group_id)
        self.assertEqual([item['status'] for item in items], ['succeeded', 'failed', 'succeeded'])
        self.assertEqual(self.image.call_count, 3)
        self.assertEqual(self.groups[-1]['status'], 'failed')
        self.assertEqual(self.manifest(group_id)['totals']['running'], 0)
        self.assertNotIn(TEST_KEY, json.dumps([items, self.groups, self.logs], ensure_ascii=False))

    def test_pool_submission_failure_skips_unsubmitted_items_and_waits_for_active_job(self):
        self.manager = self.make_manager(concurrency=1)
        submit = ThreadPoolExecutor.submit
        submitted = []

        def reject_second(pool, *args, **kwargs):
            submitted.append(args)
            if len(submitted) == 2:
                raise RuntimeError('pool closed ' + TEST_KEY)
            return submit(pool, *args, **kwargs)

        with patch('core.ecommerce.manager.ThreadPoolExecutor.submit', new=reject_second):
            group_id = self.start()
        self.assertEqual(len(submitted), 2)
        self.assertEqual(self.image.call_count, 1)
        self.assertEqual([item['status'] for item in self.store.list_items(group_id)],
                         ['succeeded', 'skipped', 'skipped'])
        manifest = self.manifest(group_id)
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['totals']['pending'], 0)
        self.assertEqual(manifest['totals']['running'], 0)

    def test_output_failure_settles_pending_and_running_items_without_requests(self):
        group_id = self.start()
        items = self.store.list_items(group_id)
        self.store.upsert_item(dict(items[0], status='pending'))
        self.store.upsert_item(dict(items[1], status='running'))
        blocked = self.root / 'not-a-directory'
        blocked.write_bytes(b'file')
        self.store.upsert_group(dict(group_id=group_id, output_dir=str(blocked)))
        self.image.reset_mock()
        self.manager.resume_group(group_id)
        self.image.assert_not_called()
        self.assertEqual([item['status'] for item in self.store.list_items(group_id)],
                         ['skipped', 'failed', 'succeeded'])
        group = self.store.get_group(group_id)
        self.assertEqual(group['status'], 'failed')
        self.assertEqual(group['totals']['pending'], 0)
        self.assertEqual(group['totals']['running'], 0)

    def test_analysis_failure_is_persisted_and_raises_typed_redacted_error(self):
        self.analysis.side_effect = EcommerceAnalysisError('analysis ' + TEST_KEY, 503, 'body ' + TEST_KEY)
        with self.assertRaises(EcommerceAnalysisError) as caught:
            self.start()
        error = caught.exception
        self.assertEqual(error.status_code, 503)
        self.assertNotIn(TEST_KEY, str(error) + error.body_excerpt)
        self.assertIn('[REDACTED]', error.body_excerpt)
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
        self.analysis.assert_not_called()
        self.image.assert_not_called()
        self.assertEqual(self.store.get_group(group_id)['status'], 'cancelled')
        self.assertEqual(self.manifest(group_id)['totals']['cancelled'], 3)
        self.assertEqual({i['status'] for i in self.store.list_items(group_id)}, {'cancelled'})
        self.manager.resume_group(group_id)
        self.image.assert_not_called()  # skipped tasks require an explicit regeneration.

    def test_switch_decision_uses_full_classification_before_excerpt_truncation(self):
        for status, suffix, switchable in ((404, 'model_not_found', True),
                                           (400, 'upstream_text_reply', False)):
            with self.subTest(status=status):
                error = EcommerceImageError('请求失败', status, 'x' * 600 + suffix)
                self.assertEqual(len(error.body_excerpt), 500)
                self.assertEqual(self.manager._can_switch(error), switchable)

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
        self.assertEqual(counts['cancelled'], 2)

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

        def reject_manifest(path, content, *, durable=False):
            if Path(path).name == 'manifest.json':
                raise PermissionError('fixture denied ' + TEST_KEY)
            return original_write(path, content, durable=durable)
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

    def test_each_finished_image_touches_group_after_item_callback_with_current_counts(self):
        self.manager = self.make_manager(concurrency=1)
        events = []
        touch = self.store.touch_group

        def on_item(item):
            self.items.append(item)
            if item['status'] in ('succeeded', 'failed'):
                events.append(('item', item['seq']))

        def on_touch(group_id, *, totals=None):
            events.append(('touch', deepcopy(totals)))
            if events[-2][0] == 'item':
                self.assertGreaterEqual(totals['succeeded'] + totals['failed'], 1)
                self.assertEqual(self.store.get_group(group_id)['status'], 'generating')
            return touch(group_id, totals=totals)

        self.manager.on_item = on_item
        with patch.object(self.store, 'touch_group', side_effect=on_touch) as touched:
            group_id = self.start()
        self.assertEqual(touched.call_count, 4)  # Each image, then the settling pass.
        self.assertEqual([event[0] for event in events],
                         ['item', 'touch', 'item', 'touch', 'item', 'touch', 'touch'])
        self.assertEqual([event[1]['succeeded'] for event in events if event[0] == 'touch'],
                         [1, 2, 3, 3])
        self.assertEqual(self.store.get_group(group_id)['status'], 'succeeded')

    def test_failed_images_touch_current_failure_counts_without_extra_requests(self):
        self.image.side_effect = EcommerceImageError('离线失败', 400)
        self.manager = self.make_manager(concurrency=1)
        with patch.object(self.store, 'touch_group', wraps=self.store.touch_group) as touched:
            group_id = self.start()
        self.assertEqual(self.image.call_count, 3)
        self.assertEqual([call.kwargs['totals']['failed'] for call in touched.call_args_list],
                         [1, 2, 3, 3])
        self.assertEqual(self.store.get_group(group_id)['status'], 'failed')

    def test_cancellation_touches_after_all_unstarted_items_are_settled(self):
        cancel = threading.Event()

        def image(*args, **kwargs):
            cancel.set()
            return fake_image(*args, **kwargs)

        self.image.side_effect = image
        self.manager = self.make_manager(concurrency=1)
        with patch.object(self.store, 'touch_group', wraps=self.store.touch_group) as touched:
            group_id = self.start(cancel_event=cancel)
        self.assertEqual(touched.call_count, 2)
        self.assertEqual(touched.call_args.kwargs['totals']['cancelled'], 2)
        self.assertEqual(touched.call_args.kwargs['totals']['succeeded'], 1)
        self.assertEqual(self.store.get_group(group_id)['status'], 'cancelled')

    def test_touch_preserves_latest_group_state_and_never_upserts_a_stale_group(self):
        self.store.upsert_group(dict(group_id='touch', status='generating',
                                     product_name='茶杯', config={'keep': 'original'}))
        other = EcommerceImageStore(self.store.path)
        self.addCleanup(other.close)
        updated = other.upsert_group(dict(group_id='touch', status='partial', error='并发状态',
                                          config={'keep': 'latest'}))
        with patch.object(self.store, 'upsert_group', wraps=self.store.upsert_group) as upsert:
            touched = self.manager._touch_group('touch', totals={'succeeded': 1})
        upsert.assert_not_called()
        for name in ('status', 'error', 'product_name', 'config', 'created_at', 'manifest'):
            self.assertEqual(touched[name], updated[name])
        self.assertEqual(touched['totals'], {'succeeded': 1})
        self.assertGreaterEqual(touched['updated_at'], updated['updated_at'])
        self.assertEqual(self.groups, [touched])

    def test_touch_notifies_after_persistence_and_keeps_callbacks_isolated(self):
        self.store.upsert_group(dict(group_id='touch-notify', status='generating',
                                     config={'keep': 'saved'}))
        received = []

        def on_group(record):
            received.append(deepcopy(record))
            self.assertEqual(self.store.get_group('touch-notify'), record)
            record['config']['keep'] = 'presentation-only'
            raise RuntimeError('offline presentation failure')

        self.manager.on_group = on_group
        touched = self.manager._touch_group('touch-notify', totals={'succeeded': 1})
        self.assertEqual(received, [touched])
        self.assertEqual(touched['config'], {'keep': 'saved'})
        self.assertEqual(self.store.get_group('touch-notify'), touched)
        self.assertEqual(self.logs, [])

    def test_terminal_group_timestamp_strictly_follows_item_touch_in_same_clock_tick(self):
        for status in ('succeeded', 'failed'):
            with self.subTest(status=status), patch('core.ecommerce.manager.time.time', return_value=100.0):
                group_id = f'same-tick-{status}'
                generating = self.store.upsert_group(dict(
                    group_id=group_id, status='generating', updated_at=100.0))
                self.manager._item(dict(group_id=group_id, kind='carousel', seq=1, status=status))
                touched = self.manager._touch_group(group_id)
                self.assertGreater(touched['updated_at'], generating['updated_at'])
                self.assertEqual(touched['status'], 'generating')

                terminal = self.manager._group(dict(generating, status=status))

                self.assertGreater(terminal['updated_at'], touched['updated_at'])
                self.assertEqual(terminal['status'], status)
                self.assertEqual(self.groups[-1], terminal)
                latest = self.store.get_group(group_id)
                self.assertEqual(latest['updated_at'], terminal['updated_at'])
                self.assertEqual(latest['status'], status)

    def test_missing_or_failed_touch_does_not_emit_group_notification(self):
        self.assertIsNone(self.manager._touch_group('absent', totals={}))
        with patch.object(self.store, 'touch_group', side_effect=OSError('offline database')):
            self.assertIsNone(self.manager._touch_group('absent', totals={}))
        self.assertEqual(self.groups, [])

    def test_touch_failure_warns_once_and_does_not_turn_saved_images_into_failures(self):
        with patch.object(self.store, 'touch_group', side_effect=OSError('offline database')) as touched:
            group_id = self.start()
        self.assertEqual(touched.call_count, 4)
        self.assertEqual(self.store.get_group(group_id)['status'], 'succeeded')
        self.assertEqual(self.image.call_count, 3)
        warnings = [message for message, level in self.logs if level == 'warning' and '组刷新失败' in message]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.manifest(group_id)['totals']['succeeded'], 3)

    def test_touch_count_failure_is_also_contained_and_warned_once(self):
        with patch.object(self.store, 'item_counts', side_effect=OSError('offline counts')), \
                patch.object(self.store, 'touch_group') as touched:
            self.assertIsNone(self.manager._touch_group('group'))
            self.assertIsNone(self.manager._touch_group('group'))
        touched.assert_not_called()
        self.assertEqual(len([message for message, level in self.logs
                              if level == 'warning' and '组刷新失败' in message]), 1)

    def test_missing_touch_method_skips_without_upserting_or_counting(self):
        old_store = Mock(spec=['item_counts', 'upsert_group'])
        self.manager.store = old_store
        self.assertIsNone(self.manager._touch_group('group'))
        old_store.item_counts.assert_not_called()
        old_store.upsert_group.assert_not_called()
        self.assertEqual(self.logs, [])

    def test_image_writes_skip_fsync_while_manifest_remains_durable(self):
        from core.ecommerce import manager as manager_module
        write = manager_module._atomic_write
        writes = []
        self.manager = self.make_manager(concurrency=1)
        with patch.object(manager_module.os, 'fsync') as fsync:
            def observed_write(path, content, *, durable=False):
                before = fsync.call_count
                write(path, content, durable=durable)
                writes.append((Path(path).suffix, durable, fsync.call_count - before))

            with patch.object(manager_module, '_atomic_write', side_effect=observed_write):
                group_id = self.start()
        self.assertEqual(writes, [('.png', False, 0)] * 3 + [('.json', True, 1)])
        self.assertEqual(fsync.call_count, 1)
        self.assertEqual(self.manifest(group_id)['status'], 'succeeded')
        for item in self.store.list_items(group_id):
            self.assertEqual(Path(item['file_path']).read_bytes(), PNG)

    def test_durable_atomic_write_fsync_failure_preserves_old_file_and_cleans_temp(self):
        from core.ecommerce.manager import _atomic_write
        path = self.root / 'durable.json'
        path.write_bytes(b'old-content')
        with patch('core.ecommerce.manager.os.fsync', side_effect=OSError('offline flush')) as fsync:
            with self.assertRaisesRegex(OSError, 'offline flush'):
                _atomic_write(path, b'new-content', durable=True)
            self.assertEqual(path.read_bytes(), b'old-content')
            self.assertEqual(list(self.root.glob('*.tmp')), [])
            _atomic_write(path, PNG)
        self.assertEqual(fsync.call_count, 1)
        self.assertEqual(path.read_bytes(), PNG)

    def test_timings_measure_request_and_write_before_ui_callback_without_default_log_noise(self):
        from core.ecommerce import manager as manager_module
        clock = [10.]
        write = manager_module._atomic_write

        def image(*args, **kwargs):
            clock[0] += 2.5
            result = fake_image(*args, **kwargs)
            return ImageResult(result.png_bytes, result.model, result.size, result.tier_used,
                               result.quality, result.endpoint, delivery='url')

        def observed_write(path, content, *, durable=False):
            write(path, content, durable=durable)
            if Path(path).suffix == '.png':
                clock[0] += .005

        def callback(item):
            if item['status'] == 'succeeded':
                clock[0] += 100  # UI callback time is excluded from item processing.

        self.image.side_effect = image
        self.manager.on_item = callback
        with patch.object(manager_module.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(manager_module, '_atomic_write', side_effect=observed_write):
            self.start(carousel_count=1, detail_count=0)
        timing, = self.manager.timings_snapshot()
        self.assertEqual((timing['kind'], timing['seq'], timing['model'], timing['delivery'], timing['status']),
                         ('carousel', 1, self.manager.image_model, 'url', 'succeeded'))
        self.assertEqual(timing['request_seconds'], 2.5)
        self.assertAlmostEqual(timing['write_seconds'], .005)
        self.assertAlmostEqual(timing['total_seconds'], 2.505)
        self.assertIsNone(timing['decode_seconds'])
        self.assertFalse(any(level == 'debug' for _, level in self.logs))

    def test_timings_record_one_failure_for_a_failed_request_with_no_write_time(self):
        from core.ecommerce import manager as manager_module
        clock = [10.]

        def image(*args, **kwargs):
            clock[0] += 3
            raise EcommerceImageError('离线失败', 400)

        self.image.side_effect = image
        with patch.object(manager_module.time, 'monotonic', side_effect=lambda: clock[0]):
            self.start(carousel_count=1, detail_count=0)
        timing, = self.manager.timings_snapshot()
        self.assertEqual(timing['status'], 'failed')
        self.assertEqual(timing['request_seconds'], 3)
        self.assertEqual(timing['total_seconds'], 3)
        self.assertEqual(timing['write_seconds'], 0)
        self.assertIsNone(timing['delivery'])
        self.assertIsNone(timing['decode_seconds'])

    def test_timings_record_write_failure_without_issuing_a_second_request(self):
        from core.ecommerce import manager as manager_module
        clock = [10.]
        write = manager_module._atomic_write

        def image(*args, **kwargs):
            clock[0] += 2
            return fake_image(*args, **kwargs)

        def fail_image_write(path, content, *, durable=False):
            if Path(path).suffix == '.png':
                clock[0] += .125
                raise OSError('offline disk failure')
            return write(path, content, durable=durable)

        self.image.side_effect = image
        with patch.object(manager_module.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(manager_module, '_atomic_write', side_effect=fail_image_write):
            self.start(carousel_count=1, detail_count=0)
        timing, = self.manager.timings_snapshot()
        self.assertEqual(timing['status'], 'failed')
        self.assertEqual(timing['delivery'], 'b64')
        self.assertEqual(timing['request_seconds'], 2)
        self.assertEqual(timing['write_seconds'], .125)
        self.assertEqual(timing['total_seconds'], 2.125)
        self.assertEqual(self.image.call_count, 1)

    def test_timing_covers_the_existing_failover_chain_once_with_the_used_model(self):
        from core.ecommerce import manager as manager_module
        clock = [10.]

        def image(*args, **kwargs):
            clock[0] += 2
            if args[2] == 'chosen-image':
                raise EcommerceImageError('model not found', 400)
            return fake_image(*args, **kwargs)

        self.image.side_effect = image
        manager = self.build_pool_manager(image_pool={
            'enabled': True, 'models': [{'name': 'fallback-image', 'enabled': True}]})
        with patch.object(manager_module.time, 'monotonic', side_effect=lambda: clock[0]):
            manager.start_group(['douyin'], '商品', '商品信息', 'zh', 1, 0, '1:1', '3:4', '1K')
        timing, = manager.timings_snapshot()
        self.assertEqual(timing['model'], 'fallback-image')
        self.assertEqual(timing['request_seconds'], 4)
        self.assertEqual(timing['status'], 'succeeded')
        self.assertEqual(self.image.call_count, 2)

    def test_timing_snapshots_are_independent_and_keep_only_latest_200_items(self):
        group = {'group_id': 'bounded', 'output_dir': str(self.root)}
        with patch.object(self.manager, '_item', side_effect=lambda item: item), \
                patch.object(self.manager, '_touch_group'), \
                patch('core.ecommerce.manager._atomic_write'), \
                patch('core.ecommerce.manager._valid_file', return_value=True):
            for seq in range(1, 206):
                item = dict(item_id=f'bounded:carousel:{seq}', group_id='bounded', kind='carousel',
                            seq=seq, model=self.manager.image_model, prompt='offline fixture',
                            ratio='1:1', tier='1K', reference_paths=[])
                self.manager._generate(group, item, None)
        snapshot = self.manager.timings_snapshot()
        self.assertEqual(len(snapshot), 200)
        self.assertEqual([record['seq'] for record in snapshot], list(range(6, 206)))
        snapshot[0]['status'] = 'changed'
        snapshot.clear()
        current = self.manager.timings_snapshot()
        self.assertEqual(len(current), 200)
        self.assertEqual(current[0]['status'], 'succeeded')


    def test_cancel_during_failed_request_prevents_model_retry_and_new_requests(self):
        cancel = threading.Event()
        self.manager = self.make_manager(concurrency=1, image_model_candidates=['fallback'])
        def rejected(*args, **kwargs):
            cancel.set()
            raise EcommerceImageError('model not found', 404, 'model not found', retry_safe=True)
        self.image.side_effect = rejected
        group_id = self.start(cancel_event=cancel)
        self.assertEqual(self.image.call_count, 1)
        self.assertEqual(self.store.get_group(group_id)['status'], 'cancelled')
        self.assertEqual([item['status'] for item in self.store.list_items(group_id)], ['cancelled']*3)



class EcommerceConfigTests(unittest.TestCase):
    def test_old_configuration_merges_all_defaults_and_preserves_existing_values(self):
        expected = dict(analysis_model='gemini-3.8-flash', image_model='gpt-image-2.5-sunburst',
                        default_tier='1K', default_carousel_ratio='1:1', default_detail_ratio='3:4',
                        default_carousel_count=3, default_detail_count=4, language='zh', max_concurrency=5,
                        thumbnail_adaptive=True)
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
