"""Qt-only startup placement, geometry persistence and live-zoom regressions."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QPoint, QRect, QSize
from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from ui.scaling import apply_interface_scale
from ui.tokens import px
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from tests.test_interface_scaling import FakeScreen


class WindowGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        # offscreen 的默认 800×600 屏幕不足最低布局尺寸；正常放置用明确工作区。
        self.available = QRect(0, 0, 1920, 1040)
        work_area = patch('PyQt5.QtGui.QScreen.availableGeometry', return_value=self.available)
        work_area.start()
        self.addCleanup(work_area.stop)
        if sys.platform == 'win32':
            native = patch('qframelesswindow.windows.win32gui.SetWindowPos')
            native.start()
            self.addCleanup(native.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.manager = ConfigManager(Path(self.temporary.name) / 'config.json')
        self.manager.load_config()
        self.manager.update(('appearance', 'ui_scale'), '1.0')
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def create_window(self, saved=None):
        self.manager.update(('appearance', 'window_geometry'), saved)
        window = MainWindow(self.manager, network_time=False, model_sync=False)
        self.windows.append(window)
        return window

    def test_first_start_is_centered_before_the_first_show(self):
        window = self.create_window()
        available = self.app.primaryScreen().availableGeometry()
        self.assertFalse(window.isVisible())
        self.assertLessEqual((window.geometry().center() - available.center()).manhattanLength(), 8)
        self.assertTrue(available.contains(window.geometry()))

    def test_normal_geometry_saves_and_restores_exactly(self):
        window = self.create_window()
        available = self.app.primaryScreen().availableGeometry()
        target = QRect(available.left() + 12, available.top() + 12, 1000, 650)
        target = window._bounded_geometry(target, available, window.minimumSize())
        window.setGeometry(target)
        window._save_window_geometry()
        saved = self.manager.config['appearance']['window_geometry']
        self.assertTrue(all(type(value) is int for value in saved.values()))
        rebuilt = self.create_window(saved)
        self.assertEqual(rebuilt.geometry(), target)

    def test_unplugged_screen_coordinates_fall_back_to_primary_center(self):
        window = self.create_window(dict(x=-20000, y=4000, w=1200, h=800, maximized=0))
        available = self.app.primaryScreen().availableGeometry()
        self.assertLessEqual((window.geometry().center() - available.center()).manhattanLength(), 8)

    def test_small_saved_size_is_raised_to_the_supported_minimum(self):
        available = self.app.primaryScreen().availableGeometry()
        for width, height in ((400, 300), (100, 100), (1, 1)):
            with self.subTest(size=(width, height)):
                window = self.create_window(dict(x=available.left(), y=available.top(), w=width, h=height, maximized=0))
                self.assertEqual(window.width(), min(960, available.width()))
                self.assertEqual(window.height(), min(600, available.height()))

    def test_tiny_work_area_preserves_minimum_size_and_centers_overflow(self):
        available = QRect(-800, 40, 800, 500)
        with patch('PyQt5.QtGui.QScreen.availableGeometry', return_value=available):
            window = self.create_window()
            self.assertEqual(window.minimumSize(), QSize(960, 600))
            self.assertEqual(window.size(), QSize(960, 600))
            self.assertLessEqual((window.geometry().center() - available.center()).manhattanLength(), 2)
            window.show()
            self.app.processEvents()
            window.on_interface_scale_committed(1.0, 1.75)
            self.assertEqual(window.minimumSize(), QSize(960, 600))
            self.assertEqual(window.size(), QSize(960, 600))
            self.assertLessEqual((window.geometry().center() - available.center()).manhattanLength(), 2)

    def test_invalid_saved_geometry_has_a_safe_centered_fallback(self):
        for saved in (dict(x='bad', y=0, w=1000, h=650), dict(x=0), []):
            with self.subTest(saved=saved):
                window = self.create_window(saved)
                available = self.app.primaryScreen().availableGeometry()
                self.assertLessEqual((window.geometry().center() - available.center()).manhattanLength(), 8)

    def test_maximized_flag_restores_and_saves_normal_geometry(self):
        available = self.app.primaryScreen().availableGeometry()
        window = self.create_window(dict(x=available.left(), y=available.top(), w=1000, h=650, maximized=1))
        normal = window.geometry()
        window.show()
        self.app.processEvents()
        self.assertTrue(window.isMaximized())
        window._save_window_geometry()
        saved = self.manager.config['appearance']['window_geometry']
        self.assertEqual(saved['maximized'], 1)
        self.assertEqual(QRect(saved['x'], saved['y'], saved['w'], saved['h']), normal)

    def test_minimized_window_does_not_overwrite_last_normal_geometry(self):
        window = self.create_window()
        window.show()
        self.app.processEvents()
        window._save_window_geometry()
        saved = dict(self.manager.config['appearance']['window_geometry'])
        window.showMinimized()
        window._save_window_geometry()
        self.assertEqual(self.manager.config['appearance']['window_geometry'], saved)

    def test_move_resize_persistence_is_debounced_and_close_flushes_it(self):
        window = self.create_window()
        window.show()
        self.app.processEvents()
        with patch.object(self.manager, 'update', wraps=self.manager.update) as update:
            window.move(window.x() + 10, window.y())
            window.move(window.x() + 10, window.y())
            self.assertTrue(window._geometry_save.timer.isActive())
            self.assertEqual(window._geometry_save.timer.interval(), 500)
            self.assertFalse(any(call.args[0] == ('appearance', 'window_geometry') for call in update.call_args_list))
            window.close()
            self.assertTrue(any(call.args[0] == ('appearance', 'window_geometry') for call in update.call_args_list))
            self.assertFalse(window._geometry_save.timer.isActive())

    def test_zoom_keeps_center_and_scales_size_before_updates_resume(self):
        window = self.create_window()
        window.show()
        self.app.processEvents()
        screen = FakeScreen(96, 3840, 2160, 2080)
        with patch.object(window, 'screen', return_value=screen):
            window.setGeometry(1300, 700, 1000, 650)
            before = window.geometry()
            apply_interface_scale(self.app, screen, '1.5')
            after = window.geometry()
            self.assertLessEqual(abs(after.width() - before.width() * 1.5), 4)
            self.assertLessEqual(abs(after.height() - before.height() * 1.5), 4)
            self.assertLessEqual((after.center() - before.center()).manhattanLength(), 4)
            self.assertTrue(window.updatesEnabled())

    def test_zoom_sequence_stays_inside_the_current_work_area(self):
        window = self.create_window()
        window.show()
        self.app.processEvents()
        screen = FakeScreen(96, 1920, 1080, 1040)
        screen.available = QRect(-1920, 40, 1920, 1040)
        with patch.object(window, 'screen', return_value=screen):
            window.setGeometry(-1800, 100, 1000, 650)
            for scale in ('1.0', '1.25', '1.3', '1.5', '1.75'):
                with self.subTest(scale=scale):
                    apply_interface_scale(self.app, screen, scale)
                    self.assertTrue(screen.available.contains(window.geometry()), window.geometry())
                    self.assertTrue(window.updatesEnabled())

    def test_avatar_is_owned_by_bottom_layout_and_remains_anchored(self):
        window = self.create_window()
        window.show()
        self.app.processEvents()
        panel = window.navigationInterface.panel
        avatar = window.user_avatar
        self.assertGreaterEqual(panel.bottomLayout.indexOf(avatar), 0)
        for scale in ('1.0', '1.25', '1.3', '1.5', '1.75'):
            with self.subTest(scale=scale):
                apply_interface_scale(self.app, self.app.primaryScreen(), scale)
                flush_layout(self.app, window)
                self.assertEqual(panel.height(), window.navigationInterface.height())
                bounds = QRect(avatar.mapTo(panel, QPoint()), avatar.size())
                visible = QRect(0, 0, px(30), px(30))
                visible.moveCenter(bounds.center())
                self.assertTrue(panel.rect().contains(visible), (panel.rect(), bounds, visible,
                                window.geometry(), window.navigationInterface.geometry(), panel.parent().geometry(),
                                panel.isVisible(), panel.displayMode))
                self.assertEqual(bounds.left(), panel.bottomLayout.geometry().left()
                                 + panel.bottomLayout.contentsMargins().left())
                gap = panel.height() - bounds.bottom() - 1
                self.assertEqual(gap, panel.height() - panel.vBoxLayout.geometry().bottom() - 1
                                 + panel.vBoxLayout.contentsMargins().bottom()
                                 + panel.bottomLayout.contentsMargins().bottom())
                previous_gap = gap
                window.resize(window.width(), max(window.minimumHeight(), window.height() - 80))
                flush_layout(self.app, window)
                bounds = QRect(avatar.mapTo(panel, QPoint()), avatar.size())
                self.assertEqual(panel.height() - bounds.bottom() - 1, previous_gap)


if __name__ == '__main__':
    unittest.main()
