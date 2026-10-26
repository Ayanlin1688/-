"""Offline contracts for bundled ecommerce rules and analysis prompts."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.ecommerce import (
    LANGUAGE_TEXT, MAX_PLATFORMS, RATIOS, all_platforms, append_reference_order,
    build_reference_order, build_system, build_user, detect_conflicts, domestic,
    get_platform, overseas,
)
from core.ecommerce import platforms as platform_rules


class PlatformRulesTests(unittest.TestCase):
    def test_all_25_platforms_have_complete_fields_and_valid_defaults(self):
        rows = all_platforms()
        self.assertEqual(len(rows), 25)
        self.assertEqual(len({row['id'] for row in rows}), 25)
        fields = {
            'id', 'name', 'group', 'bgRule', 'toneDesc', 'rules', 'primaryColor',
            'accentColor', 'defaultCarouselRatio', 'defaultDetailRatio',
            'defaultCarouselCount', 'defaultDetailCount', 'previewComponent',
        }
        for row in rows:
            with self.subTest(platform=row['id']):
                self.assertTrue(fields <= row.keys())
                self.assertTrue(all(row[field] for field in fields))
                self.assertEqual(set(row['rules']), {'carousel', 'detail'})
                self.assertTrue(all(row['rules'].values()))
                self.assertIn(row['group'], ('domestic', 'overseas'))
                self.assertIn(row['bgRule'], ('white', 'no-white', 'free'))
                for field in ('defaultCarouselRatio', 'defaultDetailRatio'):
                    self.assertIn(row[field], RATIOS)
                for field in ('defaultCarouselCount', 'defaultDetailCount'):
                    self.assertIs(type(row[field]), int)
                    self.assertGreater(row[field], 0)

    def test_domestic_and_overseas_partition_catalog_without_losing_order(self):
        self.assertEqual(len(domestic()), 10)
        self.assertEqual(len(overseas()), 15)
        rows = all_platforms()
        self.assertEqual(domestic(), [row for row in rows if row['group'] == 'domestic'])
        self.assertEqual(overseas(), [row for row in rows if row['group'] == 'overseas'])
        self.assertEqual(get_platform('amazon')['name'], '亚马逊')

    def test_callers_cannot_mutate_bundled_rules(self):
        record = get_platform('amazon')
        original = record['rules']['carousel']
        record['rules']['carousel'] = 'changed'
        rows = all_platforms()
        rows[0]['rules']['detail'] = 'changed'
        self.assertEqual(get_platform('amazon')['rules']['carousel'], original)
        self.assertNotEqual(all_platforms()[0]['rules']['detail'], 'changed')

    def test_resource_loading_uses_collected_package_in_frozen_layout(self):
        # Simulate a collected package in _MEIPASS, with no access to source data.
        fixture = get_platform('amazon')
        fixture['name'] = '冻结资源测试'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package_dir = root / 'assets'
            package_dir.mkdir()
            init = package_dir / '__init__.py'
            init.write_text('', encoding='utf-8')
            (package_dir / 'ecommerce_platforms.json').write_text(
                json.dumps([fixture], ensure_ascii=False), encoding='utf-8')
            spec = importlib.util.spec_from_file_location(
                '_ecommerce_assets_fixture', init,
                submodule_search_locations=[str(package_dir)])
            package = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(package)
            platform_rules._catalog.cache_clear()
            try:
                with patch.object(platform_rules, 'platform_assets', package), \
                        patch('sys.frozen', True, create=True), \
                        patch('sys._MEIPASS', str(root), create=True):
                    self.assertEqual(all_platforms(), [fixture])
            finally:
                platform_rules._catalog.cache_clear()
        self.assertEqual(len(all_platforms()), 25)

    def test_selection_limit_unknown_ids_and_duplicate_ids(self):
        ids = [row['id'] for row in all_platforms()]
        self.assertEqual(MAX_PLATFORMS, 5)
        detect_conflicts(ids[:5])
        with self.assertRaises(ValueError):
            detect_conflicts(ids[:6])
        with self.assertRaises(ValueError):
            detect_conflicts(['amazon', 'amazon'])
        with self.assertRaises(KeyError):
            get_platform('missing-platform')
        with self.assertRaises(KeyError):
            detect_conflicts(['missing-platform'])
        self.assertEqual(detect_conflicts([]), [])

    def test_background_conflict_lists_both_sides_and_ignores_free_platforms(self):
        conflicts = detect_conflicts(['amazon', 'taobao', 'xiaohongshu', 'etsy', 'douyin'])
        conflict = next(row for row in conflicts if row['type'] == 'background')
        self.assertEqual(conflict['names'], ['亚马逊', '某宝', '小某书', '手工集市'])
        self.assertIn('要求白底的平台：亚马逊、某宝', conflict['tip'])
        self.assertIn('禁止白底的平台：小某书、手工集市', conflict['tip'])
        self.assertIn('分两批', conflict['tip'])

    def test_free_background_does_not_create_background_conflict(self):
        for ids in (['amazon', 'douyin'], ['xiaohongshu', 'douyin'], ['amazon'], ['etsy']):
            with self.subTest(ids=ids):
                self.assertNotIn('background', [row['type'] for row in detect_conflicts(ids)])

    def test_matching_default_ratios_have_no_conflicts(self):
        # The supplied JSON has 3:4 carousel and 9:16 detail for both platforms.
        self.assertEqual(detect_conflicts(['douyin', 'tiktok']), [])

    def test_different_ratios_report_carousel_and_detail_independently(self):
        conflicts = detect_conflicts(['amazon', 'douyin'])
        self.assertEqual([row['type'] for row in conflicts], ['ratio_carousel', 'ratio_detail'])
        for row in conflicts:
            self.assertEqual(row['names'], ['亚马逊', '抖店'])
            self.assertIn('统一为 1:1', row['tip'])
            self.assertIn('分两批', row['tip'])
        self.assertEqual([row['type'] for row in detect_conflicts(['amazon', 'taobao'])],
                         ['ratio_detail'])
        self.assertEqual([row['type'] for row in detect_conflicts(['taobao', 'xiaohongshu'])],
                         ['background', 'ratio_carousel'])


class PromptBuilderTests(unittest.TestCase):
    def test_single_amazon_system_has_reference_template_and_exact_counts(self):
        system = build_system([get_platform('amazon')], 'zh', 3, 4, '1:1', '3:4')
        self.assertTrue(system.startswith('你是专业电商视觉策划师，精通国内外主流电商平台的视觉规范和审美标准。\n'))
        self.assertIn(
            '【亚马逊】调性：Amazon 全球电商平台，风格简洁专业，重点展示产品功能和规格参数\n'
            '  轮播图规范：【严格规定】第1张主图必须纯白底(#FFFFFF)，产品占图片85%以上，'
            '绝对禁止任何文字/水印/装饰/道具；第2-7张可用信息图、场景图、细节图\n'
            '  详情图规范：信息图（infographic）风格，白/浅灰背景，专业简洁', system)
        self.assertIn('图片文字语言要求：中文', system)
        self.assertIn('- 必须严格生成 轮播图 3 张（宽高比 1:1） + 详情图 4 张（宽高比 3:4），'
                      'JSON 数组长度必须恰好为 7 项，不多不少', system)
        self.assertIn('prompt 必须包含 "pure white background #FFFFFF, studio lighting"', system)
        self.assertIn('  - type: "carousel"（轮播图）或 "detail"（详情图）', system)
        self.assertIn('carousel 和 detail 各自独立编号', system)
        self.assertIn('详细的英文图片生成 prompt', system)
        self.assertIn('该图片的中文说明（一句话概括用途和内容）', system)
        self.assertTrue(system.endswith('直接输出 JSON 数组，不要有任何前缀或后缀文字。'))

    def test_multiple_platform_blocks_have_one_blank_line_between_them(self):
        system = build_system(['douyin', 'tiktok'], 'en', 2, 3, '3:4', '9:16')
        first, second = get_platform('douyin'), get_platform('tiktok')
        self.assertIn(f'  详情图规范：{first["rules"]["detail"]}\n\n'
                      f'【{second["name"]}】调性：{second["toneDesc"]}', system)
        self.assertIn('每个序号各一张，不是每个平台各一套', system)

    def test_mixed_background_requirements_stay_scoped_to_their_platforms(self):
        self.assertIn('background', [row['type'] for row in
                                    detect_conflicts(['amazon', 'xiaohongshu'])])
        system = build_system(['amazon', 'xiaohongshu'], 'zh', 3, 4, '1:1', '3:4')
        background = next(line for line in system.splitlines() if line.startswith('- 背景要求：'))
        positive, negative, conflict = background.split('；')
        self.assertIn('仅适用于：亚马逊', positive)
        self.assertIn('pure white background #FFFFFF, studio lighting', positive)
        self.assertNotIn('小某书', positive)
        self.assertIn('仅适用于：小某书', negative)
        self.assertIn('prompt 中不得出现 white background', negative)
        self.assertNotIn('必须包含', negative)
        self.assertIn('分两批生成', conflict)

    def test_no_white_and_free_backgrounds_do_not_get_positive_white_keywords(self):
        for ids in (['xiaohongshu'], ['etsy', 'shein'], ['douyin'], ['xiaohongshu', 'douyin']):
            with self.subTest(ids=ids):
                system = build_system(ids, 'en', 1, 1, '3:4', '3:4')
                self.assertNotIn('pure white background', system)
                if 'douyin' not in ids or len(ids) > 1:
                    self.assertIn('不得出现 white background', system)

    def test_all_language_mappings_and_ratio_choices(self):
        expected = {
            'none': '图片中不包含任何文字', 'zh': '中文', 'en': '英文', 'ja': '日文',
            'ko': '韩文', 'ar': '阿拉伯语', 'fr': '法语', 'de': '德语', 'es': '西班牙语',
            'pt': '葡萄牙语', 'ru': '俄语', 'th': '泰语', 'vi': '越南语', 'id': '印尼语',
            'ms': '马来语', 'tr': '土耳其语', 'it': '意大利语', 'nl': '荷兰语', 'pl': '波兰语',
            'multi': '图片文字同时包含中英文',
        }
        self.assertEqual(LANGUAGE_TEXT, expected)
        self.assertEqual(RATIOS, ['1:1', '5:4', '9:16', '16:9', '3:2', '4:3', '4:5', '3:4', '2:3'])
        for language, text in expected.items():
            with self.subTest(language=language):
                self.assertIn(f'图片文字语言要求：{text}\n',
                              build_system(['amazon'], language, 1, 1, '1:1', '1:1'))

    def test_empty_product_name_omits_optional_line(self):
        expected = ('已选平台（共 2 个）：抖店、国际抖店\n产品信息：\n保温杯，容量 500ml\n\n'
                    '请严格按配置生成 7 条电商图提示词（轮播图 3 张（宽高比 1:1） + '
                    '详情图 4 张（宽高比 3:4））。')
        for name in ('', '  ', None):
            with self.subTest(name=name):
                user = build_user(name, '保温杯，容量 500ml', ['抖店', '国际抖店'], 3, 4, '1:1', '3:4')
                self.assertEqual(user, expected)
                self.assertNotIn('产品名称：', user)

    def test_product_name_has_blank_lines_and_product_info_is_preserved(self):
        self.assertEqual(
            build_user('林牌保温杯', '不锈钢\n500ml', ['亚马逊'], 1, 2, '1:1', '3:4'),
            '已选平台（共 1 个）：亚马逊\n\n产品名称：林牌保温杯\n\n'
            '产品信息：\n不锈钢\n500ml\n\n'
            '请严格按配置生成 3 条电商图提示词（轮播图 1 张（宽高比 1:1） + '
            '详情图 2 张（宽高比 3:4））。')

    def test_zero_count_for_one_image_type_keeps_the_configured_total(self):
        for d, g in ((0, 4), (3, 0)):
            with self.subTest(d=d, g=g):
                self.assertIn(f'JSON 数组长度必须恰好为 {d + g} 项',
                              build_system(['amazon'], 'none', d, g, '1:1', '3:4'))
                self.assertIn(f'生成 {d + g} 条电商图提示词',
                              build_user('', '', ['亚马逊'], d, g, '1:1', '3:4'))

    def test_invalid_counts_ratios_language_and_selection_fail_before_generation(self):
        for d, g in ((-1, 2), (0, 0), (1.5, 2), (True, 1)):
            with self.subTest(d=d, g=g):
                with self.assertRaises(ValueError):
                    build_system(['amazon'], 'zh', d, g, '1:1', '1:1')
                with self.assertRaises(ValueError):
                    build_user('', '', ['亚马逊'], d, g, '1:1', '1:1')
        for cr, dr in (('7:1', '1:1'), ('1:1', '7:1')):
            with self.subTest(cr=cr, dr=dr):
                with self.assertRaises(ValueError):
                    build_system(['amazon'], 'zh', 1, 1, cr, dr)
        with self.assertRaises(ValueError):
            build_system(['amazon'], 'missing-language', 1, 1, '1:1', '1:1')
        for ids in ([], [row['id'] for row in all_platforms()[:6]], 'amazon'):
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    build_system(ids, 'zh', 1, 1, '1:1', '1:1')
        for names in ([], ['a'] * 6, '亚马逊'):
            with self.subTest(names=names):
                with self.assertRaises(ValueError):
                    build_user('', '', names, 1, 1, '1:1', '1:1')

    def test_reference_order_exactly_preserves_english_contract(self):
        expected = (
            'Reference image order (critical): Images 1-6 are product photos — preserve the product '
            'appearance, shape, colors, materials and branding from these images. Image 7 (the last '
            'image) is style reference only — match its color palette, lighting, mood, composition '
            'and visual atmosphere; do not copy objects, products, logos or text from the style '
            'reference image.'
        )
        self.assertEqual(build_reference_order(6), expected)
        for prompt in ('A product on a table.', 'A close-up of the product.'):
            self.assertEqual(append_reference_order(prompt, 6, has_style_reference=True),
                             prompt + '\n\n' + expected)

    def test_product_only_references_preserve_product_identity_and_text(self):
        prompt = 'A product on a table.'
        suffix = build_reference_order(2, has_style_reference=False)
        self.assertIn('Images 1-2 are product photos', suffix)
        self.assertIn('appearance, shape, colors, materials, branding and text', suffix)
        self.assertTrue(suffix.endswith('Images 1-2 are all product references.'))
        self.assertNotIn('style reference', suffix)
        self.assertEqual(append_reference_order(prompt, 2), prompt + '\n\n' + suffix)

    def test_style_only_reference_excludes_objects_logos_and_text(self):
        prompt = 'A product on a table.'
        suffix = build_reference_order(0, has_style_reference=True)
        self.assertIn('Image 1 (the only reference image) is style reference only', suffix)
        self.assertIn('color palette, lighting, mood, composition', suffix)
        self.assertIn('do not copy objects, products, logos or text', suffix)
        self.assertEqual(append_reference_order(prompt, 0, True), prompt + '\n\n' + suffix)

    def test_without_reference_photos_prompt_is_unchanged_and_counts_are_validated(self):
        prompt = 'A product on a table.'
        self.assertEqual(append_reference_order(prompt, 0), prompt)
        self.assertEqual(append_reference_order(prompt, 0, has_style_reference=False), prompt)
        self.assertEqual(build_reference_order(0), '')
        for count in (-1, 1.5, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                build_reference_order(count)

    def test_style_counts_zero_to_three_and_legacy_bool_contract(self):
        for count in range(4):
            with self.subTest(count=count):
                text = build_reference_order(6, style_count=count)
                self.assertIn('Images 1-6', text)
                if count:
                    self.assertIn(f'最后 {count} 张为风格参考图', text)
                else:
                    self.assertNotIn('style reference', text)
                self.assertEqual(append_reference_order('prompt', 6, style_count=count),
                                 'prompt\n\n' + text)
        self.assertEqual(build_reference_order(6), build_reference_order(6, True))
        for count in (-1, 4, True, 1.5):
            with self.subTest(invalid=count), self.assertRaises(ValueError):
                build_reference_order(6, style_count=count)


if __name__ == '__main__':
    unittest.main()
