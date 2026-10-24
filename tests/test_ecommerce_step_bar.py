"""电商图三步条的阶段、缩放、主题与语言总线回归。"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtWidgets import QApplication

from core.i18n import set_language, tr
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from ui import materials, tokens
from ui.components.ecommerce_step_bar import EcommerceStepBar


class EcommerceStepBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        set_language('zh-CN')
        self.addCleanup(set_language, 'zh-CN')
        self.addCleanup(reset_interface_scaling, self.app)
        self.bar = EcommerceStepBar()
        self.bar.resize(600, 48)
        self.bar.show()
        flush_layout(self.app, self.bar)
        self.addCleanup(self.dispose_bar)

    def dispose_bar(self):
        self.bar.close()
        self.bar.deleteLater()
        self.app.processEvents()

    def test_stages_update_circle_line_and_label_states(self):
        expected = {
            'idle': ('pending', 'pending', 'pending'),
            'analyzing': ('active', 'pending', 'pending'),
            'generating': ('done', 'active', 'pending'),
            'finished': ('done', 'done', 'done'),
            'failed': ('done', 'done', 'failed'),
        }
        for stage, states in expected.items():
            with self.subTest(stage=stage):
                self.bar.set_stage(stage)
                self.app.processEvents()
                self.assertEqual(self.bar.stage, stage)
                self.assertEqual(tuple(c.property('stepState') for c in self.bar.circles), states)
                self.assertEqual(tuple(label.property('stepState') for label in self.bar.labels), states)
                self.assertEqual(tuple(line.property('lineState') for line in self.bar.lines),
                                 tuple('done' if state == 'done' else 'pending' for state in states[:2]))
        self.assertEqual(self.bar.labels[2].text(), '部分失败')

    def test_finished_connectors_use_green_done_theme_token(self):
        self.bar.set_stage('finished')
        for line in self.bar.lines:
            self.assertIn(materials.ecommerce_color('step_done'), line.styleSheet())
        self.bar.set_stage('idle')
        for line in self.bar.lines:
            self.assertIn(materials.ecommerce_color('step_line'), line.styleSheet())

    def test_same_stage_skips_styles_while_language_and_theme_refresh_remain_available(self):
        self.bar.set_stage('generating')
        label = self.bar.labels[0]
        with patch.object(label, 'setStyleSheet', wraps=label.setStyleSheet) as restyle:
            for _ in range(30):
                self.bar.set_stage('generating')
            self.assertEqual(restyle.call_count, 0)
            self.bar.refresh_colors()
            self.assertEqual(restyle.call_count, 1)
        set_language('en-US')
        self.assertEqual(label.text(), tr('AI 分析'))
        self.assertNotEqual(label.text(), 'AI 分析')

    def test_supported_scales_resize_circles_height_and_fonts_idempotently(self):
        for scale in (1.0, 1.25, 1.5, 1.75):
            with self.subTest(scale=scale):
                tokens.set_interface_scale(scale)
                self.bar.resize(tokens.px(600), tokens.px(48))
                self.bar.refresh_interface_scale()
                flush_layout(self.app, self.bar)
                for _ in range(2):
                    self.bar.refresh_interface_scale()
                self.assertEqual(self.bar.height(), tokens.px(48))
                self.assertEqual(self.bar.circles[0].width(), tokens.px(22))
                self.assertEqual(self.bar.labels[0].font().pixelSize(), tokens.px(12))
                for line in self.bar.lines:
                    self.assertEqual(line.height(), tokens.px(2))
                for previous, following in zip(self.bar.labels, self.bar.circles[1:]):
                    previous_right = previous.mapTo(self.bar, previous.rect().topRight()).x()
                    following_left = following.mapTo(self.bar, following.rect().topLeft()).x()
                    self.assertLess(previous_right, following_left)

    def test_language_bus_retranslates_without_rebuilding_step_widgets(self):
        circles = tuple(id(circle) for circle in self.bar.circles)
        set_language('en-US')
        self.app.processEvents()
        self.assertEqual([label.text() for label in self.bar.labels],
                         [tr('AI 分析'), tr('生成图片'), tr('完成')])
        self.assertNotEqual(self.bar.labels[0].text(), 'AI 分析')
        self.assertEqual(tuple(id(circle) for circle in self.bar.circles), circles)
        self.bar.set_stage('failed')
        self.assertEqual(self.bar.labels[2].text(), tr('部分失败'))


if __name__ == '__main__':
    unittest.main()
