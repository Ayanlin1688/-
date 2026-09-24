"""Conservative classification and persistent per-file model choices."""
import copy
from pathlib import Path
import tempfile
import unittest
from core.config_manager import DEFAULT_CONFIG
from core.prompt_detector import detect_model, annotate_tasks


class PromptDetectorTests(unittest.TestCase):
    def test_h3_requires_two_distinct_signatures(self):
        self.assertEqual(detect_model('subject_definitions: red blanket\n[Shot 1] pan slowly'), 'MiniMax-H3')
        self.assertEqual(detect_model('(S1) says hello\n<Subject 2> smiles'), 'MiniMax-H3')
        self.assertEqual(detect_model('retention_analysis: hook\ndetailed_description: texture'), 'MiniMax-H3')
        self.assertEqual(detect_model('subject_definitions: a\nsubject_definitions: b'), '')
        self.assertEqual(detect_model('[Shot 1] camera\n[Shot 2] product'), '')

    def test_structured_and_natural_chinese_are_distinguished(self):
        self.assertEqual(detect_model('镜头1：@图1放在桌上。镜头2：展示细节。'), 'video-v2')
        self.assertEqual(detect_model('第一镜：展示产品，第二镜：特写。'), 'video-v2')
        self.assertEqual(detect_model('请保持@图片1的外形。'), 'video-v2')
        self.assertEqual(detect_model('镜头缓慢推进，展示桌上的玫瑰毯子。'), 'video-v3')
        self.assertEqual(detect_model('以自然光展示产品的面料与褶皱，保持产品外形。'*15), 'video-v2')

    def test_short_description_precedes_english_and_long_english_uses_grok(self):
        self.assertEqual(detect_model('A blanket on a table in natural daylight.'), 'video-v3')
        self.assertEqual(detect_model('The camera glides past the product with soft studio light and shows every detail. '*4),
                         'grok-imagine-1.5-video')

    def test_ambiguous_structures_and_empty_text_fall_back(self):
        for prompt in ('', '   ', '1234', 'subject_definitions: camera moves', '{"unknown_structure": "value"}',
                       'title: product\nscene: unknown', 'Это длинное описание продукта.'):
            with self.subTest(prompt=prompt):
                self.assertEqual(detect_model(prompt), '')

    def test_h3_wins_over_chinese_and_image_markers(self):
        self.assertEqual(detect_model('subject_definitions: @图1\nretention_analysis: 中文镜头1'), 'MiniMax-H3')

    def test_manual_override_and_reset_keep_reference_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'玫瑰毯子1.txt'
            path.write_text('subject_definitions: blanket\n[Shot 1] texture', encoding='utf-8')
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['prompt_detection'] = dict(enabled=True, fallback_model='video-v2')
            config['model_overrides'] = {str(path): 'video-v3'}
            tasks = [dict(prompt_path=str(path), images=['three', 'one', 'two'])]
            annotate_tasks(tasks, config)
            self.assertEqual(tasks[0]['detected_model'], 'MiniMax-H3')
            self.assertEqual(tasks[0]['requested_model'], 'video-v3')
            self.assertEqual(tasks[0]['model_source'], 'manual')
            self.assertTrue(tasks[0]['model_locked'])
            self.assertEqual(tasks[0]['images'], ['three', 'one', 'two'])
            config['model_overrides'] = {}
            annotate_tasks(tasks, config)
            self.assertEqual(tasks[0]['requested_model'], 'MiniMax-H3')
            self.assertEqual(tasks[0]['model_source'], 'auto')
            config['prompt_detection']['enabled'] = False
            annotate_tasks(tasks, config)
            self.assertEqual(tasks[0]['requested_model'], config['workspace']['model'])
            self.assertEqual(tasks[0]['model_source'], 'workspace')

    def test_unavailable_detected_model_falls_back_to_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '1.txt'
            path.write_text('The camera glides past the product with soft studio light and shows every detail. ' * 4, encoding='utf-8')
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['workspace']['model'] = 'MiniMax-H3'
            config['_model_catalog'] = {'MiniMax-H3': {
                'id': 'MiniMax-H3', 'name': 'MiniMax-H3', 'kind': 'video', 'family': 'MiniMax-H3',
                'available': True, 'protocol_known': True, 'ratios': ['9:16'], 'resolutions': ['1080p'],
                'durations': list(range(4, 16)), 'audio': False, 'seed': False, 'max_images': 9,
            }}
            tasks = [dict(prompt_path=str(path), images=[])]
            annotate_tasks(tasks, config)
            self.assertEqual(tasks[0]['detected_model'], 'grok-imagine-1.5-video')
            self.assertEqual(tasks[0]['requested_model'], 'MiniMax-H3')
            self.assertEqual(tasks[0]['model_source'], 'fallback')
            self.assertIn('grok-imagine-1.5-video', tasks[0]['model_fallback_reason'])

    def test_unrecognized_uses_configured_fallback_or_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'1.txt'; path.write_text('subject_definitions: single', encoding='utf-8')
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['prompt_detection'] = dict(enabled=True, fallback_model='video-v2')
            tasks = [dict(prompt_path=str(path), images=[])]
            annotate_tasks(tasks, config)
            self.assertEqual(tasks[0]['requested_model'], 'video-v2')
            self.assertEqual(tasks[0]['model_source'], 'fallback')
            config['prompt_detection']['fallback_model'] = ''
            annotate_tasks(tasks, config)
            self.assertEqual(tasks[0]['requested_model'], config['workspace']['model'])
