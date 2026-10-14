"""Injected native backdrops and coordinate-preserving drag rendering regressions."""

import ctypes
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QCoreApplication, QEvent, QPoint, QRect, QSize, Qt
from PyQt5.QtGui import QImage, QPainter, QPixmap
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import FluentWindow

from core.config_manager import ConfigManager
from tests.qt_scale_support import reset_interface_scaling
from tests.test_task_manager import wait_until
from ui.main_window import MainWindow
from ui import materials
from ui.materials import paint_background, paint_window_tint, legacy_background_snapshot
from ui.window_backdrop import (
    AccentPolicy, CompositionData, FrameMargins, GuiThreadInfo, WindowsBackdropApi, WindowBackdrop,
    DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE, DWMSBT_TRANSIENTWINDOW,
    GUI_INMOVESIZE, WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE,
)


class NativeFixture:
    def __init__(self, build=22621, modern=True, legacy=True):
        self.attributes = []
        self.accents = []
        self.margins = []
        self.accent_flags = []
        self.dwmapi = Mock()
        self.user32 = Mock()
        self.modern = modern
        self.legacy = legacy
        self.moving = True
        self.dwmapi.DwmSetWindowAttribute.side_effect = self.attribute
        self.dwmapi.DwmExtendFrameIntoClientArea.side_effect = self.extend
        self.user32.SetWindowCompositionAttribute.side_effect = self.accent
        self.user32.GetWindowThreadProcessId.return_value = 77
        self.user32.GetGUIThreadInfo.side_effect = self.gui_info
        self.user32.RedrawWindow.return_value = 1
        self.api = WindowsBackdropApi(build, self.dwmapi, self.user32)
        self.controller = WindowBackdrop(123, self.api)

    def attribute(self, hwnd, attribute, value, size):
        number = ctypes.cast(value, ctypes.POINTER(ctypes.c_int)).contents.value
        self.attributes.append((hwnd, attribute, number, size))
        return -1 if attribute == DWMWA_SYSTEMBACKDROP_TYPE and number == DWMSBT_TRANSIENTWINDOW and not self.modern else 0

    def extend(self, hwnd, value):
        margins = ctypes.cast(value, ctypes.POINTER(FrameMargins)).contents
        self.margins.append((hwnd, margins.left, margins.right, margins.top, margins.bottom))
        return 0

    def accent(self, hwnd, value):
        data = ctypes.cast(value, ctypes.POINTER(CompositionData)).contents
        policy = ctypes.cast(data.data, ctypes.POINTER(AccentPolicy)).contents
        self.accent_flags.append(policy.flags)
        self.accents.append((hwnd, data.attribute, policy.state, policy.color, data.size))
        return int(self.legacy)

    def gui_info(self, thread_id, value):
        info = ctypes.cast(value, ctypes.POINTER(GuiThreadInfo)).contents
        info.flags = GUI_INMOVESIZE if self.moving else 0
        info.move_size = 123 if self.moving else None
        return 1

    def configure(self, enabled=True, tint='12141CA8', dark=True):
        self.controller.configure(enabled, tint, dark)
        return self.controller

    def restorations(self):
        return (sum(attribute == 38 and value == 3 for _, attribute, value, _ in self.attributes)
                + sum(state == 4 for _, _, state, _, _ in self.accents))


class PaintProbe:
    def __init__(self, function=None):
        self.function = function
        self.call_count = 0

    def __call__(self, painter, rect, **kwargs):
        self.call_count += 1
        if self.function is not None:
            self.function(painter, rect, **kwargs)


class BackdropBackendTests(unittest.TestCase):
    def test_build_matrix_selects_only_supported_backends(self):
        for build, expected in ((17000, 'tint'), (17763, 'legacy'), (19045, 'legacy'),
                                (22000, 'dwm'), (22523, 'dwm')):
            with self.subTest(build=build):
                fixture = NativeFixture(build=build)
                self.assertEqual(fixture.configure().backend, expected)
                self.assertEqual(fixture.api.frame_extended, expected != 'tint')

    def test_controlled_redraw_includes_children_without_erasing_new_edges(self):
        fixture = NativeFixture()
        self.assertTrue(fixture.api.redraw(123))
        fixture.user32.RedrawWindow.assert_called_once_with(123, None, None, 0x581)

    def test_unavailable_redraw_fails_without_breaking_safe_tint(self):
        api = WindowsBackdropApi(17000, available=False)
        controller = WindowBackdrop(123, api)
        controller.configure(True, '12141CA8', True)
        self.assertEqual(controller.paint_mode, 'tint')
        self.assertFalse(api.redraw(123))

    def test_recreated_hwnd_is_rebound_and_configured_once(self):
        fixture = NativeFixture()
        controller = fixture.configure()
        self.assertTrue(controller.bind(456))
        self.assertFalse(controller.bind(456))
        before = fixture.restorations()
        self.assertTrue(controller.configure(True, '12141CA8', True))
        self.assertFalse(controller.configure(True, '12141CA8', True))
        self.assertEqual(fixture.restorations() - before, 1)
        self.assertEqual(fixture.margins[-1], (456, -1, -1, -1, -1))

    def test_recovered_modern_api_releases_previous_legacy_backend(self):
        fixture = NativeFixture(modern=False)
        controller = fixture.configure()
        self.assertEqual(controller.backend, 'legacy')
        fixture.modern = True
        before = len(fixture.accents)
        controller.configure(True, 'FFFFFF80', False)
        self.assertEqual(controller.backend, 'dwm')
        self.assertEqual([state for _, _, state, _, _ in fixture.accents[before:]], [0])

    def test_win11_uses_transient_dwm_backdrop(self):
        fixture = NativeFixture()
        controller = fixture.configure()
        self.assertEqual(controller.backend, 'dwm')
        self.assertEqual(controller.paint_mode, 'clear')
        self.assertIn((123, 38, 3, 4), fixture.attributes)
        self.assertIn((123, 20, 1, 4), fixture.attributes)
        self.assertEqual(fixture.margins, [(123, -1, -1, -1, -1)])
        self.assertFalse(any(state == 4 for _, _, state, _, _ in fixture.accents))

    def test_build_22000_attempts_modern_api(self):
        fixture = NativeFixture(build=22000)
        self.assertEqual(fixture.configure().backend, 'dwm')
        self.assertIn((123, 1029, 0, 4), fixture.attributes)

    def test_win10_uses_legacy_without_requesting_modern_backdrop(self):
        fixture = NativeFixture(build=19045)
        self.assertEqual(fixture.configure().backend, 'legacy')
        self.assertEqual(fixture.attributes, [])
        self.assertEqual(fixture.margins, [(123, -1, -1, -1, -1)])
        self.assertTrue(fixture.api.frame_extended)
        self.assertEqual(fixture.accent_flags[-1], 0x1e0)
        self.assertIn((123, 19, 4, 0xA81C1412, ctypes.sizeof(AccentPolicy)), fixture.accents)

    def test_pre_acrylic_build_falls_back_to_tint(self):
        fixture = NativeFixture(build=17134)
        controller = fixture.configure()
        self.assertEqual(controller.backend, 'tint')
        self.assertEqual(controller.paint_mode, 'tint')
        self.assertEqual(fixture.restorations(), 0)

    def test_rejected_modern_attribute_falls_back_to_legacy(self):
        fixture = NativeFixture(modern=False)
        self.assertEqual(fixture.configure().backend, 'legacy')
        self.assertIn((123, 38, 1, 4), fixture.attributes)

    def test_missing_dwm_function_falls_back_to_legacy(self):
        fixture = NativeFixture()
        fixture.dwmapi.DwmSetWindowAttribute.side_effect = AttributeError('unsupported')
        self.assertEqual(fixture.configure().backend, 'legacy')

    def test_missing_dwm_export_at_initialization_falls_back_safely(self):
        fixture = NativeFixture()
        api = WindowsBackdropApi(22621, SimpleNamespace(), fixture.user32)
        controller = WindowBackdrop(123, api)
        controller.configure(True, '12141CA8', True)
        self.assertEqual(controller.backend, 'legacy')

    def test_repeated_tint_paints_do_not_accumulate_opacity(self):
        image = QImage(20, 20, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        try:
            paint_window_tint(painter, image.rect())
            before = image.pixelColor(10, 10)
            self.assertEqual(before.alpha(), int(materials.acrylic_tint()[6:], 16))
            for _iteration in range(30):
                paint_window_tint(painter, image.rect())
            self.assertEqual(image.pixelColor(10, 10), before)
            self.assertEqual(painter.compositionMode(), QPainter.CompositionMode_SourceOver)
        finally:
            painter.end()

    def test_failed_frame_extension_falls_back_to_legacy(self):
        fixture = NativeFixture()
        fixture.dwmapi.DwmExtendFrameIntoClientArea.return_value = -1
        fixture.dwmapi.DwmExtendFrameIntoClientArea.side_effect = None
        self.assertEqual(fixture.configure().backend, 'legacy')

    def test_optional_dark_mode_failure_does_not_disable_supported_backdrop(self):
        fixture = NativeFixture()
        fixture.dwmapi.DwmSetWindowAttribute.side_effect = lambda hwnd, attr, value, size: -1 if attr == 20 else fixture.attribute(hwnd, attr, value, size)
        self.assertEqual(fixture.configure().backend, 'dwm')

    def test_all_native_backends_failing_uses_static_tint(self):
        fixture = NativeFixture(modern=False, legacy=False)
        self.assertEqual(fixture.configure().backend, 'tint')
        self.assertEqual(fixture.controller.paint_mode, 'tint')

    def test_api_unavailable_does_not_load_native_libraries(self):
        with patch('ui.window_backdrop.ctypes.WinDLL') as loader:
            api = WindowsBackdropApi(22621, available=False)
            controller = WindowBackdrop(123, api)
            controller.configure(True, '12141CA8', True)
        loader.assert_not_called()
        self.assertEqual(controller.backend, 'tint')

    def test_hwnd_and_native_structure_signatures_are_pointer_safe(self):
        fixture = NativeFixture()
        self.assertIs(fixture.dwmapi.DwmSetWindowAttribute.argtypes[0], ctypes.c_void_p)
        self.assertIs(fixture.user32.SetWindowCompositionAttribute.argtypes[0], ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(AccentPolicy), 16)
        self.assertEqual(ctypes.sizeof(CompositionData), 24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 12)
        self.assertEqual(ctypes.sizeof(GuiThreadInfo), 72 if ctypes.sizeof(ctypes.c_void_p) == 8 else 48)

    def test_move_resize_query_uses_the_window_thread_and_native_loop_flag(self):
        fixture = NativeFixture()
        self.assertTrue(fixture.api.is_move_resize_active(123))
        fixture.user32.GetWindowThreadProcessId.assert_called_with(123, None)
        self.assertEqual(fixture.user32.GetGUIThreadInfo.call_args.args[0], 77)
        fixture.moving = False
        self.assertFalse(fixture.api.is_move_resize_active(123))

    def test_failed_move_resize_query_is_unknown_not_an_idle_signal(self):
        fixture = NativeFixture()
        fixture.user32.GetGUIThreadInfo.side_effect = OSError('unavailable')
        self.assertIsNone(fixture.api.is_move_resize_active(123))
        fixture.user32.GetGUIThreadInfo.side_effect = None
        fixture.user32.GetGUIThreadInfo.return_value = 0
        self.assertIsNone(fixture.api.is_move_resize_active(123))
        fixture.api.user32 = None
        self.assertIsNone(fixture.api.is_move_resize_active(123))


class BackdropDragTests(unittest.TestCase):
    def test_static_surface_is_prepared_before_native_blur_is_disabled(self):
        fixture = NativeFixture(build=19045)
        controller = fixture.configure()
        observed = []

        def prepare():
            observed.append((controller.dragging, controller.paint_mode, fixture.accents[-1][2]))

        self.assertTrue(controller.begin_drag(prepare))
        self.assertFalse(controller.begin_drag(prepare))
        self.assertEqual(observed, [(True, 'tint', 4)])
        self.assertEqual(fixture.accents[-1][2], 0)

    def test_modern_drag_keeps_frame_and_glass_without_native_switches(self):
        fixture = NativeFixture()
        controller = fixture.configure()
        before = (list(fixture.attributes), list(fixture.accents), list(fixture.margins))
        prepare = Mock()
        self.assertTrue(controller.begin_drag(prepare))
        self.assertTrue(controller.dragging)
        self.assertTrue(controller._active)
        self.assertEqual(controller.paint_mode, 'clear')
        self.assertFalse(controller.begin_drag(prepare))
        prepare.assert_not_called()
        self.assertTrue(controller.end_drag())
        self.assertFalse(controller.end_drag())
        self.assertEqual(controller.paint_mode, 'clear')
        self.assertEqual((fixture.attributes, fixture.accents, fixture.margins), before)

    def test_legacy_enter_disables_acrylic_once(self):
        fixture = NativeFixture(build=19045)
        controller = fixture.configure()
        fixture.accents.clear()
        controller.begin_drag()
        controller.begin_drag()
        self.assertEqual([state for _, _, state, _, _ in fixture.accents], [0])
        self.assertEqual(controller.paint_mode, 'tint')

    def test_exit_restores_exactly_once_for_both_backends(self):
        for build in (19045, 22621):
            with self.subTest(build=build):
                fixture = NativeFixture(build=build)
                controller = fixture.configure()
                controller.begin_drag()
                before = fixture.restorations()
                self.assertTrue(controller.end_drag())
                self.assertFalse(controller.end_drag())
                self.assertEqual(fixture.restorations() - before, 0 if build >= 22000 else 1)
                self.assertFalse(controller.dragging)
                self.assertEqual(controller.paint_mode, 'clear')

    def test_repeated_settings_are_a_native_noop(self):
        fixture = NativeFixture()
        controller = fixture.configure()
        before = fixture.restorations()
        for _iteration in range(100):
            self.assertFalse(controller.configure(True, '12141CA8', True))
        self.assertEqual(fixture.restorations(), before)

    def test_settings_changed_during_drag_only_apply_latest_values_on_exit(self):
        fixture = NativeFixture(build=19045)
        controller = fixture.configure()
        controller.begin_drag()
        before = fixture.restorations()
        for _iteration in range(50):
            controller.configure(True, 'FFFFFF80', False)
        self.assertEqual(fixture.restorations(), before)
        controller.end_drag()
        self.assertEqual(fixture.restorations(), before + 1)
        self.assertEqual(fixture.accents[-1][3], 0x80FFFFFF)

    def test_switching_modern_blur_off_during_drag_defers_until_exit(self):
        fixture = NativeFixture()
        controller = fixture.configure()
        controller.begin_drag()
        before = fixture.restorations()
        calls = (list(fixture.attributes), list(fixture.accents), list(fixture.margins))
        controller.configure(False, '12141CA8', True)
        self.assertTrue(controller.enabled)
        self.assertTrue(controller._active)
        self.assertEqual(controller.paint_mode, 'clear')
        self.assertEqual((fixture.attributes, fixture.accents, fixture.margins), calls)
        controller.end_drag()
        self.assertEqual(fixture.restorations(), before)
        self.assertEqual(controller.backend, 'off')
        self.assertEqual(controller.paint_mode, 'gradient')
        self.assertEqual(fixture.attributes[len(calls[0]):], [(123, 38, DWMSBT_NONE, 4)])
        controller.end_drag()
        self.assertEqual(fixture.attributes[len(calls[0]):], [(123, 38, DWMSBT_NONE, 4)])

    def test_switching_blur_on_during_drag_defers_native_work(self):
        fixture = NativeFixture()
        controller = fixture.configure(False)
        controller.begin_drag()
        controller.configure(True, '12141CA8', True)
        self.assertEqual(fixture.restorations(), 0)
        self.assertEqual(controller.paint_mode, 'gradient')
        controller.end_drag()
        self.assertEqual(fixture.restorations(), 1)

    def test_modern_setting_changes_apply_only_latest_selection_once_after_exit(self):
        fixture = NativeFixture()
        controller = fixture.configure()
        controller.begin_drag()
        calls = (list(fixture.attributes), list(fixture.accents), list(fixture.margins))
        for _iteration in range(10):
            controller.configure(False, 'FFFFFF80', False)
            controller.configure(True, 'FFFFFF80', False)
            self.assertEqual(controller.paint_mode, 'clear')
        self.assertEqual((fixture.attributes, fixture.accents, fixture.margins), calls)
        before = fixture.restorations()
        controller.end_drag()
        controller.end_drag()
        self.assertEqual(fixture.restorations() - before, 1)
        self.assertEqual(fixture.attributes[len(calls[0]):], [(123, 20, 0, 4), (123, 38, 3, 4)])

    def test_modern_switches_back_to_original_selection_need_no_reapplication(self):
        fixture = NativeFixture()
        controller = fixture.configure()
        calls = (list(fixture.attributes), list(fixture.accents), list(fixture.margins))
        controller.begin_drag()
        controller.configure(False, '12141CA8', True)
        controller.configure(True, '12141CA8', True)
        controller.end_drag()
        self.assertEqual((fixture.attributes, fixture.accents, fixture.margins), calls)

    def test_tint_enter_disables_once_and_exit_attempts_one_reapplication(self):
        fixture = NativeFixture(build=17134, legacy=False)
        controller = fixture.configure()
        with patch.object(fixture.api, 'disable', wraps=fixture.api.disable) as disable, \
                patch.object(fixture.api, 'enable_legacy', wraps=fixture.api.enable_legacy) as enable:
            controller.begin_drag()
            controller.begin_drag()
            self.assertEqual(controller.paint_mode, 'tint')
            disable.assert_called_once_with(123, 'tint')
            enable.assert_not_called()
            controller.end_drag()
            controller.end_drag()
            enable.assert_called_once_with(123, '12141CA8')
            disable.assert_called_once()

    def test_switching_legacy_blur_off_during_drag_only_changes_background_on_exit(self):
        fixture = NativeFixture(build=19045)
        controller = fixture.configure()
        controller.begin_drag()
        calls = list(fixture.accents)
        controller.configure(False, '12141CA8', True)
        self.assertEqual(controller.paint_mode, 'tint')
        self.assertEqual(fixture.accents, calls)
        controller.end_drag()
        self.assertFalse(controller.enabled)
        self.assertEqual(controller.paint_mode, 'gradient')
        self.assertEqual(fixture.accents, calls)

    def test_blur_off_enter_exit_does_not_touch_native_apis(self):
        fixture = NativeFixture()
        controller = fixture.configure(False)
        attributes, accents = len(fixture.attributes), len(fixture.accents)
        controller.begin_drag()
        controller.end_drag()
        self.assertEqual(controller.paint_mode, 'gradient')
        self.assertEqual((len(fixture.attributes), len(fixture.accents)), (attributes, accents))

    def test_close_during_drag_never_reactivates_blur(self):
        fixture = NativeFixture()
        controller = fixture.configure()
        controller.begin_drag()
        before = fixture.restorations()
        controller.close()
        controller.end_drag()
        self.assertEqual(fixture.restorations(), before)
        self.assertFalse(controller.enabled)


class WindowBackdropIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        self.temporary = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temporary.name) / 'config.json')
        self.config.config['appearance'].update(ui_scale='1.0', reduce_motion=True, blur=False)
        self.config.save_config()
        self.fixture = NativeFixture()
        capture = patch('ui.main_window.legacy_background_snapshot', return_value=None)
        self.capture = capture.start()
        self.addCleanup(capture.stop)
        native = patch('qframelesswindow.windows.WindowsWindowEffect')
        native.start()
        self.addCleanup(native.stop)
        frame = patch('qframelesswindow.windows.win32gui.SetWindowPos')
        frame.start()
        self.addCleanup(frame.stop)
        adapter = patch('ui.main_window.WindowsBackdropApi', return_value=self.fixture.api)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.window = MainWindow(self.config, network_time=False, model_sync=False)
        self.window.show()
        QTest.qWait(120)

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        reset_interface_scaling(self.app)
        self.temporary.cleanup()

    def enable_blur(self, build=22621):
        self.config.config['appearance']['blur'] = False
        self.window._apply_blur_setting()
        self.fixture.api.build = build
        self.config.config['appearance']['blur'] = True
        self.window._apply_blur_setting()
        self.window._material_repaint.flush()
        self.app.processEvents()

    def test_restored_window_enables_glass_and_repaints_immediately(self):
        for build in (19045, 22000, 22523):
            with self.subTest(build=build):
                with patch.object(self.window, 'repaint', wraps=self.window.repaint) as repaint:
                    self.enable_blur(build)
                self.assertFalse(self.window.isMaximized())
                self.assertEqual(self.window._backdrop.paint_mode, 'clear')
                self.assertTrue(self.fixture.api.frame_extended)
                self.assertGreater(repaint.call_count, 0)
                self.fixture.user32.RedrawWindow.assert_called_with(int(self.window.winId()), None, None, 0x581)

    def test_legacy_drag_reuses_color_snapshot_and_covers_expanding_edges(self):
        self.enable_blur(19045)
        snapshot = QPixmap(16, 10)
        snapshot.fill(Qt.gray)
        self.capture.return_value = snapshot
        margins = list(self.fixture.margins)
        self.window._interactive_move_resize(True)
        self.assertIs(self.window._drag_background, snapshot)
        self.assertTrue(self.fixture.api.frame_extended)
        for step in range(4):
            self.window.resize(1200 + step * 80, 750 + step * 25)
            self.window._responsive_resize.flush()
            image = self.window.grab().toImage()
            self.assertEqual(image.pixelColor(self.window.width() - 30, self.window.height() - 30), snapshot.toImage().pixelColor(0, 0))
        self.capture.assert_called_once_with(self.window)
        self.assertEqual(self.fixture.margins, margins)
        self.window._interactive_move_resize(False)
        self.assertIsNone(self.window._drag_background)
        self.assertEqual(self.window._backdrop.paint_mode, 'clear')

    def test_snapshot_capture_is_coarse_in_memory_and_uses_logical_coordinates(self):
        screen = Mock()
        captured = QPixmap(1200, 800)
        captured.fill(Qt.gray)
        screen.grabWindow.return_value = captured
        fake = Mock()
        fake.screen.return_value = screen
        fake.isVisible.return_value = True
        fake._window_margins_px.return_value = 8
        fake.size.return_value = QSize(1200, 800)
        for ratio in (1.0, 1.25, 1.5, 2.0):
            for screen_origin, window_origin in ((QPoint(0, 0), QPoint(100, 120)),
                                                  (QPoint(-1920, 0), QPoint(-1500, 120)),
                                                  (QPoint(1920, -1080), QPoint(2000, -950))):
                with self.subTest(ratio=ratio, screen_origin=screen_origin):
                    fake.devicePixelRatioF.return_value = ratio
                    fake.mapToGlobal.return_value = window_origin
                    screen.geometry.return_value = QRect(screen_origin, QSize(1920, 1080))
                    snapshot = legacy_background_snapshot(fake)
                    self.assertEqual(snapshot.size(), QSize(16, 10))
                    self.assertEqual(snapshot.toImage().pixelColor(0, 0), captured.toImage().pixelColor(0, 0))
                    offset = window_origin - screen_origin
                    screen.grabWindow.assert_called_with(0, offset.x(), offset.y(), 1184, 784)

    def test_failed_snapshot_uses_theme_alpha_without_black_or_clear_surface(self):
        screen = Mock()
        screen.geometry.return_value = self.window.screen().geometry()
        screen.grabWindow.side_effect = RuntimeError('capture unavailable')
        with patch.object(self.window, 'screen', return_value=screen):
            self.assertIsNone(legacy_background_snapshot(self.window))
        self.enable_blur(19045)
        self.window._interactive_move_resize(True)
        image = self.window.grab().toImage()
        alpha = image.pixelColor(self.window.width() - 30, self.window.height() - 30).alpha()
        self.assertEqual(alpha, int(materials.acrylic_tint()[6:], 16))
        self.assertGreater(alpha, 0)
        self.assertLess(alpha, 255)

    def test_theme_scale_and_language_changes_defer_compositor_updates_during_drag(self):
        original_theme = materials.THEME_ID
        self.addCleanup(materials.apply_theme_tokens, original_theme)
        self.enable_blur(19045)
        self.window._interactive_move_resize(True)
        calls = (list(self.fixture.accents), list(self.fixture.attributes), list(self.fixture.margins))
        with patch.object(self.window, '_refresh_native_window_frame') as refresh:
            for theme, scale, language in (('light', '1.25', 'English'), ('dark', '1.5', '简体中文')):
                self.config.config['appearance'].update(theme=theme, ui_scale=scale, language=language)
                self.window.apply_appearance()
            refresh.assert_not_called()
        self.assertEqual((self.fixture.accents, self.fixture.attributes, self.fixture.margins), calls)
        before = self.fixture.restorations()
        self.window._interactive_move_resize(False)
        self.assertEqual(self.fixture.restorations() - before, 1)
        self.assertEqual(self.window._backdrop.settings[1], materials.acrylic_tint())
        self.assertEqual(self.fixture.accents[-1][3], int(''.join(materials.acrylic_tint()[offset:offset + 2] for offset in (6, 4, 2, 0)), 16))

    def test_scale_and_dpi_matrix_preserves_glass_and_scaled_rounded_frame(self):
        for ratio in (1.0, 1.25, 1.5, 2.0):
            with self.subTest(dpi=ratio):
                with patch.object(self.window, 'devicePixelRatioF', return_value=ratio):
                    self.enable_blur(19045)
                    self.window._interactive_move_resize(True)
                    self.window.resize(960, 600)
                    self.window._responsive_resize.flush()
                    self.window.grab()
                    self.assertTrue(self.fixture.api.frame_extended)
                    self.assertEqual(self.window._backdrop.paint_mode, 'tint')
                    self.window._interactive_move_resize(False)
                    self.assertEqual(self.window._backdrop.paint_mode, 'clear')
                    self.assertEqual(self.window.windowOpacity(), 1.0)

    def test_maximize_and_restore_preserve_glass_and_restore_border_margin(self):
        self.enable_blur(19045)
        before = self.fixture.restorations()
        self.window.showMaximized()
        self.app.processEvents()
        self.window._sync_window_frame()
        self.assertEqual(self.window._window_margins_px(), 0)
        self.window.showNormal()
        self.app.processEvents()
        self.window._sync_window_frame()
        self.assertGreater(self.window._window_margins_px(), 0)
        self.assertEqual(self.window.windowOpacity(), 1.0)
        self.assertEqual(self.fixture.restorations(), before)
        self.assertTrue(self.fixture.api.frame_extended)

    def test_modern_root_skips_gradient_and_environment_glows(self):
        self.enable_blur()
        expensive, tint = PaintProbe(), PaintProbe()
        with patch('ui.main_window.paint_background', expensive), patch('ui.main_window.paint_window_tint', tint):
            self.window.grab()
        self.assertEqual(expensive.call_count, 0)
        self.assertEqual(tint.call_count, 0)
        self.assertEqual(self.window._backdrop.backend, 'dwm')

    def test_native_enter_exit_messages_transition_once(self):
        from ctypes.wintypes import MSG
        self.enable_blur()
        message = MSG()
        message.hWnd = int(self.window.winId())
        with patch.object(FluentWindow, 'nativeEvent', return_value=(False, 0)):
            message.message = WM_ENTERSIZEMOVE
            self.window.nativeEvent(b'windows_generic_MSG', ctypes.addressof(message))
            self.assertTrue(self.window._backdrop.dragging)
            self.assertEqual(self.window._backdrop.paint_mode, 'clear')
            before = self.fixture.restorations()
            message.message = WM_EXITSIZEMOVE
            self.window.nativeEvent(b'windows_generic_MSG', ctypes.addressof(message))
            self.window.nativeEvent(b'windows_generic_MSG', ctypes.addressof(message))
        self.assertEqual(self.fixture.restorations(), before)
        self.assertFalse(self.window._backdrop.dragging)

    def test_modern_resize_keeps_new_edges_covered_by_the_same_extended_frame(self):
        self.enable_blur()
        calls = (list(self.fixture.attributes), list(self.fixture.accents), list(self.fixture.margins))
        expensive, tint = PaintProbe(), PaintProbe()
        with patch('ui.main_window.paint_background', expensive), patch('ui.main_window.paint_window_tint', tint), \
                patch.object(self.fixture.api, 'disable', wraps=self.fixture.api.disable) as disable, \
                patch.object(self.fixture.api, 'enable_modern', wraps=self.fixture.api.enable_modern) as enable:
            for _iteration in range(3):
                self.window._interactive_move_resize(True)
                for step in range(12):
                    self.window.resize(1100 + step * 30, 700 + step * 10)
                    self.window._responsive_resize.flush()
                    self.app.processEvents()
                    image = self.window.grab().toImage()
                    self.assertEqual(image.pixelColor(self.window.width() - 20, self.window.height() - 20).alpha(), 1)
                    self.assertTrue(self.window._backdrop._active)
                    self.assertEqual(self.window._backdrop.paint_mode, 'clear')
                self.window._interactive_move_resize(False)
            disable.assert_not_called()
            enable.assert_not_called()
        self.assertEqual(expensive.call_count, 0)
        self.assertEqual(tint.call_count, 0)
        self.assertEqual((self.fixture.attributes, self.fixture.accents, self.fixture.margins), calls)
        self.assertEqual(self.fixture.margins, [(int(self.window.winId()), -1, -1, -1, -1)])

    def test_idle_after_missing_exit_corrects_drag_once_without_modern_switches(self):
        for build in (19045, 22621):
            with self.subTest(build=build):
                self.fixture.moving = True
                self.enable_blur(build)
                self.window._interactive_move_resize(True)
                before = self.fixture.restorations()
                self.window._move_resize_dispatcher.aboutToBlock.emit()
                self.assertTrue(self.window._backdrop.dragging)
                self.fixture.moving = False
                self.window._move_resize_dispatcher.aboutToBlock.emit()
                self.window._move_resize_dispatcher.aboutToBlock.emit()
                self.assertFalse(self.window._backdrop.dragging)
                self.assertEqual(self.fixture.restorations() - before, 0 if build >= 22000 else 1)

    def test_unknown_native_loop_state_cannot_restore_a_backdrop_mid_drag(self):
        self.enable_blur(build=19045)
        self.window._interactive_move_resize(True)
        before = self.fixture.restorations()
        with patch.object(self.fixture.api, 'is_move_resize_active', return_value=None):
            self.window._recover_drag_on_idle()
        self.assertTrue(self.window._backdrop.dragging)
        self.assertEqual(self.fixture.restorations(), before)
        self.window._interactive_move_resize(False)
        self.assertEqual(self.fixture.restorations(), before + 1)

    def test_modern_configuration_during_drag_keeps_paint_transparent_until_exit(self):
        self.enable_blur()
        self.window._interactive_move_resize(True)
        calls = list(self.fixture.attributes)
        self.config.config['appearance']['blur'] = False
        self.window._apply_blur_setting()
        self.window._material_repaint.flush()
        self.app.processEvents()
        self.assertEqual(self.window._backdrop.paint_mode, 'clear')
        self.assertEqual(self.fixture.attributes, calls)
        self.window._interactive_move_resize(False)
        self.assertEqual(self.window._backdrop.paint_mode, 'gradient')
        self.assertEqual(self.fixture.attributes[len(calls):], [(int(self.window.winId()), 38, 1, 4)])

    def test_drag_surface_is_repainted_before_disabling_native_acrylic(self):
        self.enable_blur(build=19045)
        observed = []
        original = self.fixture.api.disable

        def disable(hwnd, backend=None):
            observed.append(self.window.grab().toImage().pixelColor(self.window.width() - 30, self.window.height() - 30).alpha())
            original(hwnd, backend)

        with patch.object(self.fixture.api, 'disable', side_effect=disable), \
                patch.object(self.window, 'repaint', wraps=self.window.repaint) as repaint:
            self.window._interactive_move_resize(True)
        self.assertEqual(observed, [int(materials.acrylic_tint()[6:], 16)])
        repaint.assert_called_once()
        self.assertFalse(self.window._material_repaint.timer.isActive())

    def test_nccalcsize_preserves_parent_geometry_without_forcing_full_redraw(self):
        from ctypes.wintypes import MSG
        import win32con

        message = MSG()
        message.hWnd = int(self.window.winId())
        message.message = win32con.WM_NCCALCSIZE
        message.wParam = 1
        with patch.object(FluentWindow, 'nativeEvent', return_value=(True, win32con.WVR_REDRAW)) as original:
            self.assertEqual(self.window.nativeEvent(b'windows_generic_MSG', ctypes.addressof(message)), (True, 0))
        original.assert_called_once()
        with patch.object(FluentWindow, 'nativeEvent', return_value=(False, 0)):
            self.assertEqual(self.window.nativeEvent(b'windows_generic_MSG', ctypes.addressof(message)), (False, 0))

    def test_coalesced_resizes_do_not_force_native_frame_recalculation_with_or_without_blur(self):
        for enabled in (False, True):
            with self.subTest(blur=enabled):
                self.config.config['appearance']['blur'] = enabled
                self.window._apply_blur_setting()
                self.window._interactive_move_resize(True)
                with patch.object(self.window, '_refresh_native_window_frame') as refresh:
                    for step in range(12):
                        self.window.resize(1200 + step * 8, 750 + step * 4)
                        self.window._responsive_resize.flush()
                        self.app.processEvents()
                    refresh.assert_not_called()
                self.window._interactive_move_resize(False)

    def test_blur_off_drag_freezes_only_decorative_background_until_exit(self):
        expensive = PaintProbe(paint_background)
        with patch('ui.main_window.paint_background', expensive), patch('ui.materials.paint_background', expensive):
            self.window._interactive_move_resize(True)
            snapshot = self.window._drag_background
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.toImage().pixelColor(10, 10).alpha(), 255)
            for step in range(12):
                self.window.resize(1200 + step * 8, 750 + step * 4)
                self.window._responsive_resize.flush()
                self.app.processEvents()
                self.window.grab()
                self.assertIs(self.window._drag_background, snapshot)
            self.assertEqual(expensive.call_count, 1)
            self.window._interactive_move_resize(False)
            self.assertIsNone(self.window._drag_background)
            self.window._material_repaint.flush()
            self.window.grab()
            self.assertGreater(expensive.call_count, 1)

    def test_many_resizes_keep_layout_live_but_never_reenable_blur_or_paint_gradient(self):
        for build in (19045, 22621):
            with self.subTest(build=build):
                self.enable_blur(build)
                self.window._interactive_move_resize(True)
                before = self.fixture.restorations()
                layout = Mock(wraps=self.window._responsive_resize.callback)
                self.window._responsive_resize.callback = layout
                expensive, tint = PaintProbe(), PaintProbe(paint_window_tint)
                with patch('ui.main_window.paint_background', expensive), patch('ui.main_window.paint_window_tint', tint):
                    for step in range(12):
                        self.window.resize(1100 + step * 6, 700 + step * 3)
                        self.window._responsive_resize.flush()
                        self.app.processEvents()
                        self.window.grab()
                    self.assertEqual(expensive.call_count, 0)
                    if build >= 22000:
                        self.assertEqual(tint.call_count, 0)
                    else:
                        self.assertGreater(tint.call_count, 0)
                self.assertGreaterEqual(layout.call_count, 12)
                self.assertEqual(self.fixture.restorations(), before)
                self.window._interactive_move_resize(False)
                self.assertEqual(self.fixture.restorations(), before if build >= 22000 else before + 1)

    def test_burst_repaints_are_coalesced(self):
        self.enable_blur(build=19045)
        self.window._material_repaint.callback = Mock()
        self.window._interactive_move_resize(True)
        for step in range(30):
            self.window.resize(1100 + step, 700 + step)
        self.assertTrue(self.window._material_repaint.timer.isActive())
        self.window._material_repaint.flush()
        self.window._material_repaint.callback.assert_called_once_with()

    def test_drag_preserves_opacity_frame_and_eight_direction_hit_testing(self):
        self.enable_blur(build=19045)
        before = (self.window.windowOpacity(), self.window._window_margin, self.window._window_radius)
        self.window._interactive_move_resize(True)
        self.window.resize(960, 600)
        for point, hit in (((1, 300), 10), ((959, 300), 11), ((480, 1), 12), ((480, 599), 15),
                           ((1, 1), 13), ((959, 1), 14), ((1, 599), 16), ((959, 599), 17)):
            self.assertEqual(self.window._resize_hit_test(QPoint(*point)), hit)
        image = self.window.grab().toImage()
        self.assertGreater(image.pixelColor(1, 1).alpha(), 0)
        self.window._interactive_move_resize(False)
        self.assertEqual((self.window.windowOpacity(), self.window._window_margin, self.window._window_radius), before)
        self.assertEqual(self.window.minimumWidth(), 960)
        self.assertEqual(self.window.minimumHeight(), 600)

    def test_blur_off_still_paints_original_gradient(self):
        expensive, tint = PaintProbe(), PaintProbe()
        with patch('ui.main_window.paint_background', expensive), patch('ui.main_window.paint_window_tint', tint):
            self.window.grab()
        self.assertGreater(expensive.call_count, 0)
        self.assertEqual(tint.call_count, 0)
        self.assertEqual(self.window.windowOpacity(), 1.0)

    def test_close_stops_material_timer_and_never_restores_backdrop(self):
        self.enable_blur()
        self.window._interactive_move_resize(True)
        before = self.fixture.restorations()
        self.window.close()
        self.assertFalse(self.window._material_repaint.timer.isActive())
        self.assertIsNone(self.window._move_resize_dispatcher)
        self.window._interactive_move_resize(False)
        self.assertEqual(self.fixture.restorations(), before)


if __name__ == '__main__':
    unittest.main()
