import re
import unittest

from core.prompt_converter import (
    convert_for_model,
    detect_format,
    h3_to_v2,
    model_format,
    v2_to_h3,
)


H3_PROMPT = """subject_definitions:
<Subject 1> is Maya, standing on the left beside <Picture 1>.
<Subject 2> is the bicycle from <Picture10>, parked on the right.

summary:
[reference generation] Metadata that must not become a shot.

retention_analysis:
<Subject 1>: fully_preserved
<Subject 2>: fully_preserved

detailed_description:
[Shot 1] A wide view frames Maya and @Image1.
She reaches toward the bicycle.
(S1) says in an off-screen voiceover: <d>[English] "Ready to ride."</d>
[Shot 2] At 00:03.000, cut to a close view of <Picture 10>.
Maya says: <d>[English] "Let's go."</d>

overall_soundscape:
Light street ambience and a bicycle bell.

non_diegetic_music:
N/A"""


V2_TAIL = """【禁止项】
文字/UI/水印/Logo/角标/可读文字/真实UI

【强制声明】
无背景音乐,仅保留环境音与人声和音效;画面禁字幕/文字/水印/Logo;禁止可读文字(指画面字幕文字,不含人声台词)"""


class PromptFormatDetectionTests(unittest.TestCase):
    def test_h3_requires_two_signatures_and_takes_precedence(self):
        self.assertEqual(detect_format('subject_definitions:\nonly one marker'), 'text')
        self.assertEqual(
            detect_format('subject_definitions:\n人物站位：左侧\n[Shot 1] action'),
            'H3',
        )

    def test_v2_needs_one_exact_structural_marker(self):
        self.assertEqual(detect_format('开场\n镜头1：人物抬头'), 'V2')
        self.assertEqual(detect_format('A person walks through a quiet room.'), 'text')
        self.assertEqual(detect_format('讨论镜头1的设计，但没有结构化标点'), 'text')


class H3ToV2Tests(unittest.TestCase):
    def test_renders_literal_v2_without_h3_metadata_or_voice_tags(self):
        expected = """【分镜】

人物站位：Maya, standing on the left beside @图1. The bicycle from @图10, parked on the right.

镜头1：A wide view frames Maya and @图1.
She reaches toward the bicycle.
"Ready to ride."

镜头2：A close view of @图10.
Maya says: "Let's go."

环境音：Light street ambience and a bicycle bell.

【禁止项】
文字/UI/水印/Logo/角标/可读文字/真实UI

【强制声明】
无背景音乐,仅保留环境音与人声和音效;画面禁字幕/文字/水印/Logo;禁止可读文字(指画面字幕文字,不含人声台词)"""
        self.assertEqual(h3_to_v2(H3_PROMPT), expected)

    def test_rejects_missing_or_out_of_order_h3_sections(self):
        missing = H3_PROMPT.replace('overall_soundscape:\nLight street ambience and a bicycle bell.\n\n', '')
        reordered = H3_PROMPT.replace(
            'summary:\n[reference generation] Metadata that must not become a shot.\n\nretention_analysis:',
            'retention_analysis:',
        ) + '\n\nsummary:\nlate metadata'
        for prompt in (missing, reordered):
            with self.subTest(prompt=prompt[-40:]):
                with self.assertRaisesRegex(ValueError, 'H3'):
                    h3_to_v2(prompt)

    def test_rejects_duplicate_or_nonsequential_shots(self):
        duplicate = H3_PROMPT.replace('[Shot 2]', '[Shot 1]')
        with self.assertRaisesRegex(ValueError, '镜头'):
            h3_to_v2(duplicate)

    def test_voiceover_marker_without_colon_preserves_dialogue_colon_and_words(self):
        prompt = H3_PROMPT.replace(
            '(S1) says in an off-screen voiceover: <d>[English] "Ready to ride."</d>',
            '(S1) says in an off-screen voiceover <d>[English] "Look: it works."</d>',
        )
        converted = h3_to_v2(prompt)
        self.assertIn('镜头1：A wide view frames Maya and @图1.\nShe reaches toward the bicycle.\n"Look: it works."', converted)
        self.assertNotIn('(S1) says in an off-screen voiceover', converted)
        self.assertNotIn('<d>', converted)


class V2ToH3Tests(unittest.TestCase):
    def test_removes_template_metadata_and_voice_blocks_then_renders_six_fields(self):
        prompt = """该SKILL 由Work-Fisher制作，免费公开，禁止任何盗卖行为

【分镜】

人物站位：Maya在左侧，单车@图10在右侧。

镜头1：Maya看向@图1。
她说：“准备出发。”

镜头2：近景拍摄@图10。
Maya画外音继续：“走吧。”

【Maya声线】 二十多岁女声，中音，语速平稳。

环境音：街道底噪和一声车铃。

""" + V2_TAIL
        expected = """subject_definitions:
<Subject 1> is Maya在左侧，单车<Picture 10>在右侧。

summary:
[reference generation] Maya看向<Picture 1>。 她说：“准备出发。” 近景拍摄<Picture 10>。 Maya画外音继续：“走吧。”

retention_analysis:
<Subject 1>: fully_preserved

detailed_description:
[Shot 1] Maya看向<Picture 1>。
她说：“准备出发。”
[Shot 2] At 00:03.000, 近景拍摄<Picture 10>。
Maya画外音继续：“走吧。”

overall_soundscape:
街道底噪和一声车铃。

non_diegetic_music:
N/A"""
        self.assertEqual(v2_to_h3(prompt), expected)

    def test_generates_monotonic_timestamps_strictly_inside_duration(self):
        prompt = """【分镜】
人物站位：一人在房间中央。
镜头1：动作一。
镜头2：动作二。
镜头3：动作三。
镜头4：动作四。
【禁止项】
文字/UI/水印/Logo/角标/可读文字/真实UI
【强制声明】
无背景音乐,仅保留环境音与人声和音效;画面禁字幕/文字/水印/Logo;禁止可读文字(指画面字幕文字,不含人声台词)"""
        converted = v2_to_h3(prompt, duration=2.0)
        self.assertIn('[Shot 2] At 00:00.500, 动作二。', converted)
        self.assertIn('[Shot 3] At 00:01.000, 动作三。', converted)
        self.assertIn('[Shot 4] At 00:01.500, 动作四。', converted)
        timestamps = [float(minutes) * 60 + float(seconds)
                      for minutes, seconds in re.findall(r'At (\d\d):(\d\d\.\d{3})', converted)]
        self.assertEqual(timestamps, sorted(set(timestamps)))
        self.assertTrue(all(value < 2.0 for value in timestamps))

    def test_missing_sound_becomes_na_and_dialogue_language_is_unchanged(self):
        prompt = """【分镜】
人物站位：Ana stands on the left.
镜头1：Ana says: “No lo traduzcas.”
【禁止项】
文字/UI/水印/Logo/角标/可读文字/真实UI
【强制声明】
无背景音乐,仅保留环境音与人声和音效;画面禁字幕/文字/水印/Logo;禁止可读文字(指画面字幕文字,不含人声台词)"""
        converted = v2_to_h3(prompt)
        self.assertIn('Ana says: “No lo traduzcas.”', converted)
        self.assertIn('overall_soundscape:\nN/A', converted)
        self.assertTrue(converted.endswith('non_diegetic_music:\nN/A'))

    def test_rejects_unknown_content_missing_sections_and_bad_shot_numbers(self):
        unknown = """【分镜】
人物站位：左侧。
未知元数据：不能丢弃
镜头1：动作。
【禁止项】
文字/UI/水印/Logo/角标/可读文字/真实UI
【强制声明】
固定"""
        missing_station = """【分镜】
镜头1：动作。
【禁止项】
固定
【强制声明】
固定"""
        bad_numbers = """【分镜】
人物站位：左侧。
镜头1：动作。
镜头3：动作。
【禁止项】
固定
【强制声明】
固定"""
        for prompt in (unknown, missing_station, bad_numbers):
            with self.subTest(prompt=prompt):
                with self.assertRaises(ValueError):
                    v2_to_h3(prompt)

    def test_discards_every_line_in_multiline_voice_blocks(self):
        prompt = """【分镜】
人物站位：Maya在左侧。
镜头1：Maya说：“保留镜头台词。”
【Maya声线】
二十多岁女声，中音。
常态语速平稳，句尾自然收束。
【旁白声线】 四十岁男声。
低沉，不急促。
环境音：安静室内底噪。
""" + V2_TAIL
        converted = v2_to_h3(prompt)
        self.assertIn('[Shot 1] Maya说：“保留镜头台词。”', converted)
        self.assertIn('overall_soundscape:\n安静室内底噪。', converted)
        for voice_metadata in ('二十多岁女声', '常态语速', '四十岁男声', '低沉，不急促'):
            self.assertNotIn(voice_metadata, converted)

    def test_accepts_documented_sample_without_storyboard_heading(self):
        prompt = """人物站位：Maya在左侧。
镜头1：Maya看向@图1。
镜头2：她拿起产品。
环境音：房间底噪。
""" + V2_TAIL
        converted = v2_to_h3(prompt)
        self.assertIn('<Subject 1> is Maya在左侧。', converted)
        self.assertIn('[Shot 1] Maya看向<Picture 1>。', converted)
        self.assertIn('[Shot 2] At 00:03.000, 她拿起产品。', converted)


class ModelConversionTests(unittest.TestCase):
    def test_model_format_uses_catalog_family_before_model_name(self):
        catalog = {
            'custom-h3': {'family': 'MiniMax-H3'},
            'custom-v2': {'family': 'video-v2'},
            'narrative': {'family': 'grok-imagine-1.5-video'},
        }
        self.assertEqual(model_format('custom-h3', catalog), 'H3')
        self.assertEqual(model_format('custom-v2', catalog), 'V2')
        self.assertEqual(model_format('narrative', catalog), 'text')
        self.assertEqual(model_format('MiniMax-H3'), 'H3')
        self.assertEqual(model_format('video-v3'), 'V2')

    def test_convert_for_model_returns_immutable_deterministic_result(self):
        converted = convert_for_model(H3_PROMPT, 'video-v2', duration=10)
        self.assertEqual(converted.source_format, 'H3')
        self.assertEqual(converted.target_format, 'V2')
        self.assertTrue(converted.converted)
        self.assertEqual(converted.text, h3_to_v2(H3_PROMPT))
        self.assertEqual(converted.warnings, ())

        unchanged = convert_for_model(H3_PROMPT, 'video-v2', enabled=False)
        self.assertEqual(unchanged.text, H3_PROMPT)
        self.assertFalse(unchanged.converted)
        self.assertEqual(unchanged.source_format, 'H3')
        self.assertEqual(unchanged.target_format, 'V2')

    def test_plain_text_and_same_format_are_never_rewritten(self):
        plain = 'A quiet room at sunrise.'
        self.assertEqual(convert_for_model(plain, 'MiniMax-H3').text, plain)
        self.assertFalse(convert_for_model(plain, 'MiniMax-H3').converted)
        same = convert_for_model(H3_PROMPT, 'MiniMax-H3')
        self.assertEqual(same.text, H3_PROMPT)
        self.assertFalse(same.converted)

    def test_known_leading_declaration_is_removed_before_same_format_or_disabled_conversion(self):
        body = """【分镜】
人物站位：Maya在左侧。
镜头1：Maya挥手。
""" + V2_TAIL
        declarations = (
            '该SKILL 由Work-Fisher制作，免费公开，禁止任何盗卖行为',
            '该SKILL由Work-Fisher制作，免费公开，禁止任何盗卖行为',
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                prompt = declaration + '\n\n' + body
                same_format = convert_for_model(prompt, 'video-v2')
                disabled_cross_format = convert_for_model(prompt, 'MiniMax-H3', enabled=False)
                self.assertEqual(same_format.text, body)
                self.assertEqual(same_format.source_format, 'V2')
                self.assertEqual(same_format.target_format, 'V2')
                self.assertFalse(same_format.converted)
                self.assertEqual(disabled_cross_format.text, body)
                self.assertEqual(disabled_cross_format.target_format, 'H3')
                self.assertFalse(disabled_cross_format.converted)

        unrelated_leading_line = '制作说明：保留这一行\n' + body
        untouched = convert_for_model(unrelated_leading_line, 'video-v2', enabled=False)
        self.assertEqual(untouched.text, unrelated_leading_line)


if __name__ == '__main__':
    unittest.main()
