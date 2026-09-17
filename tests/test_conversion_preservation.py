import unittest

from core.prompt_converter import convert_for_model, h3_to_v2, v2_to_h3
from test_prompt_converter import H3_PROMPT, V2_TAIL


class ConversionPreservationTests(unittest.TestCase):
    def test_h3_keeps_constraints_from_all_six_sections(self):
        source = H3_PROMPT.replace('Metadata that must not become a shot.',
                                 'No subtitles, no watermark. Never change the red roses.')
        source = source.replace('<Subject 1>: fully_preserved',
                                '<Subject 1>: fully_preserved; no shape distortion')
        source = source.replace('non_diegetic_music:\nN/A', 'non_diegetic_music:\nQuiet piano at low volume.')
        result = convert_for_model(source, 'video-v2')
        for content in ('No subtitles, no watermark.', 'Never change the red roses.',
                        'no shape distortion', 'Quiet piano at low volume.',
                        'Light street ambience and a bicycle bell.', 'Ready to ride.'):
            self.assertIn(content, result.text)
        self.assertEqual(result.shot_count, 2)
        self.assertNotIn('无背景音乐', result.text)

    def test_v2_keeps_custom_negatives_and_voice_description(self):
        source = ('【分镜】\n人物站位：A red rose blanket.\n镜头1：Show @图1.\n'
                  '【旁白声线】 Warm female American voice.\nKeep a slow pace.\n'
                  '环境音：Soft fabric rustle.\n' + V2_TAIL.replace('文字/UI/水印/Logo/角标/可读文字/真实UI',
                    'No subtitles, no watermark; never alter the red roses.'))
        result = convert_for_model(source, 'MiniMax-H3')
        for content in ('No subtitles, no watermark; never alter the red roses.',
                        'Warm female American voice.', 'Keep a slow pace.', 'Soft fabric rustle.'):
            self.assertIn(content, result.text)
        self.assertEqual(result.shot_count, 1)
        for field in ('subject_definitions:', 'summary:', 'retention_analysis:',
                      'detailed_description:', 'overall_soundscape:', 'non_diegetic_music:'):
            self.assertEqual(result.text.count(field), 1)

    def test_h3_constraints_survive_round_trip(self):
        source = H3_PROMPT.replace('Metadata that must not become a shot.', 'No watermark; keep all red petals.')
        converted = v2_to_h3(h3_to_v2(source))
        self.assertIn('No watermark; keep all red petals.', converted)

