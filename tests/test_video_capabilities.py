"""Verified video limits and conservative future-model fallback."""
from pathlib import Path
import tempfile
import unittest

from core.video_capabilities import capabilities, output_size, validate_media_file


class VideoCapabilitiesTests(unittest.TestCase):
    def test_verified_family_limits_and_defaults(self):
        for model, durations, default, limits, presets in (
            ('MiniMax-H3', tuple(range(4, 16)), 5, (9, 3, 3), (5, 10, 15)),
            ('video-v2', (5, 10, 15), 5, (9, 3, 3), (5, 10, 15)),
            ('video-v3', tuple(range(4, 31)), 30, (30, 10, 10), (5, 10, 15, 30)),
        ):
            with self.subTest(model=model):
                result = capabilities(model)
                self.assertEqual(result['durations'], durations)
                self.assertEqual(result['default_duration'], default)
                self.assertEqual(result['quick_presets'], presets)
                self.assertEqual(tuple(result['max_' + k] for k in ('images', 'videos', 'audios')), limits)
                self.assertEqual(result['source'], 'upstream')

    def test_variant_resolutions_and_future_seedance_alias(self):
        for model, resolutions in (
            ('MiniMax-H3', ('768p', '1080p', '2K', '4K')),
            ('video-v2', ('480p', '720p', '1080p')),
            ('video-v2-480P', ('480p',)), ('video-v2-fast', ('720p',)),
            ('video-v2-特价', ('720p',)), ('video-v2-720P', ('720p',)),
            ('video-v3-480P', ('480p',)), ('video-v3-fast', ('720p',)),
            ('seedance-2.5', ('720p',)),
        ):
            with self.subTest(model=model):
                self.assertEqual(capabilities(model)['resolutions'], resolutions)

    def test_catalog_family_is_authoritative_and_false_flags_are_respected(self):
        record = {'family': 'video-v3', 'audio': False, 'seed': False}
        result = capabilities('custom-provider-video', {'custom-provider-video': record})
        self.assertEqual(result['default_duration'], 30)
        self.assertFalse(result['supports_audio'])
        self.assertFalse(result['supports_seed'])

    def test_unknown_openai_video_uses_logged_fallback(self):
        with self.assertLogs('core.video_capabilities', level='WARNING') as logs:
            result = capabilities('future-video', {'future-video': {
                'supported_endpoint_types': ['openai-video']}})
        self.assertEqual(result['source'], 'fallback')
        self.assertEqual(result['durations'], tuple(range(4, 16)))
        self.assertEqual(result['resolutions'], ('720p',))
        self.assertEqual((result['max_images'], result['max_videos'], result['max_audios']), (9, 3, 3))
        self.assertIn('未识别的视频模型，使用保守能力', logs.output[0])

    def test_output_dimensions_and_h3_exact_sizes(self):
        self.assertEqual(output_size('9:16', '480p'), '480x854')
        self.assertEqual(output_size('16:9', '480p'), '854x480')
        self.assertEqual(output_size('9:16', '720p'), '720x1280')
        self.assertEqual(output_size('16:9', '720p'), '1280x720')
        self.assertEqual(output_size('1:1', '720p'), '720x720')
        for res, short in (('2K', 2560), ('4K', 3840)):
            for ratio in ('16:9', '9:16', '1:1'):
                width, height = map(int, output_size(ratio, res).split('x'))
                rw, rh = map(int, ratio.split(':'))
                self.assertEqual(min(width, height), short)
                self.assertAlmostEqual(width / height, rw / rh, delta=.001)
        self.assertEqual(capabilities('MiniMax-H3')['output_sizes']['9:16'],
                         ('768x1376', '1088x1920', '2K', '4K'))

    def test_media_files_validate_format_presence_and_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix, kind in (('.mp4', 'videos'), ('.webm', 'videos'), ('.mov', 'videos'),
                                 ('.mp3', 'audios'), ('.wav', 'audios'), ('.m4a', 'audios'),
                                 ('.aac', 'audios'), ('.ogg', 'audios')):
                path = root / ('中文素材' + suffix)
                path.write_bytes(b'fixture')
                self.assertEqual(validate_media_file(path, kind), str(path))
            with self.assertRaisesRegex(ValueError, '不存在'):
                validate_media_file(root / 'missing.mp4', 'videos')
            empty = root / 'empty.mp4'; empty.touch()
            with self.assertRaisesRegex(ValueError, '为空'):
                validate_media_file(empty, 'videos')
            with self.assertRaisesRegex(ValueError, '格式不支持'):
                validate_media_file(root / '中文素材.mp3', 'videos')
            for kind, suffix, limit in (('videos', '.mp4', 200), ('audios', '.wav', 50)):
                path = root / ('oversize' + suffix)
                with path.open('wb') as stream:
                    stream.truncate(limit * 1024 * 1024)
                self.assertEqual(validate_media_file(path, kind), str(path))
                with path.open('ab') as stream:
                    stream.write(b'x')
                with self.assertRaisesRegex(ValueError, str(limit) + 'MB'):
                    validate_media_file(path, kind)
