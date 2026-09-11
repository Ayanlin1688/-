import tempfile
import unittest
from pathlib import Path

from core.matcher import StoryboardMatcher
from core.prompt_processor import process_prompt, reference_warnings
from core.video_downloader import build_filename


class MatchingTests(unittest.TestCase):
    def test_priority_order_overrides_and_unicode_recursive_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = root / '提示词'; prompts.mkdir()
            images = root / '图片'; images.mkdir()
            (images / '子目录').mkdir()
            for name in ['01汽车.txt', '02车轮.txt', '03道路.txt', '04空白.txt']:
                (prompts / name).write_text('文本', encoding='utf-8')
            for name in ['01汽车.jpg', '01汽车_2.png', '02车轮_1.webp', '02车轮_2.jpg']:
                (images / name).touch()
            (images / '子目录' / '额外.png').touch()
            matcher = StoryboardMatcher(recursive=False)
            p, i = matcher.scan_directories(prompts, images)
            self.assertEqual((len(p), len(i)), (4, 4))
            result = matcher.match_files(p, i)
            self.assertEqual([Path(v).name for v in result[0]['images']], ['01汽车.jpg'])
            self.assertEqual([Path(v).name for v in result[1]['images']], ['02车轮_1.webp', '02车轮_2.jpg'])
            # An unrelated positional image must no longer masquerade as a match.
            self.assertEqual(result[2]['images'], [])
            matcher.overrides = {p[0]: [], p[1]: [i[3], i[2]]}
            overridden = matcher.match_files(p, i)
            self.assertFalse(overridden[0]['matched'])
            self.assertEqual(overridden[1]['images'], [i[3], i[2]])
            self.assertEqual(len(StoryboardMatcher().scan_directories(prompts, images)[1]), 5)

    def test_numbered_series_collects_all_views_in_natural_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ['10(1).jpg', '1(10).jpg', '1 (2).jpg', '2(1).jpg', '1(1).jpg']
            tasks = StoryboardMatcher().match_files([root / '玫瑰毯子1.txt', root / '玫瑰毯子10.txt'],
                                                    [root / name for name in names])
            self.assertEqual([task['prompt_name'] for task in tasks], ['玫瑰毯子1', '玫瑰毯子10'])
            self.assertEqual([Path(p).name for p in tasks[0]['images']], ['1(1).jpg', '1 (2).jpg', '1(10).jpg'])
            self.assertEqual([Path(p).name for p in tasks[1]['images']], ['10(1).jpg'])
            self.assertIn('序号匹配', tasks[0]['match_method'])

    def test_prefix_respects_number_boundary_and_exact_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [root / name for name in ['产品10(1).jpg', '产品1(10).jpg', '产品1(2).jpg', '1(1).jpg']]
            matcher = StoryboardMatcher()
            task = matcher.match_files([root / '产品1.txt'], images)[0]
            self.assertEqual([Path(p).name for p in task['images']], ['产品1(2).jpg', '产品1(10).jpg'])
            task = matcher.match_files([root / '产品1.txt'], images + [root / '产品1.jpg', root / '产品1.png'])[0]
            self.assertEqual([Path(p).name for p in task['images']], ['产品1.jpg', '产品1.png'])

    def test_manual_order_and_explicit_empty_override_take_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); prompt = str(root / '玫瑰毯子1.txt')
            paths = [str(root / name) for name in ['1(1).jpg', '1(2).jpg', '1(3).jpg']]
            matcher = StoryboardMatcher(overrides={prompt: [paths[2], paths[0], paths[1]]})
            self.assertEqual(matcher.match_files([prompt], paths)[0]['images'], [paths[2], paths[0], paths[1]])
            matcher.overrides[prompt] = []
            self.assertEqual(matcher.match_files([prompt], paths)[0]['images'], [])

    def test_number_matching_handles_leading_zero_and_never_picks_unrelated_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StoryboardMatcher().match_files([root / '01毯子.txt', root / '毯子2.txt'],
                                                    [root / '01（2）.JPG', root / '01 (1).jpg', root / '无关图片.jpg'])
            self.assertEqual([Path(p).name for p in tasks[0]['images']], ['01 (1).jpg', '01（2）.JPG'])
            self.assertFalse(tasks[1]['matched'])

    def test_reference_shortage_warns_with_max_index_even_when_few_markers(self):
        self.assertEqual(reference_warnings('<Picture 1> @Image5 <Picture 5 >', 3),
                         ['提示词引用了Picture 5，但只绑定了3张图，可能影响生成质量'])
        self.assertEqual(reference_warnings('<Picture 1> @Image3', 3), [])

    def test_prompt_normalization_preserves_other_text(self):
        self.assertEqual(process_prompt(' \n<Picture 1> <Picture2> <图片3> @Image4 @图片5\n原文  保留 '),
                         '@参考图1 @参考图2 @参考图3 @参考图4 @参考图5\n原文  保留')

    def test_safe_filename_and_placeholders(self):
        task = {'prompt_name': '../汽车:夜景', 'model': 'video-v3', 'task_id': 'abc'}
        name = build_filename('{序号}_{提示词名}_{模型}_{task_id}.mp4', task, 3)
        self.assertTrue(name.startswith('003_'))
        self.assertNotIn('/', name)
        self.assertNotIn(':', name)
        self.assertTrue(name.endswith('_video-v3_abc.mp4'))


if __name__ == '__main__':
    unittest.main()
