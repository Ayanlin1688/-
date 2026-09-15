"""Per-prompt decisions exercised through actual local upload/submit/poll/download."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication
from core.config_manager import DEFAULT_CONFIG
from core.task_manager import TaskManager
from test_pool_execution import PoolServer
from test_task_manager import wait_until

H3_COMPLETE = '''subject_definitions:
<Subject 1> is a blanket from <Picture 1>.
summary:
[reference generation] A blanket demonstration.
retention_analysis:
<Subject 1>: fully_preserved
detailed_description:
[Shot 1] A hand unfolds the blanket.
overall_soundscape:
Cloth rustles.
non_diegetic_music:
N/A'''


def catalog_fixture():
    records = {}
    for model, resolutions, audio, seed, duration, images in (
        ('MiniMax-H3', ['1080p', '2K', '4K'], False, False, list(range(4,16)), 9),
        ('video-v2', ['720p'], True, False, [5,10,15], 9),
        ('video-v3', ['720p'], True, True, list(range(4,31)), 30),
        ('video-v3-480p', ['480p'], True, True, list(range(4,31)), 30)):
        records[model] = dict(id=model, name=model, family='video-v3' if model.endswith('-480p') else model,
            kind='video', available=True, protocol_known=True, ratios=['16:9','9:16','1:1'],
            resolutions=resolutions, durations=duration, audio=audio, seed=seed, max_images=images,
            description='', pricing_text='计费未提供', aliases=[])
    return records


class DetectedTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.prompts = self.root/'prompts'; self.prompts.mkdir()
        images = self.root/'images'; images.mkdir()
        for i in range(1,4):
            (images/f'1({i}).jpg').write_bytes(b'good-image')
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config['prompt_detection'] = dict(enabled=True, fallback_model='')
        self.config['_model_catalog'] = catalog_fixture()
        self.config['paths'] = dict(prompts=str(self.prompts), images=str(images), output=str(self.root/'out'))
        self.config['workspace'].update(model='video-v3', resolution='1080p', aspect_ratio='9:16', duration=8, poll_interval=.01)
        self.config['task_strategy'].update(max_retries=2, retry_interval=.01, unmatched_prompt='仍提交文生视频')
        self.config['model_pool'].update(cooldown=.01, models=[dict(name='video-v2', enabled=True), dict(name='video-v3', enabled=True)])
        self.manager = TaskManager(); self.logs = []
        self.manager.log_message.connect(lambda message, level: self.logs.append(message))

    def tearDown(self):
        self.manager.cancel_all(); wait_until(lambda: not self.manager.is_running)
        self.temp.cleanup()

    def start(self, server):
        self.config['api'].update(base_url=server.base, api_key='local-fixture-only', upload_url=server.base+'/upload')
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running, timeout=15000)
        return [json.loads(body) for path, _, body in server.calls if path == '/videos']

    def test_three_prompt_formats_submit_different_models_and_fields(self):
        # 本用例按扫描顺序逐个校验三种格式的提交字段（串行语义）；全队列并发由专项测试覆盖。
        self.config['task_strategy']['max_concurrency'] = 1
        texts = ['subject_definitions: blanket\n[Shot 1] <Picture 1>',
                 '镜头1：展示@图1。镜头2：面料特写。', '自然光照亮桌上的玫瑰毯子。']
        for i, text in enumerate(texts, 1):
            (self.prompts/f'{i}.txt').write_text(text, encoding='utf-8')
        with PoolServer() as server:
            bodies = self.start(server)
            self.assertEqual([body['model'] for body in bodies], ['MiniMax-H3','video-v2','video-v3'])
            self.assertEqual(bodies[0]['size'], '1088x1920')
            self.assertEqual(len(bodies[0]['images']), 3)
            self.assertEqual(bodies[1]['aspect_ratio'], '9:16')
            self.assertEqual(bodies[1]['resolution'], '720p')
            self.assertEqual(bodies[1]['prompt'], '镜头1：展示@Image1。镜头2：面料特写。')
            self.assertEqual(bodies[2]['ratio'], '9:16')
            self.assertEqual([task['status'] for task in self.manager.tasks], ['completed']*3)
            self.assertTrue(any('任务1使用模型 MiniMax-H3（自动识别）' in text for text in self.logs))

    def test_manual_model_is_locked_even_with_failover_enabled(self):
        path = self.prompts/'1.txt'; path.write_text(H3_COMPLETE, encoding='utf-8')
        self.config['model_overrides'] = {str(path): 'video-v2'}
        self.config['model_pool']['enabled'] = True
        with PoolServer() as server:
            server.fail_models = {'video-v2'}
            bodies = self.start(server)
            self.assertEqual([body['model'] for body in bodies], ['video-v2']*3)
            self.assertEqual(self.manager.tasks[0]['model_source'], 'manual')
            self.assertEqual(self.manager.tasks[0]['status'], 'failed')

    def test_detected_h3_adapts_720p_default_to_catalog_supported_size(self):
        (self.prompts/'1.txt').write_text('subject_definitions: blanket\n[Shot 1] <Picture 1>', encoding='utf-8')
        self.config['workspace']['resolution'] = '720p'
        with PoolServer() as server:
            bodies = self.start(server)
            self.assertEqual([body['size'] for body in bodies], ['1088x1920'])
            self.assertEqual(self.manager.tasks[0]['effective_parameters']['resolution'], '1080p')

    def test_detected_model_outside_pool_can_fail_over_to_enabled_pool(self):
        (self.prompts/'1.txt').write_text(H3_COMPLETE, encoding='utf-8')
        self.config['model_pool']['enabled'] = True
        with PoolServer() as server:
            server.fail_models = {'MiniMax-H3'}
            bodies = self.start(server)
            self.assertEqual([body['model'] for body in bodies], ['MiniMax-H3','video-v2'])
            self.assertEqual(self.manager.tasks[0]['status'], 'completed')

    def test_detected_model_outside_pool_retries_same_model_without_failover(self):
        (self.prompts/'1.txt').write_text(H3_COMPLETE, encoding='utf-8')
        self.config['model_pool'].update(enabled=True, auto_failover=False)
        self.config['task_strategy']['max_retries'] = 1
        with PoolServer() as server:
            server.fail_models = {'MiniMax-H3'}
            bodies = self.start(server)
            self.assertEqual([body['model'] for body in bodies], ['MiniMax-H3']*2)
            self.assertEqual(self.manager.tasks[0]['status'], 'failed')

    def test_detected_model_history_resumes_the_same_actual_model(self):
        (self.prompts/'1.txt').write_text(H3_COMPLETE, encoding='utf-8')
        self.config['model_pool']['enabled'] = True
        with PoolServer() as server:
            server.fail_models = {'MiniMax-H3'}
            first = self.start(server)
            self.assertEqual([body['model'] for body in first], ['MiniMax-H3', 'video-v2'])
            task_id = self.manager.tasks[0]['task_id']
            before = len(server.calls)
            second = self.start(server)
            self.assertEqual([body['model'] for body in second], ['MiniMax-H3', 'video-v2'])
            self.assertEqual(len(server.calls), before)
            self.assertEqual(self.manager.tasks[0]['task_id'], task_id)
            self.assertEqual(self.manager.tasks[0]['model'], 'video-v2')

    def test_changed_manual_override_does_not_recover_an_unrelated_model(self):
        path = self.prompts/'1.txt'; path.write_text(H3_COMPLETE, encoding='utf-8')
        self.config['model_pool']['enabled'] = True
        self.config['model_overrides'] = {str(path): 'video-v2'}
        with PoolServer() as server:
            self.start(server)
            self.config['model_overrides'] = {str(path): 'video-v3'}
            bodies = self.start(server)
            self.assertEqual([body['model'] for body in bodies], ['video-v2', 'video-v3'])
            self.assertEqual(self.manager.tasks[0]['model'], 'video-v3')
            self.assertEqual(self.manager.tasks[0]['requested_model'], 'video-v3')

    def test_delisted_actual_model_keeps_prior_task_id_and_prevents_resubmission(self):
        (self.prompts/'1.txt').write_text(H3_COMPLETE, encoding='utf-8')
        self.config['model_pool']['enabled'] = True
        with PoolServer() as server:
            server.fail_models = {'MiniMax-H3'}
            self.start(server)
            original = copy.deepcopy(self.manager.tasks[0])
            self.assertEqual(original['model'], 'video-v2')
            del self.config['_model_catalog']['video-v2']
            before = len(server.calls)
            self.start(server)
            self.assertEqual(len(server.calls), before)
            self.assertEqual(self.manager.tasks[0]['task_id'], original['task_id'])
            self.assertEqual(self.manager.tasks[0]['model'], 'video-v2')

    def test_delisted_pool_model_is_not_scheduled(self):
        (self.prompts/'1.txt').write_text('pool legacy prompt', encoding='utf-8')
        self.config['prompt_detection']['enabled'] = False
        self.config['model_pool']['enabled'] = True
        self.config['_model_catalog']['video-v2']['available'] = False
        with PoolServer() as server:
            bodies = self.start(server)
            self.assertEqual([body['model'] for body in bodies], ['video-v3'])
            self.assertEqual(self.manager.tasks[0]['status'], 'completed')

    def test_nonvideo_pool_entry_is_excluded_without_consuming_retry(self):
        (self.prompts/'1.txt').write_text('pool legacy prompt', encoding='utf-8')
        self.config['prompt_detection']['enabled'] = False
        self.config['model_pool']['enabled'] = True
        self.config['_model_catalog']['video-v2']['kind'] = 'image'
        with PoolServer() as server:
            bodies = self.start(server)
            self.assertEqual([body['model'] for body in bodies], ['video-v3'])
            self.assertEqual(self.manager.tasks[0]['retry_count'], 0)

    def test_expanded_current_capabilities_do_not_reuse_old_shorter_generation(self):
        path = self.prompts/'1.txt'; path.write_text('simple product shot', encoding='utf-8')
        self.config['model_overrides'] = {str(path): 'video-v3'}
        self.config['workspace']['duration'] = 15
        self.config['_model_catalog']['video-v3']['durations'] = list(range(4, 16))
        with PoolServer() as server:
            self.start(server)
            first_id = self.manager.tasks[0]['task_id']
            self.config['_model_catalog']['video-v3']['durations'] = list(range(4, 31))
            self.config['workspace']['duration'] = 20
            bodies = self.start(server)
            self.assertEqual([body['duration'] for body in bodies], [15, 20])
            self.assertNotEqual(self.manager.tasks[0]['task_id'], first_id)

    def test_detected_pool_model_waits_for_its_own_cooldown(self):
        (self.prompts/'1.txt').write_text('镜头1：展示@图1。', encoding='utf-8')
        self.config['model_pool'].update(enabled=True, models=[
            dict(name='video-v2', enabled=True, status='冷却中', cooldown_until=time.time()+.3),
            dict(name='video-v3', enabled=True, status='健康')])
        with PoolServer() as server:
            self.config['api'].update(base_url=server.base, api_key='local-fixture-only', upload_url=server.base+'/upload')
            self.manager.start_tasks(self.config)
            wait_until(lambda: self.manager.tasks and self.manager.tasks[0]['status'] == 'cooling')
            self.assertFalse(any(path == '/videos' for path, _, _ in server.calls))
            wait_until(lambda: not self.manager.is_running)
            bodies = [json.loads(body) for path, _, body in server.calls if path == '/videos']
            self.assertEqual([body['model'] for body in bodies], ['video-v2'])

    def test_fallback_model_adapts_parameters_with_pool_disabled(self):
        (self.prompts/'1.txt').write_text('subject_definitions: one marker', encoding='utf-8')
        self.config['prompt_detection']['fallback_model'] = 'video-v2'
        with PoolServer() as server:
            body = self.start(server)[0]
            self.assertEqual(body['model'], 'video-v2')
            self.assertEqual((body['duration'], body['resolution']), (10, '720p'))

    def test_unknown_protocol_override_fails_before_upload(self):
        path = self.prompts/'1.txt'; path.write_text('product prompt', encoding='utf-8')
        self.config['_model_catalog']['future-video'] = dict(
            id='future-video', name='future-video', family='', kind='video', available=True,
            protocol_known=False, ratios=[], resolutions=[], durations=[],
            audio=False, seed=False, max_images=0)
        self.config['model_overrides'] = {str(path): 'future-video'}
        with PoolServer() as server:
            self.assertEqual(self.start(server), [])
            self.assertEqual(server.calls, [])
            self.assertEqual(self.manager.tasks[0]['status'], 'failed')
            self.assertIn('协议', self.manager.tasks[0]['error'])

    def test_variant_wire_id_and_frozen_batch_survive_catalog_change(self):
        for i in (1,2):
            path = self.prompts/f'{i}.txt'; path.write_text('简单展示产品。', encoding='utf-8')
            self.config['model_overrides'][str(path)] = 'video-v3-480p'
        with PoolServer() as server:
            server.processing_seconds = .15
            self.config['api'].update(base_url=server.base, api_key='local-fixture-only', upload_url=server.base+'/upload')
            self.manager.start_tasks(self.config)
            wait_until(lambda: len(server.jobs) == 1)
            self.config['_model_catalog'] = {}
            self.config['model_overrides'] = {}
            wait_until(lambda: not self.manager.is_running)
            bodies = [json.loads(body) for path, _, body in server.calls if path == '/videos']
            self.assertEqual([body['model'] for body in bodies], ['video-v3-480p']*2)
            self.assertEqual([body['resolution'] for body in bodies], ['480p']*2)
            self.assertEqual([task['status'] for task in self.manager.tasks], ['completed']*2)
