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
    ZH_LABELS = ('工作台', '图片生成', '视频制作', '电商图', '任务历史', '设置')
    EN_LABELS = ('Workspace', 'Image generation', 'Video creation',
                 'Ecommerce Images', 'History', 'Settings')

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
        self.window = MainWindow(manager, network_time=False)
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
        self.assertTrue(all(abs(gap - gaps[0]) <= 1 for gap in gaps), gaps)

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


if __name__ == '__main__':
    unittest.main()
