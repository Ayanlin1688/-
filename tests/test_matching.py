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
            self.assertEqual([Path(v).name for v in result[0]['images']], ['01汽车.jpg', '01汽车_2.png'])
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

    def test_prefix_respects_number_boundary_and_exact_keeps_series_views(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [root / name for name in ['产品10(1).jpg', '产品1(10).jpg', '产品1(2).jpg', '1(1).jpg']]
            matcher = StoryboardMatcher()
            task = matcher.match_files([root / '产品1.txt'], images)[0]
            self.assertEqual([Path(p).name for p in task['images']], ['1(1).jpg', '产品1(2).jpg', '产品1(10).jpg'])
            task = matcher.match_files([root / '产品1.txt'], images + [root / '产品1.jpg', root / '产品1.png'])[0]
        self.assertEqual([Path(p).name for p in task['images']],
                         ['1(1).jpg', '产品1(2).jpg', '产品1(10).jpg', '产品1.jpg', '产品1.png'])

    def test_prefix_matches_when_image_stem_is_shorter_than_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = StoryboardMatcher().match_files(
                [root / '产品1-夜景.txt'],
                [root / '产品1.jpg', root / '产品10.jpg'],
            )[0]
            self.assertEqual([Path(path).name for path in task['images']], ['产品1.jpg'])

    def test_number_matching_accepts_numeric_group_inside_image_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StoryboardMatcher().match_files(
                [root / '玫瑰毯子2.txt'],
                [root / '图片2.jpg', root / '图片20.jpg', root / '01（2）.jpg'],
            )
            self.assertEqual([Path(path).name for path in tasks[0]['images']], ['图片2.jpg'])

    def test_exact_numbered_base_also_collects_its_numbered_views(self):
        root = Path('fixture')
        task = StoryboardMatcher().match_files([root / '01.txt'],
            [root / name for name in ['01.jpg', '01(2).png', '01(1).jpg', '010.jpg', '图片1.jpg']])[0]
        self.assertEqual(set(Path(p).name for p in task['images']), {'01.jpg', '01(1).jpg', '01(2).png', '图片1.jpg'})

    def test_prompt_number_can_be_inside_filename_without_using_row_position(self):
        root = Path('fixture')
        task = StoryboardMatcher().match_files([root / '玫瑰毯子02场景.txt'], [root / '图片2.jpg'])[0]
        self.assertEqual([Path(p).name for p in task['images']], ['图片2.jpg'])

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

    def test_chinese_short_reference_markers_normalize_and_warn(self):
        self.assertEqual(process_prompt('@图1 @图10 @图片2'), '@参考图1 @参考图10 @参考图2')
        self.assertEqual(reference_warnings('@图5', 3),
                         ['提示词引用了Picture 5，但只绑定了3张图，可能影响生成质量'])

    def test_safe_filename_and_placeholders(self):
        task = {'prompt_name': '../汽车:夜景', 'model': 'video-v3', 'task_id': 'abc'}
        name = build_filename('{序号}_{提示词名}_{模型}_{task_id}.mp4', task, 3)
        self.assertTrue(name.startswith('003_'))
        self.assertNotIn('/', name)
        self.assertNotIn(':', name)
        self.assertTrue(name.endswith('_video-v3_abc.mp4'))


    def test_spec_prefix_series_and_embedded_number_examples(self):
        """需求示例：前缃匹配/序号匹配（含“图片2.jpg”这种内嵌序号）。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # 前缃：玫瑰毯子1 ↔ 玫瑰毯子1(1..3)
            tasks = StoryboardMatcher().match_files(
                [root / '玫瑰毯子1.txt'],
                [root / name for name in ['玫瑰毯子1(1).jpg', '玫瑰毯子1(2).jpg', '玫瑰毯子1(3).jpg']])
            self.assertEqual([Path(p).name for p in tasks[0]['images']],
                             ['玫瑰毯子1(1).jpg', '玫瑰毯子1(2).jpg', '玫瑰毯子1(3).jpg'])
            self.assertIn('前缀匹配', tasks[0]['match_method'])
            # 前缃：01 ↔ 01(1).jpg、01(2).png（按自然排序）
            tasks = StoryboardMatcher().match_files([root / '01.txt'],
                                                    [root / n for n in ['01(1).jpg', '01(2).png']])
            self.assertEqual([Path(p).name for p in tasks[0]['images']], ['01(1).jpg', '01(2).png'])
            # 序号：玫瑰毯子1 ↔ 1(1..3)
            tasks = StoryboardMatcher().match_files(
                [root / '玫瑰毯子1.txt'],
                [root / name for name in ['1(1).jpg', '1(2).jpg', '1(3).jpg']])
            self.assertEqual([Path(p).name for p in tasks[0]['images']], ['1(1).jpg', '1(2).jpg', '1(3).jpg'])
            self.assertIn('序号匹配', tasks[0]['match_method'])
            # 序号：02 ↔ 2.jpg、2(1).png、图片2.jpg
            tasks = StoryboardMatcher().match_files(
                [root / '02.txt'],
                [root / name for name in ['2.jpg', '2(1).png', '图片2.jpg']])
            self.assertEqual(sorted(Path(p).name for p in tasks[0]['images']),
                             sorted(['2.jpg', '2(1).png', '图片2.jpg']))
            self.assertIn('序号匹配', tasks[0]['match_method'])

    def test_five_prompts_all_match_with_mixed_naming_schemes(self):
        """回归：5 个提示词在混合命名下必须全部匹配到图片。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = [root / f'玫瑰毯子{i}.txt' for i in range(1, 6)]
            images = [root / name for name in
                      ['玫瑰毯子1(1).jpg', '玫瑰毯子1(2).jpg', '2(1).png', '3.jpg', '图片4.png', '5(1).webp']]
            tasks = StoryboardMatcher().match_files(prompts, images)
            self.assertTrue(all(task['matched'] for task in tasks))
            self.assertEqual([Path(p).name for p in tasks[0]['images']], ['玫瑰毯子1(1).jpg', '玫瑰毯子1(2).jpg'])
            self.assertEqual([Path(p).name for p in tasks[1]['images']], ['2(1).png'])
            self.assertEqual([Path(p).name for p in tasks[2]['images']], ['3.jpg'])
            self.assertEqual([Path(p).name for p in tasks[3]['images']], ['图片4.png'])
            self.assertEqual([Path(p).name for p in tasks[4]['images']], ['5(1).webp'])

    def test_reverse_prefix_and_series_boundary_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # 图片名是提示词名的前缀：玫瑰毯子1 ↔ 玫瑰毯子1特写
            tasks = StoryboardMatcher().match_files([root / '玫瑰毯子1特写.txt'], [root / '玫瑰毯子1.jpg'])
            self.assertEqual([Path(p).name for p in tasks[0]['images']], ['玫瑰毯子1.jpg'])
            # 系列保护：玫瑰毯子10 不得吸附玫瑰毯子1.jpg
            tasks = StoryboardMatcher().match_files([root / '玫瑰毯子10.txt'], [root / '玫瑰毯子1.jpg'])
            self.assertFalse(tasks[0]['matched'])


if __name__ == '__main__':
    unittest.main()
