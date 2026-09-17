"""Regression coverage for mixed series names and a selected product folder."""
import tempfile
import unittest
from pathlib import Path

from core.matcher import StoryboardMatcher, natural_path_key


class MatchingClosureTests(unittest.TestCase):
    def test_missing_selected_product_never_binds_other_product(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = root / 'prompts' / 'rose'; prompts.mkdir(parents=True)
            sibling = prompts.parent / 'fan'; sibling.mkdir()
            (prompts / '1.txt').write_text('Rose product', encoding='utf-8')
            (sibling / '1.txt').write_text('Fan product', encoding='utf-8')
            images = root / 'images' / 'fan'; images.mkdir(parents=True)
            (images / '1.jpg').touch()
            task = StoryboardMatcher().scan_and_match(dict(prompts=str(prompts), images=str(images.parent)))[0]
            self.assertFalse(task['matched'])
            self.assertEqual(task['product'], 'rose')
            self.assertIn('缺少', task['skip_reason'])

    def test_missing_product_images_respect_pause_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = root / 'prompts' / 'rose'; prompts.mkdir(parents=True)
            images = root / 'images'; images.mkdir()
            (prompts / '1.txt').write_text('Rose product', encoding='utf-8')
            matcher = StoryboardMatcher(unmatched_policy='暂停任务')
            task = matcher.scan_and_match(dict(prompts=str(prompts.parent), images=str(images)))[0]
            self.assertEqual(task['skip_reason'], '')
            self.assertTrue(any('暂停' in warning for warning in matcher.warnings))

    def test_five_rose_prompts_bind_ten_images(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = root / 'prompts' / '玫瑰毯子'; prompts.mkdir(parents=True)
            images = root / 'images' / '玫瑰毯子'; images.mkdir(parents=True)
            for n in range(1, 6):
                (prompts / f'玫瑰毯子{n}.txt').write_text(f'Rose prompt {n}', encoding='utf-8')
            # The requested example enumerated nine files; add 3(1) as the tenth.
            names = ['1(1).jpg', '1(2).jpg', '1(3).jpg', '2(1).jpg', '2(2).jpg',
                     '3.jpg', '3(1).jpg', '4(1).png', '5(1).jpg', '5(2).jpg']
            for name in names:
                (images / name).touch()
            tasks = StoryboardMatcher().scan_and_match(dict(prompts=str(prompts.parent), images=str(images.parent)))
            self.assertEqual([len(t['images']) for t in tasks], [3, 2, 2, 1, 2])
            self.assertTrue(all(t['matched'] for t in tasks))

    def test_exact_and_prefix_hits_do_not_hide_same_number_views(self):
        root = Path('fixture')
        wanted = ['1.jpg', '1(1).jpg', '1(2).jpg', '1(3).png', '图片1.jpg',
                  '01.jpg', '玫瑰毯子1.jpg', '玫瑰毯子1_正面.jpg']
        task = StoryboardMatcher().match_files([root / '玫瑰毯子1.txt'],
            [root / name for name in wanted + ['10.jpg', '11(1).png']])[0]
        self.assertEqual([Path(p).name for p in task['images']],
                         sorted(wanted, key=natural_path_key))
        self.assertEqual(task['image_match_methods'][str((root / '玫瑰毯子1.jpg').resolve())], '完全匹配')
        self.assertIn('序号匹配', task['image_match_methods'][str((root / '01.jpg').resolve())])

    def test_zero_padded_exact_does_not_hide_unpadded_series(self):
        root = Path('fixture')
        wanted = ['2.jpg', '2(1).jpg', '02.png', '图片2.jpg']
        task = StoryboardMatcher().match_files([root / '02.txt'],
            [root / name for name in wanted + ['20.jpg', '21.jpg']])[0]
        self.assertEqual(set(Path(p).name for p in task['images']), set(wanted))

    def test_unnumbered_prompt_does_not_use_its_row_number(self):
        root = Path('fixture')
        self.assertFalse(StoryboardMatcher().match_files([root / '无序号.txt'], [root / '1.jpg'])[0]['matched'])

    def test_view_parentheses_do_not_replace_the_prompt_series(self):
        root = Path('fixture')
        task = StoryboardMatcher().match_files([root / '玫瑰毯子2(3).txt'],
            [root / '2(1).jpg', root / '3.jpg'])[0]
        self.assertEqual([Path(p).name for p in task['images']], ['2(1).jpg'])

    def test_selected_product_prompt_folder_uses_same_named_image_child_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = root / 'prompts' / '玫瑰毯子'; prompts.mkdir(parents=True)
            images = root / 'images'
            for product in ('玫瑰毯子', '其他产品'):
                (images / product).mkdir(parents=True)
                (images / product / '1.jpg').touch()
            (prompts / '玫瑰毯子1.txt').write_text('prompt', encoding='utf-8')
            matcher = StoryboardMatcher()
            task = matcher.scan_and_match(dict(prompts=str(prompts), images=str(images)))[0]
            self.assertEqual(task['images'], [str((images / '玫瑰毯子' / '1.jpg').resolve())])
            self.assertEqual(task['product'], '玫瑰毯子')
            self.assertEqual(task['output_subdir'], '玫瑰毯子')
            self.assertEqual(matcher.products, ['玫瑰毯子'])

    def test_both_selected_product_folders_retain_product_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = root / 'prompts' / '玫瑰毯子'; prompts.mkdir(parents=True)
            images = root / 'images' / '玫瑰毯子'; images.mkdir(parents=True)
            (prompts / '1.txt').write_text('prompt', encoding='utf-8')
            (images / '1.jpg').touch()
            task = StoryboardMatcher().scan_and_match(dict(prompts=str(prompts), images=str(images)))[0]
            self.assertEqual(task['product'], '玫瑰毯子')
            self.assertEqual(task['output_subdir'], '玫瑰毯子')


if __name__ == '__main__':
    unittest.main()
