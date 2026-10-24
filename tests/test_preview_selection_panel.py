"""选择栏在现有倍率下创建与运行中切换倍率的独立回归。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtWidgets import QApplication

from tests.qt_scale_support import flush_layout, reset_interface_scaling
from ui.components.preview_selection_panel import PreviewSelectionPanel
from ui.scaling import apply_interface_scale
from ui.tokens import px


class PreviewSelectionScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        reset_interface_scaling(self.app)

    def _verify_scale(self, scale, *, scale_first):
        reset_interface_scaling(self.app)
        panel = None
        try:
            if scale_first:
                apply_interface_scale(self.app, self.app.primaryScreen(), str(scale))
            panel = PreviewSelectionPanel(
                ['missing-carousel-1.png', 'missing-carousel-2.png', 'missing-carousel-3.png'],
                ['missing-detail-1.png', 'missing-detail-2.png'])
            panel.resize(px(260), px(700))
            panel.show()
            flush_layout(self.app, panel)
            if not scale_first:
                apply_interface_scale(self.app, self.app.primaryScreen(), str(scale))
                panel.resize(px(260), px(700))
                flush_layout(self.app, panel)
            self.assertEqual(panel.title_labels['carousel'].font().pixelSize(), px(13))
            self.assertEqual(panel.clear_buttons['carousel'].font().pixelSize(), px(11))
            self.assertEqual(panel.clear_buttons['carousel'].height(), px(26))
            self.assertEqual(panel.hint_label.font().pixelSize(), px(11))
            margins = panel._content_layout.contentsMargins()
            self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()),
                             (px(16), px(16), px(16), px(8)))
            grid = panel.groups['carousel']['grid']
            self.assertEqual((grid.horizontalSpacing(), grid.verticalSpacing()), (px(8), px(8)))
            self.assertLessEqual(panel._content.width(), panel.scroll_area.viewport().width())
            self.assertEqual(panel.scroll_area.horizontalScrollBar().maximum(), 0)
            first_geometry = tuple(tile.geometry().getRect() for tile in panel.tiles['carousel'])
            flush_layout(self.app, panel)
            self.assertEqual(first_geometry,
                             tuple(tile.geometry().getRect() for tile in panel.tiles['carousel']))
            for tile in panel.tiles['carousel']:
                self.assertEqual(tile.badge_label.font().pixelSize(), px(10))
                self.assertEqual(tile.width(), tile.height())
                self.assertLessEqual(tile.width(), px(72))
                self.assertGreater(tile.width(), 0)
            tiles = panel.tiles['carousel']
            self.assertEqual(tiles[1].x() - tiles[0].geometry().right() - 1, px(8))
            panel.set_columns(2)
            panel.resize(px(232), px(700))
            flush_layout(self.app, panel)
            self.assertEqual(panel.columns, 2)
            self.assertLessEqual(panel._content.width(), panel.scroll_area.viewport().width())
            for tile in panel.tiles['carousel']:
                self.assertLessEqual(tile.width(), px(72))
                self.assertGreaterEqual(tile.x(), 0)
                self.assertLessEqual(tile.geometry().right(), tile.parentWidget().width() - 1)
        finally:
            if panel is not None:
                panel.close()
                panel.deleteLater()
            reset_interface_scaling(self.app)

    def test_panel_created_after_scaling_uses_unscaled_baselines(self):
        for scale in (1.0, 1.25, 1.5, 1.75):
            with self.subTest(scale=scale):
                self._verify_scale(scale, scale_first=True)

    def test_existing_panel_scales_fonts_spacing_and_tile_geometry_once(self):
        for scale in (1.0, 1.25, 1.5, 1.75):
            with self.subTest(scale=scale):
                self._verify_scale(scale, scale_first=False)


if __name__ == '__main__':
    unittest.main()
