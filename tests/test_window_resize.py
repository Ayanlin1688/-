"""Eight-direction native resize and translucent-frame hit regressions."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QPoint, QSize, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QTableWidget
from qfluentwidgets import FluentWindow

from core.config_manager import ConfigManager
from tests.test_interface_scaling import FakeScreen
from tests.test_task_manager import wait_until
from ui.main_window import MainWindow
from ui.scaling import apply_interface_scale
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from ui.window_resize import resize_edges, resize_hit_test


class ResizeHotZoneTests(unittest.TestCase):
    size = QSize(1280, 800)

    def assert_direction(self, point, edges, hit):
        self.assertEqual(resize_edges(QPoint(*point), self.size), edges)
        self.assertEqual(resize_hit_test(QPoint(*point), self.size), hit)

    def test_left_edge(self):
        self.assert_direction((7, 400), Qt.LeftEdge, 10)

    def test_right_edge(self):
        self.assert_direction((1272, 400), Qt.RightEdge, 11)

    def test_top_edge(self):
        self.assert_direction((640, 7), Qt.TopEdge, 12)

    def test_bottom_edge(self):
        self.assert_direction((640, 792), Qt.BottomEdge, 15)

    def test_top_left_corner_has_sixteen_pixel_hot_zone(self):
        self.assert_direction((15, 15), Qt.TopEdge | Qt.LeftEdge, 13)

    def test_top_right_corner_has_sixteen_pixel_hot_zone(self):
        self.assert_direction((1264, 15), Qt.TopEdge | Qt.RightEdge, 14)

    def test_bottom_left_corner_has_sixteen_pixel_hot_zone(self):
        self.assert_direction((15, 784), Qt.BottomEdge | Qt.LeftEdge, 16)

    def test_bottom_right_corner_has_sixteen_pixel_hot_zone(self):
        self.assert_direction((1264, 784), Qt.BottomEdge | Qt.RightEdge, 17)

    def test_all_outermost_pixels_are_reachable(self):
        for point, edges, hit in (
                ((0, 0), Qt.TopEdge | Qt.LeftEdge, 13),
                ((1279, 0), Qt.TopEdge | Qt.RightEdge, 14),
                ((0, 799), Qt.BottomEdge | Qt.LeftEdge, 16),
                ((1279, 799), Qt.BottomEdge | Qt.RightEdge, 17)):
            with self.subTest(point=point):
                self.assert_direction(point, edges, hit)

    def test_interior_and_boundary_are_not_resize_zones(self):
        for point in ((640, 400), (8, 400), (1271, 400), (640, 8), (640, 791),
                      (16, 16), (1263, 783), (640, 40)):
            with self.subTest(point=point):
                self.assert_direction(point, Qt.Edges(), 0)

    def test_outside_and_empty_sizes_do_not_hit(self):
        for point in ((-1, 100), (100, -1), (1280, 100), (100, 800)):
            with self.subTest(point=point):
                self.assert_direction(point, Qt.Edges(), 0)
        self.assertEqual(resize_hit_test(QPoint(), QSize()), 0)


class WindowResizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        self.temporary = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temporary.name) / 'config.json')
        self.config.config['appearance']['ui_scale'] = '1.0'
        self.config.config['appearance']['reduce_motion'] = True
        self.config.save_config()
        if sys.platform == 'win32':
            native = patch('qframelesswindow.windows.win32gui.SetWindowPos')
            native.start()
            self.addCleanup(native.stop)
        self.window = MainWindow(self.config, network_time=False, model_sync=False)
        self.window.show()
        QTest.qWait(120)

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater()
        QTest.qWait(40)
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        self.temporary.cleanup()

    def test_translucent_outer_frame_is_not_click_through(self):
        image = self.window.grab().toImage()
        for point in (QPoint(1, 1), QPoint(self.window.width() - 2, 1),
                      QPoint(1, self.window.height() - 2),
                      QPoint(self.window.width() - 2, self.window.height() - 2),
                      QPoint(1, self.window.height() // 2)):
            with self.subTest(point=point):
                self.assertGreater(image.pixelColor(point).alpha(), 0)
        self.assertEqual(self.window.windowOpacity(), 1.0)

    def test_child_table_cannot_override_top_level_resize_hit(self):
        table = QTableWidget(1, 1, self.window)
        table.setGeometry(self.window.rect())
        table.show()
        table.raise_()
        QTest.qWait(20)
        point = QPoint(self.window.width() - 12, self.window.height() - 12)
        child = self.window.childAt(point)
        self.assertIsNotNone(child)
        self.assertTrue(child is table or table.isAncestorOf(child))
        self.assertEqual(self.window._resize_hit_test(point), 17)

    def test_minimum_size_remains_960_by_600_at_large_interface_scale(self):
        self.config.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        self.window.resize(800, 400)
        flush_layout(self.app, self.window)
        self.assertEqual(self.window.minimumSize(), QSize(960, 600))
        self.assertEqual(self.window.size(), QSize(960, 600))
        self.assertEqual(self.window._resize_hit_test(QPoint(945, 585)), 17)

    def test_maximized_fullscreen_and_disabled_resize_have_no_hot_zones(self):
        for state in (Qt.WindowMaximized, Qt.WindowFullScreen, Qt.WindowMinimized):
            with self.subTest(state=state), patch.object(self.window, 'windowState', return_value=state):
                self.assertEqual(self.window._resize_hit_test(QPoint(1, 1)), 0)
        self.window.setResizeEnabled(False)
        self.assertEqual(self.window._resize_hit_test(QPoint(1, 1)), 0)
        self.window.setResizeEnabled(True)
        self.assertEqual(self.window._resize_hit_test(QPoint(1, 1)), 13)
        self.window._closing = True
        self.assertEqual(self.window._resize_hit_test(QPoint(1, 1)), 0)
        self.window._closing = False

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native hit-test adapter')
    def test_native_message_hits_all_eight_directions_without_reading_cursor(self):
        from ctypes import addressof
        from ctypes.wintypes import MSG
        import win32con

        message = MSG()
        message.hWnd = int(self.window.winId())
        message.message = win32con.WM_NCHITTEST
        width, height = self.window.width(), self.window.height()
        for horizontal, vertical, expected in (
                (7, height // 2, 10), (width - 8, height // 2, 11),
                (width // 2, 7, 12), (width // 2, height - 8, 15),
                (15, 15, 13), (width - 16, 15, 14),
                (15, height - 16, 16), (width - 16, height - 16, 17)):
            with self.subTest(hit=expected), \
                    patch('win32gui.ScreenToClient', return_value=(horizontal, vertical)), \
                    patch('win32gui.GetClientRect', return_value=(0, 0, width, height)), \
                    patch('win32api.GetCursorPos') as cursor, \
                    patch.object(self.window, 'devicePixelRatioF', return_value=1.0):
                self.assertEqual(self.window.nativeEvent(b'windows_generic_MSG', addressof(message)),
                                 (True, expected))
                cursor.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native hit-test adapter')
    def test_native_message_uses_signed_coordinates_and_current_screen_dpi(self):
        from ctypes import addressof
        from ctypes.wintypes import MSG
        import win32con

        message = MSG()
        message.hWnd = int(self.window.winId())
        message.message = win32con.WM_NCHITTEST
        message.lParam = ((-40 & 0xffff) << 16) | (-150 & 0xffff)
        width, height = self.window.width(), self.window.height()
        for ratio in (1.0, 1.25, 1.5, 2.0):
            with self.subTest(ratio=ratio), \
                    patch('win32gui.ScreenToClient', return_value=((width - 12) * ratio,
                                                                  (height - 12) * ratio)) as convert, \
                    patch('win32gui.GetClientRect', return_value=(0, 0, width * ratio, height * ratio)), \
                    patch.object(self.window, 'devicePixelRatioF', return_value=ratio):
                self.assertEqual(self.window.nativeEvent(b'windows_generic_MSG', addressof(message)),
                                 (True, 17))
                convert.assert_called_once_with(message.hWnd, (-150, -40))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native hit-test adapter')
    def test_native_interior_delegates_without_stealing_buttons(self):
        from ctypes import addressof
        from ctypes.wintypes import MSG
        import win32con

        message = MSG()
        message.hWnd = int(self.window.winId())
        message.message = win32con.WM_NCHITTEST
        width, height = self.window.width(), self.window.height()
        with patch('win32gui.ScreenToClient', return_value=(width // 2, height // 2)), \
                patch('win32gui.GetClientRect', return_value=(0, 0, width, height)), \
                patch.object(FluentWindow, 'nativeEvent', return_value=(True, win32con.HTCLIENT)) as original:
            self.assertEqual(self.window.nativeEvent(b'windows_generic_MSG', addressof(message)),
                             (True, win32con.HTCLIENT))
            original.assert_called_once()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows resize frame style')
    def test_frame_refresh_restores_resize_style_only_when_missing(self):
        import win32con

        for style in (win32con.WS_MINIMIZEBOX, win32con.WS_MINIMIZEBOX | win32con.WS_THICKFRAME):
            with self.subTest(style=style), \
                    patch('win32gui.GetWindowLong', return_value=style), \
                    patch('win32gui.SetWindowLong') as update, \
                    patch('ctypes.windll.user32.SetWindowPos') as refresh:
                self.window._refresh_native_window_frame()
                if style & win32con.WS_THICKFRAME:
                    update.assert_not_called()
                else:
                    update.assert_called_once_with(int(self.window.winId()), win32con.GWL_STYLE,
                                                   style | win32con.WS_THICKFRAME)
                refresh.assert_called_once()


if __name__ == '__main__':
    unittest.main()
