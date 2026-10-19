import copy
import unittest

from core.config_manager import DEFAULT_CONFIG
from core.line_routing import CAPABILITIES, distinct_lines, normalize_base_url, resolve_line
from core.task_state import task_signature


class BaseUrlTests(unittest.TestCase):
    def test_normalizes_host_roots_and_preserves_custom_paths(self):
        cases = (
            ('https://api.kkone.vip/', 'https://api.kkone.vip/v1'),
            ('https://api.kkone.vip/v1', 'https://api.kkone.vip/v1'),
            ('https://api.deepseek.com', 'https://api.deepseek.com/v1'),
            ('https://api.deepseek.com/', 'https://api.deepseek.com/v1'),
            ('https://host/custom', 'https://host/custom'),
            ('', ''),
            ('  HTTPS://API.KKONE.VIP/  ', 'https://API.KKONE.VIP/v1'),
            ('https://HOST/custom///', 'https://HOST/custom'),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(normalize_base_url(value), expected)

    def test_default_video_signature_is_unchanged(self):
        from core.line_routing import resolve_line

        config = copy.deepcopy(DEFAULT_CONFIG)
        old_base_url = config['api']['base_url']
        video_line = resolve_line(config, 'video')
        task = dict(prompt_path='prompt.txt', images=[], _original_prompt='固定提示词')
        params = dict(duration=8, aspect_ratio='16:9', resolution='720p', generate_audio=True)
        self.assertEqual(video_line['base_url'], old_base_url)
        self.assertEqual(
            task_signature(task, 'video-v3', params, video_line['base_url']),
            '637c44fe160c763e0b3524031d3b7d29c7e4bfd485694fc5bc953a4dc2d8f50f')


class LineResolutionTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            'api': {'base_url': 'https://active.example/', 'api_key': 'active-secret'},
            'line_routing': {'video': 'active', 'chat': 'text', 'image': 'image'},
            'stations': [
                {'id': 'text', 'enabled': True, 'base_url': 'https://text.example', 'api_key': 'text-secret'},
                {'id': 'image', 'enabled': True, 'base_url': 'https://image.example/v1/',
                 'api_key': 'image-secret', 'upload_url': 'https://uploads.example'},
                {'id': 'disabled', 'enabled': False, 'base_url': 'https://disabled.example',
                 'api_key': 'disabled-secret'},
            ],
        }

    def test_active_and_station_routes(self):
        active = resolve_line(self.config, 'video')
        self.assertEqual(active['source'], 'active')
        self.assertEqual(active['base_url'], 'https://active.example/v1')
        self.assertEqual(active['api_key'], 'active-secret')
        text = resolve_line(self.config, 'chat')
        self.assertEqual(text['source'], 'text')
        self.assertEqual(text['base_url'], 'https://text.example/v1')
        self.assertEqual((text['upload_url'], text['upload_api_key']), ('', ''))
        image = resolve_line(self.config, 'image')
        self.assertEqual(image['base_url'], 'https://image.example/v1')
        self.assertEqual(image['upload_url'], 'https://uploads.example')
        self.assertEqual(image['upload_api_key'], '')

    def test_deleted_or_disabled_station_falls_back_to_active(self):
        self.config['line_routing']['chat'] = 'removed'
        self.assertEqual(resolve_line(self.config, 'chat')['source'], 'active')
        self.config['line_routing']['chat'] = 'disabled'
        self.assertEqual(resolve_line(self.config, 'chat')['base_url'], 'https://active.example/v1')

    def test_missing_station_fields_are_empty(self):
        self.config['stations'].append({'id': 'minimal', 'enabled': True})
        self.config['line_routing']['image'] = 'minimal'
        line = resolve_line(self.config, 'image')
        self.assertEqual({key: line[key] for key in ('base_url', 'api_key', 'upload_url', 'upload_api_key')},
                         dict(base_url='', api_key='', upload_url='', upload_api_key=''))

    def test_distinct_lines_returns_active_then_unique_selected_routes(self):
        self.config['line_routing']['video'] = 'text'
        self.config['line_routing']['chat'] = 'text'
        lines = distinct_lines(self.config)
        self.assertEqual([line['source'] for line in lines], ['active', 'text', 'image'])
        self.assertEqual(len({(line['base_url'], line['api_key']) for line in lines}), len(lines))
        self.assertEqual(CAPABILITIES, ('video', 'chat', 'image'))

    def test_resolver_does_not_put_secrets_in_failure_text(self):
        secret = self.config['api']['api_key']
        self.config['line_routing'] = {'video': object()}
        try:
            resolve_line(self.config, 'video')
        except Exception as error:
            self.assertNotIn(secret, str(error))
        else:
            self.assertEqual(resolve_line(self.config, 'video')['api_key'], secret)


if __name__ == '__main__':
    unittest.main()
