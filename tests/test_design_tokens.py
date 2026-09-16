"""设计 token 护栏：度量常量健全性 + ui/ 颜色字面量上限（防回潮）。"""
import re
import unittest
from pathlib import Path

from ui import tokens


class DesignTokenTests(unittest.TestCase):
    def test_token_scales_are_sane(self):
        self.assertEqual(sorted(tokens.SPACE.values()), [4, 8, 12, 16, 24])
        self.assertEqual(tokens.RADIUS['control'], 8)
        self.assertEqual(tokens.RADIUS['container'], 12)
        self.assertEqual(tokens.NAV_COLLAPSED_WIDTH, 64)
        self.assertEqual(tokens.NAV_EXPAND_WIDTH, 240)
        for value in tokens.MOTION.values():
            self.assertGreater(value, 0)
        for value in tokens.FONT.values():
            self.assertGreaterEqual(value, 10)

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
