"""Primary navigation geometry across interface scales and languages."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QPoint
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from core import i18n
from core.config_manager import ConfigManager
from ui import materials
from ui.tokens import px
from ui.theme import apply_ui_mode
from tests.qt_scale_support import flush_layout, reset_interface_scaling


class NavigationLayoutTests(unittest.TestCase):
    ROUTES = (
        'workspacePage',
        'imageGenerationPage',
        'videoCreationPage',
        'ecommercePage',
        'historyPage',
        'settingsPage',
    )
    ZH_LABELS = ('工作台', '图片生成', '视频制作', '电商图制作', '任务历史', '设置')
    EN_LABELS = ('Workspace', 'Image generation', 'Video creation',
                 'Ecommerce Image Maker', 'History', 'Settings')

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        i18n.set_language('简体中文')
        self.temp = tempfile.TemporaryDirectory()
        manager = ConfigManager(Path(self.temp.name) / 'config.json')
        manager.load_config()
        from ui.main_window import MainWindow
        self.window = MainWindow(manager, network_time=False, model_sync=False)
        self.manager = manager
        self.window.resize(1000, 900)
        self.window.show()
        QTest.qWait(250)
        flush_layout(self.app, self.window)

    def tearDown(self):
        i18n.set_language('简体中文')
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(80)
        reset_interface_scaling(self.app)
        self.temp.cleanup()

    def _set_scale(self, scale):
        self.manager.config.setdefault('appearance', {})['ui_scale'] = scale
        self.window._apply_interface_scale()
        QTest.qWait(180)
        self.window.resize(1000, 900)
        flush_layout(self.app, self.window)

    def _set_collapsed(self, collapsed):
        panel = self.window.navigationInterface.panel
        if panel.isCollapsed() != collapsed:
            self.window._toggle_navigation()
            QTest.qWait(450)
        self.window._sync_nav_pane(collapsed)
        flush_layout(self.app, self.window)
        self.assertEqual(panel.isCollapsed(), collapsed)

    def _assert_geometry(self, labels):
        nav = self.window.navigationInterface
        items = [nav.widget(route) for route in self.ROUTES]
        self.assertEqual([item.text() for item in items], list(labels))

        rects = [item.geometry() for item in items]
        self.assertEqual(len({rect.height() for rect in rects}), 1)
        self.assertEqual(len({rect.x() for rect in rects}), 1)
        gaps = [right.y() - left.y() for left, right in zip(rects, rects[1:])]
        self.assertEqual(gaps[:4], [px(44) + px(4)] * 4)
        self.assertEqual(gaps[4], px(44) + px(16) + px(1))
        panel = nav.panel
        for item in items:
            inner = item.itemWidget
            self.assertEqual(item.height(), px(44))
            self.assertEqual(inner.size(), item.size())
            self.assertEqual(inner.font().pixelSize(), px(14))
            self.assertEqual(item.hitRect(), item.rect())
            self.assertEqual(inner.hitRect(), inner.rect())
            self.assertEqual(inner.iconRect().width(), px(16))
            self.assertEqual(inner.iconRect().height(), px(16))
            left = item.mapTo(panel, QPoint()).x()
            self.assertEqual(left, px(8))
            self.assertEqual(panel.width() - left - item.width(), px(8))
            if panel.isCollapsed():
                self.assertAlmostEqual(inner.iconRect().center().x(), inner.width() / 2)
                self.assertAlmostEqual(inner.iconRect().center().y(), inner.height() / 2)
            else:
                self.assertEqual(inner.iconRect().left(), px(12))
                self.assertEqual(inner.textRect().left() - inner.iconRect().right(), px(38) - px(28))
                self.assertGreaterEqual(inner.textRect().width(), inner.fontMetrics().horizontalAdvance(inner.text()))
        self.assertEqual(self.window._nav_group_line.height(), px(1))
        settings_bottom = items[-1].mapTo(panel, QPoint(0, items[-1].height())).y()
        self.assertGreaterEqual(self.window.user_avatar.mapTo(panel, QPoint()).y() - settings_bottom, px(12))

        nav_right = nav.mapTo(self.window, QPoint(nav.width(), 0)).x()
        page_left = self.window.stackedWidget.mapTo(self.window, QPoint(0, 0)).x()
        self.assertGreaterEqual(page_left, nav_right,
                                f'page starts at {page_left}, navigation ends at {nav_right}')
        return items

    def test_six_primary_items_align_at_all_scales_in_both_modes(self):
        for scale in ('1.0', '1.25', '1.5', '1.75'):
            with self.subTest(scale=scale):
                self._set_scale(scale)
                for collapsed in (True, False):
                    self._set_collapsed(collapsed)
                    items = self._assert_geometry(self.ZH_LABELS)
                    if collapsed:
                        centers = [item.mapTo(self.window, QPoint(item.width() // 2, 0)).x()
                                   for item in items]
                        self.assertLessEqual(max(centers) - min(centers), 1, centers)
                        self.assertTrue(all(item.toolTip().strip() for item in items),
                                        [item.toolTip() for item in items])

    def test_english_navigation_keeps_geometry_and_text(self):
        self._set_scale('1.0')
        i18n.set_language('English')
        QTest.qWait(80)
        flush_layout(self.app, self.window)
        for collapsed in (True, False):
            with self.subTest(collapsed=collapsed):
                self._set_collapsed(collapsed)
                self._assert_geometry(self.EN_LABELS)

    def test_separator_tracks_theme_in_both_modes_at_all_scales(self):
        for scale in ('1.0', '1.25', '1.5', '1.75'):
            self._set_scale(scale)
            for mode in ('light', 'dark'):
                apply_ui_mode(mode)
                for collapsed in (True, False):
                    with self.subTest(scale=scale, theme=mode, collapsed=collapsed):
                        self._set_collapsed(collapsed)
                        self._assert_geometry(self.ZH_LABELS)
                        line = self.window._nav_group_line
                        self.assertTrue(line.isVisible())
                        self.assertIn(materials.rgba(materials.palette()['border']), line.styleSheet())

    def test_selection_is_immediate_and_filled_in_both_modes(self):
        for collapsed in (True, False):
            self._set_collapsed(collapsed)
            for route in self.ROUTES:
                page = next(self.window.stackedWidget.widget(i)
                            for i in range(self.window.stackedWidget.count())
                            if self.window.stackedWidget.widget(i).objectName() == route)
                self.window.switchTo(page)
                item = self.window.navigationInterface.widget(route)
                inner = getattr(item, 'itemWidget', item)
                self.assertTrue(inner.isSelected, (collapsed, route))
                image = inner.grab().toImage()
                color = image.pixelColor(image.width() // 2, 6).name().upper()
                self.assertEqual(color, materials.ecommerce_color('navigation_selected').upper())

    def test_ecommerce_uses_a_shopping_bag_outline(self):
        from ui.components.navigation_icons import ShoppingBagIcon
        item = self.window.navigationInterface.widget('ecommercePage')
        inner = getattr(item, 'itemWidget', item)
        self.assertIsInstance(inner._icon, ShoppingBagIcon)
        self.assertFalse(inner._icon.icon().isNull())


if __name__ == '__main__':
    unittest.main()
