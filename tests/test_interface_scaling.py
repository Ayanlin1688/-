"""Extra interface zoom: no double DPI, cumulative growth or stale layouts."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRect, QSize, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

from core.config_manager import ConfigManager, DEFAULT_CONFIG
from ui import tokens
from ui.scaling import SCALE_CHOICES, apply_interface_scale, interface_factors
from tests.qt_scale_support import flush_layout, reset_interface_scaling


class FakeScreen(QObject):
    logicalDotsPerInchChanged = pyqtSignal(float)

    def __init__(self, dpi=96, width=1920, height=1080, available_height=1040):
        super().__init__()
        self.dpi = dpi
        self.screen_size = QSize(width, height)
        self.available = QRect(0, 0, width, available_height)

    def name(self):
        return 'interface-scale-fixture'

    def logicalDotsPerInch(self):
        return self.dpi

    def size(self):
        return self.screen_size

    def availableGeometry(self):
        return self.available


class InterfaceFactorsTests(unittest.TestCase):
    def test_auto_1080p_96dpi(self):
        self.assertEqual(interface_factors(FakeScreen())['z'], 1)

    def test_auto_unscaled_4k(self):
        factors = interface_factors(FakeScreen(96, 3840, 2160, 2080))
        self.assertAlmostEqual(factors['z'], 1.6)
        self.assertAlmostEqual(factors['total'], 1.6)

    def test_auto_4k_200_percent_is_not_scaled_twice(self):
        factors = interface_factors(FakeScreen(192, 1920, 1080))
        self.assertEqual(factors['z'], 1)
        self.assertEqual(factors['total'], 2)

    def test_auto_4k_150_percent_is_not_scaled_twice(self):
        factors = interface_factors(FakeScreen(144, 2560, 1440))
        self.assertEqual(factors['z'], 1)
        self.assertEqual(factors['total'], 1.5)

    def test_manual_choices_are_extra_scale(self):
        for choice in SCALE_CHOICES[1:]:
            with self.subTest(choice=choice):
                factors = interface_factors(FakeScreen(192), choice)
                self.assertAlmostEqual(factors['z'], float(choice))
                self.assertAlmostEqual(factors['total'], 2 * float(choice))

    def test_auto_uses_logical_screen_height_not_work_area(self):
        screen = FakeScreen(96, 3840, 2160, 800)
        screen.available = QRect(-3840, 40, 3800, 800)
        self.assertAlmostEqual(interface_factors(screen)['z'], 1.6)

    def test_auto_clamps_small_large_and_extreme_dpi(self):
        for dpi, height, expected in ((96, 720, 1), (96, 8640, 1.6), (48, 2160, 2), (288, 720, 1)):
            with self.subTest(dpi=dpi, height=height):
                self.assertEqual(interface_factors(FakeScreen(dpi, 3840, height))['z'], expected)

    def test_invalid_settings_and_dpi_have_safe_fallback(self):
        for dpi in (0, -1, float('nan'), float('inf')):
            with self.subTest(dpi=dpi):
                self.assertEqual(interface_factors(FakeScreen(dpi))['z'], 1)
        for choice in (None, '', 'invalid', 'nan', 'inf'):
            with self.subTest(choice=choice):
                self.assertEqual(interface_factors(FakeScreen(96, 3840, 2160), choice)['z'], 1.6)

    def test_manual_out_of_range_values_are_clamped(self):
        self.assertEqual(interface_factors(FakeScreen(), '-2')['z'], 1)
        self.assertEqual(interface_factors(FakeScreen(), '10')['z'], 1.75)

    def test_manual_175_percent_is_available_without_changing_auto(self):
        self.assertIn('1.75', SCALE_CHOICES)
        self.assertEqual(interface_factors(FakeScreen(), '1.75')['z'], 1.75)
        self.assertEqual(interface_factors(FakeScreen(96, 3840, 2160))['z'], 1.6)


class InterfaceApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        QTest.qWait(10)
        self.base_font = QFont(self.app.font())
        self.root = QWidget()
        self.layout = QVBoxLayout(self.root)
        self.layout.setContentsMargins(12, 8, 12, 8)
        self.layout.setSpacing(10)
        self.layout.addSpacing(20)
        self.button = QPushButton('scale fixture')
        self.button.setFixedHeight(40)
        self.button.setIconSize(QSize(20, 20))
        self.button.setStyleSheet('QPushButton {border-radius:8px; padding:4px 12px;}')
        self.caption = QLabel('explicit font')
        font = QFont(self.caption.font())
        font.setPixelSize(12)
        self.caption.setFont(font)
        self.layout.addWidget(self.button)
        self.layout.addWidget(self.caption)
        self.root.show()
        QTest.qWait(20)

    def tearDown(self):
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        self.root.close()
        self.root.deleteLater()
        QTest.qWait(20)

    def test_application_font_is_scaled_in_points(self):
        apply_interface_scale(self.app, FakeScreen(192), '1.6')
        self.assertAlmostEqual(self.app.font().pointSizeF(), self.base_font.pointSizeF() * 1.6)

    def test_visible_windows_remain_frozen_until_scale_hook_and_layout_commit(self):
        observed = []
        self.root.on_interface_scale_committed = lambda old, new: observed.append(
            (old, new, self.root.updatesEnabled()))
        apply_interface_scale(self.app, FakeScreen(), '1.25')
        self.assertEqual(observed, [(1.0, 1.25, False)])
        self.assertTrue(self.root.updatesEnabled())

    def test_window_updates_are_restored_if_widget_refresh_raises(self):
        scaler = self.app._interface_scaler
        with patch.object(scaler, '_refresh_widget', side_effect=RuntimeError('fixture')):
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                scaler.apply(1.25)
        self.assertTrue(self.root.updatesEnabled())
        self.assertFalse(scaler.applying)

    def test_window_updates_are_restored_if_commit_hook_raises(self):
        self.root.on_interface_scale_committed = lambda *_args: (_ for _ in ()).throw(RuntimeError('hook'))
        with self.assertRaisesRegex(RuntimeError, 'hook'):
            apply_interface_scale(self.app, FakeScreen(), '1.25')
        self.assertTrue(self.root.updatesEnabled())
        del self.root.on_interface_scale_committed

    def test_style_tokens_layout_controls_and_icons_scale_together(self):
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        self.assertIn('border-radius:12.8px', self.button.styleSheet())
        self.assertIn('padding:6.4px 19.2px', self.button.styleSheet())
        self.assertEqual(self.button.height(), 64)
        self.assertEqual(self.button.iconSize(), QSize(32, 32))
        self.assertEqual(self.caption.font().pixelSize(), 19)
        self.assertEqual(self.layout.contentsMargins().left(), 19)
        self.assertEqual(self.layout.spacing(), 16)
        self.assertEqual(self.layout.itemAt(0).sizeHint().height(), 32)
        self.assertEqual(tokens.RADIUS['container'], 19)
        self.assertEqual(tokens.NAV_EXPAND_WIDTH, 384)

    def test_repeated_scale_and_round_trip_preserve_baselines(self):
        for choice in ('1.6', '1.6', '1.25', '1.6', '1.0'):
            apply_interface_scale(self.app, FakeScreen(), choice)
            QTest.qWait(20)
        self.assertEqual(self.button.height(), 40)
        self.assertEqual(self.button.iconSize(), QSize(20, 20))
        self.assertEqual(self.caption.font().pixelSize(), 12)
        self.assertIn('border-radius:8px', self.button.styleSheet())
        self.assertEqual(self.layout.contentsMargins().left(), 12)
        self.assertEqual(self.layout.itemAt(0).sizeHint().height(), 20)
        self.assertAlmostEqual(self.app.font().pointSizeF(), self.base_font.pointSizeF())

    def test_new_controls_inherit_zoom_and_do_not_accumulate(self):
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        child = QPushButton('late control', self.root)
        child.setFixedHeight(30)
        child.setStyleSheet('QPushButton {font-size:13px; border-radius:8px;}')
        self.layout.addWidget(child)
        child.show()
        QTest.qWait(40)
        self.assertEqual(child.height(), 48)
        self.assertIn('font-size:20.8px', child.styleSheet())
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        QTest.qWait(20)
        self.assertIn('font-size:20.8px', child.styleSheet())
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        QTest.qWait(20)
        self.assertEqual(child.height(), 30)
        self.assertEqual(child.font().pixelSize(), 13)

    def test_theme_style_reset_uses_original_px_and_colors(self):
        from ui.theme import _theme_rules
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        self.button.setStyleSheet(_theme_rules('input'))
        QTest.qWait(30)
        self.assertEqual(self.button.styleSheet(), tokens.scale_stylesheet(_theme_rules('input')))
        for choice in ('1.25', '1.0'):
            apply_interface_scale(self.app, FakeScreen(), choice)
        self.assertEqual(self.button.styleSheet(), _theme_rules('input'))

    def test_fluent_image_and_card_radii_keep_their_own_baselines(self):
        from qfluentwidgets import CardWidget, ImageLabel
        card = CardWidget(self.root)
        card.setBorderRadius(12)
        image = ImageLabel(self.root)
        image.setBorderRadius(6, 8, 4, 2)
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        self.assertEqual(card.getBorderRadius(), 19)
        self.assertEqual((image._topLeftRadius, image._topRightRadius, image._bottomLeftRadius, image._bottomRightRadius),
                         (10, 13, 6, 3))
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        self.assertEqual(card.getBorderRadius(), 12)
        self.assertEqual(image._topLeftRadius, 6)

    def test_stylesheet_scaling_preserves_non_px_values(self):
        self.assertEqual(tokens.scale_stylesheet('color:rgba(1,2,3,0.5); margin:-2px; font:10pt; width:50%;', 1.5),
                         'color:rgba(1,2,3,0.5); margin:-3px; font:10pt; width:50%;')

    def test_refresh_settles_without_repolishing_unchanged_widgets(self):
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        QTest.qWait(50)
        scaler = self.app._interface_scaler
        with patch.object(self.button, 'setStyleSheet', wraps=self.button.setStyleSheet) as restyle:
            for _ in range(3):
                scaler.pending.add(self.button)
                scaler._refresh_pending()
                QTest.qWait(20)
            restyle.assert_not_called()
        self.assertFalse(scaler.timer.isActive())
        self.assertEqual(self.caption.font().pixelSize(), 19)

    def test_closed_window_is_not_restyled_but_reopening_applies_current_zoom(self):
        self.root.close()
        with patch.object(self.button, 'setStyleSheet', wraps=self.button.setStyleSheet) as restyle:
            apply_interface_scale(self.app, FakeScreen(), '1.6')
            restyle.assert_not_called()
        self.root.show()
        QTest.qWait(40)
        self.assertEqual(self.button.height(), 64)
        self.assertIn('border-radius:12.8px', self.button.styleSheet())

    def test_replaced_spacer_does_not_modify_new_layout_item(self):
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        old = self.layout.takeAt(0)
        self.layout.insertSpacing(0, 5)
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        self.assertEqual(self.layout.itemAt(0).sizeHint().height(), 5)
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        self.assertEqual(self.layout.itemAt(0).sizeHint().height(), 8)
        del old

    def test_font_format_change_never_rebases_an_already_scaled_size(self):
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        scaler = self.app._interface_scaler
        font = self.caption.font()
        font.setBold(True)
        self.caption.setFont(font)
        QTest.qWait(30)
        self.assertEqual(scaler.widgets[self.caption]['font'].pixelSize(), 12)
        self.assertEqual(self.caption.font().pixelSize(), 19)
        font = self.caption.font()
        font.setPixelSize(12)
        self.caption.setFont(font)
        scaler._refresh_pending()
        self.assertEqual(self.caption.font().pixelSize(), 19)
        self.assertEqual(scaler.widgets[self.caption]['font'].pixelSize(), 12)
        font = self.caption.font()
        font.setPixelSize(12)
        self.caption.setFont(font)
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        self.assertEqual(self.caption.font().pixelSize(), 19)
        self.assertFalse(scaler.pending)
        self.assertFalse(scaler.timer.isActive())
        for choice in ('1.25', '1.6', '1.0'):
            apply_interface_scale(self.app, FakeScreen(), choice)
        self.assertEqual(self.caption.font().pixelSize(), 12)
        self.assertTrue(self.caption.font().bold())

    def test_deferred_own_style_and_font_events_do_not_restart_refresh(self):
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        scaler = self.app._interface_scaler
        scaler._refresh_pending()
        for kind in (QEvent.StyleChange, QEvent.FontChange, QEvent.Polish, QEvent.Show):
            QCoreApplication.postEvent(self.button, QEvent(kind))
        QCoreApplication.sendPostedEvents(self.button)
        self.assertNotIn(self.button, scaler.pending)
        self.assertFalse(scaler.timer.isActive())

    def test_unrelated_events_do_not_traverse_native_widget_ownership(self):
        scaler = self.app._interface_scaler
        with patch.object(self.button, 'window', side_effect=AssertionError('unrelated event')):
            self.assertFalse(scaler.eventFilter(self.button, QEvent(QEvent.MouseMove)))

    def test_replayed_scaled_stylesheet_uses_its_original_source(self):
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        old_style = self.button.styleSheet()
        apply_interface_scale(self.app, FakeScreen(), '1.25')
        self.button.setStyleSheet(old_style)
        self.app._interface_scaler._refresh_pending()
        self.assertIn('border-radius:10px', self.button.styleSheet())
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        self.assertIn('border-radius:8px', self.button.styleSheet())

    def test_late_control_derived_from_app_font_keeps_original_point_size(self):
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        child = QLabel('late inherited font', self.root)
        font = child.font()
        font.setBold(True)
        child.setFont(font)
        self.layout.addWidget(child)
        child.show()
        self.app._interface_scaler._refresh_pending()
        self.assertAlmostEqual(child.font().pointSizeF(), self.base_font.pointSizeF() * 1.6)
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        self.assertAlmostEqual(child.font().pointSizeF(), self.base_font.pointSizeF())
        self.assertTrue(child.font().bold())

    def test_explicit_flush_stops_timer_and_visits_each_widget_at_most_once(self):
        scaler = self.app._interface_scaler
        for _iteration in range(100):
            scaler.pending.add(self.button)
        scaler.timer.start(0)
        with patch.object(scaler, '_refresh_widget', wraps=scaler._refresh_widget) as refresh:
            scaler._refresh_pending()
        self.assertEqual([call.args[0] for call in refresh.call_args_list], [self.button])
        self.assertFalse(scaler.pending)
        self.assertFalse(scaler.timer.isActive())

    def test_dispose_removes_filter_pending_timer_and_global_scale(self):
        apply_interface_scale(self.app, FakeScreen(), '1.6')
        scaler = self.app._interface_scaler
        scaler.pending.add(self.button)
        scaler.timer.start(0)
        scaler.dispose()
        scaler.dispose()
        self.assertFalse(scaler.timer.isActive())
        self.assertFalse(scaler.pending)
        self.assertFalse(hasattr(self.app, '_interface_scaler'))
        self.assertEqual(tokens.interface_scale(), 1.0)
        self.assertEqual(self.app.font(), self.base_font)
        QCoreApplication.sendEvent(self.button, QEvent(QEvent.FontChange))
        self.assertFalse(scaler.pending)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertTrue(sip.isdeleted(scaler))

    def test_recreated_scaler_does_not_capture_a_scaled_application_font(self):
        for choice in ('1.6', '1.25', '1.6'):
            apply_interface_scale(self.app, FakeScreen(), choice)
            self.assertAlmostEqual(self.app.font().pointSizeF(), self.base_font.pointSizeF() * float(choice))
            reset_interface_scaling(self.app)
            self.assertEqual(self.app.font(), self.base_font)
            self.assertEqual(self.caption.font().pixelSize(), 12)


class InterfaceWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        from ui.main_window import MainWindow
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        if sys.platform == 'win32':
            native = patch('qframelesswindow.windows.win32gui.SetWindowPos')
            native.start()
            self.addCleanup(native.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'config.json'
        self.manager = ConfigManager(self.path)
        self.window = MainWindow(self.manager, network_time=False)
        self.window.show()
        QTest.qWait(100)
        self.window._on_screen_changed(FakeScreen())

    def tearDown(self):
        self.manager.config['appearance']['ui_scale'] = '1.0'
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(40)
        self.temporary.cleanup()

    def test_settings_choices_persist_and_apply_immediately(self):
        page = self.window.settings_page
        self.assertEqual(DEFAULT_CONFIG['appearance']['ui_scale'], 'auto')
        self.assertEqual([page.ui_scale.itemData(index) for index in range(page.ui_scale.count())], list(SCALE_CHOICES))
        page.ui_scale.setCurrentIndex(SCALE_CHOICES.index('1.25'))
        self.assertEqual(self.window._interface_factors['z'], 1.25)
        self.assertIn('125%', page.ui_scale_status.text())
        self.assertEqual(ConfigManager(self.path).load_config()['appearance']['ui_scale'], '1.25')
        self.assertEqual(self.window.navigationInterface.panel.minimumWidth(), 80)

    def test_api_key_edit_resets_ecommerce_model_test_status(self):
        ecommerce = self.window.ecommerce_page
        ecommerce.analysis_status_dot.set_color('#67c23a')
        ecommerce.analysis_status_dot.setToolTip('模型测试通过：文本调用成功')
        self.window.settings_page.api_key.setText('offline-fixture-key')
        self.assertEqual(ecommerce.analysis_status_dot._color.name(), '#92929b')
        self.assertEqual(ecommerce.analysis_status_dot.toolTip(), '未测试')

    def test_screen_switch_and_dpi_signal_recompute_without_restart(self):
        screen = FakeScreen(96, 3840, 2160)
        self.window._on_screen_changed(screen)
        self.assertEqual(self.window._interface_factors['z'], 1.6)
        screen.dpi = 144
        screen.screen_size = QSize(2560, 1440)
        screen.logicalDotsPerInchChanged.emit(144)
        self.assertEqual(self.window._interface_factors['z'], 1)
        self.assertIn('150%', self.window.settings_page.ui_scale_status.text())

    def test_manual_mode_remains_fixed_when_screen_changes(self):
        self.manager.config['appearance']['ui_scale'] = '1.3'
        self.window._on_screen_changed(FakeScreen(192))
        self.assertEqual(self.window._interface_factors['z'], 1.3)
        self.assertIn('260%', self.window.settings_page.ui_scale_status.text())

    def test_navigation_and_theme_switch_keep_manual_zoom(self):
        self.manager.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        for collapsed in (False, True, False, True):
            self.window._sync_nav_pane(collapsed)
            self.assertEqual(self.window.brand_logo.height(), round(68 * 1.6))
            expected_width = round(64 * 1.6) if collapsed else self.window.brand_logo.expanded_width()
            self.assertEqual(self.window.brand_logo.width(), expected_width)
            self.assertGreaterEqual(self.window.navigationInterface.panel.expandWidth,
                                    self.window.brand_logo.expanded_width() + round(16 * 1.6))
        self.manager.config['appearance']['theme'] = 'light'
        self.window.apply_appearance()
        QTest.qWait(30)
        self.assertEqual(self.window._interface_factors['z'], 1.6)
        self.assertIn('font-size:28.8px', self.window.settings_page.appearance_group.titleLabel.styleSheet())

    def test_expanded_navigation_fits_full_brand_across_pages_and_live_zoom(self):
        # This test exercises the expanded rail on a genuinely wide workspace.
        screen = patch.object(self.window, 'screen', return_value=FakeScreen(96, 3840, 2160, 2080))
        screen.start()
        self.addCleanup(screen.stop)
        self.window.resize(2200, 1000)
        panel = self.window.navigationInterface.panel
        panel.expandAni.stop()
        panel.expandAni.setDuration(0)
        labels = [label for label in self.window.titleBar.findChildren(QLabel)
                  if label.text() == self.window.windowTitle()]
        self.assertTrue(labels)
        for choice in ('1.0', '1.5', '1.6', '1.0'):
            with self.subTest(scale=choice):
                self.manager.config['appearance']['ui_scale'] = choice
                self.window._apply_interface_scale()
                self.window._sync_nav_pane(False)
                panel.expand(useAni=False)
                for page in (self.window.workspace_page, self.window.history_page, self.window.settings_page):
                    self.window.switchTo(page)
                    flush_layout(self.app, self.window)
                    brand = self.window.brand_logo
                    bounds = QRect(brand.mapTo(panel, QPoint()), brand.size())
                    self.assertTrue(brand._expanded)
                    self.assertEqual(brand.width(), brand.expanded_width())
                    self.assertTrue(panel.rect().contains(bounds), (bounds, panel.rect()))
                    self.assertEqual(panel.width(), panel.expandWidth)
                    self.assertFalse(any(label.isVisible() for label in labels))

    def test_task_rows_and_headers_share_scaled_metrics(self):
        panel = self.window.workspace_page.queue_panel
        panel.update_tasks([dict(local_id='scale-task', prompt_name='fixture', prompt_path='C:/fixture.txt',
                                 product='fixture', status='waiting', images=[])])
        self.manager.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        QTest.qWait(60)
        self.assertEqual(panel.list.item(0).sizeHint().height(), round(56 * 1.6))
        self.assertEqual(panel.rows[0].height(), round(52 * 1.6))
        for column, width in enumerate(panel._widths):
            self.assertEqual(panel._header_grid.columnMinimumWidth(column), width)
            self.assertEqual(panel.rows[0].grid.columnMinimumWidth(column), width)

    def test_history_row_height_and_selection_header_update_live(self):
        table = self.window.history_page.table
        base = table.verticalHeader().defaultSectionSize()
        self.manager.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        QTest.qWait(40)
        self.assertEqual(table.verticalHeader().defaultSectionSize(), round(base * 1.6))
        header = table.horizontalHeader()
        self.assertEqual(header.checkBox.text(), '')
        self.assertGreater(header.checkBox.width(), header._checkbox_size.width())
        self.assertGreaterEqual(header.checkBox.x(), header.sectionViewportPosition(0))
        self.assertLessEqual(header.checkBox.geometry().right(), header.sectionViewportPosition(0) + header.sectionSize(0) - 1)

    def test_page_switch_after_fractional_scaling_reaches_an_idle_fixed_point(self):
        scaler = self.app._interface_scaler
        for choice in ('1.6', '1.25', '1.0'):
            self.manager.config['appearance']['ui_scale'] = choice
            self.window.apply_appearance()
            for page in (self.window.history_page, self.window.settings_page, self.window.workspace_page):
                self.window.switchTo(page)
                flush_layout(self.app, self.window)
                self.assertFalse(scaler.pending)
                self.assertFalse(scaler.timer.isActive())
                with patch.object(scaler, '_refresh_pending', wraps=scaler._refresh_pending) as refresh:
                    QCoreApplication.sendPostedEvents()
                    self.app.processEvents()
                    refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
