"""设计 token 护栏：度量常量健全性 + ui/ 颜色字面量上限（防回潮）。"""
import os
import re
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path

from ui import tokens


def _luminance(color):
    color = color.lstrip('#')
    channels = [int(color[index:index + 2], 16) / 255.0 for index in (0, 2, 4)]
    channels = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(foreground, background):
    first, second = _luminance(foreground), _luminance(background)
    hi, lo = max(first, second), min(first, second)
    return (hi + 0.05) / (lo + 0.05)


class DesignTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_token_scales_are_sane(self):
        self.assertEqual(sorted(tokens.SPACE.values()), [4, 8, 12, 16, 24])
        self.assertEqual(tokens.RADIUS['control'], 8)
        self.assertEqual(tokens.RADIUS['container'], 12)
        self.assertEqual((tokens.ICON['sm'], tokens.ICON['md'], tokens.ICON['lg']), (16, 20, 24))
        self.assertEqual(tokens.NAV_COLLAPSED_WIDTH, 64)
        self.assertEqual(tokens.NAV_EXPAND_WIDTH, 240)
        for value in tokens.MOTION.values():
            self.assertGreater(value, 0)
        for value in tokens.FONT.values():
            self.assertGreaterEqual(value, 10)

    def test_theme_text_contrast_meets_baselines(self):
        # 亮 / 暗全部主题：正文 ≥ 7、次要 ≥ 4.5、弱化 ≥ 3.5（对两端背景取最差值）。
        from ui.palettes import THEMES
        for theme_id, theme in THEMES.items():
            for key, minimum in (('text1', 7.0), ('text2', 4.5), ('text3', 3.5)):
                for background in ('bg1', 'bg2'):
                    ratio = _contrast(theme[key], theme[background])
                    self.assertGreaterEqual(
                        ratio, minimum,
                        f'{theme_id}: {key} vs {background} 对比度 {ratio:.2f} < {minimum}')

    def test_reduced_motion_flag_controls_looping_animations(self):
        from ui.motion import StatusDot, reduced_motion, set_reduced_motion
        try:
            set_reduced_motion(False)
            dot = StatusDot('#3b82f6', active=True)
            self.assertTrue(dot.can_animate())
            set_reduced_motion(True)
            self.assertTrue(reduced_motion())
            self.assertFalse(dot.can_animate())
            set_reduced_motion(False)
            self.assertFalse(reduced_motion())
            self.assertTrue(dot.can_animate())
            dot.deleteLater()
        finally:
            set_reduced_motion(False)

    def test_ui_color_literals_stay_below_ceiling(self):
        # 颜色以 palettes / materials / theme 为单一来源；其余 ui 文件的字面量
        # 需保持收敛（2026-09-16 基线 134 处，上限 160 防回潮）。
        root = Path(__file__).resolve().parent.parent / 'ui'
        allowed = {'palettes.py', 'materials.py', 'theme.py'}
        count = 0
        offenders = []
        for path in root.rglob('*.py'):
            if path.name in allowed:
                continue
            text = path.read_text(encoding='utf-8')
            literals = re.findall(r'#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b', text)
            count += len(literals)
            if len(literals) > 30:
                offenders.append((str(path.relative_to(root)), len(literals)))
        self.assertLessEqual(count, 160, f'ui/ 颜色字面量回升：{count} 处（上限 160）')
        self.assertEqual(offenders, [], f'单文件颜色字面量过多：{offenders}')


if __name__ == '__main__':
    unittest.main()
