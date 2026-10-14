"""Responsive geometry and resize regressions with isolated, unpaid fixtures."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
import weakref
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QPoint, QRect, QSize, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QAbstractButton, QWidget

from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from ui.motion import set_reduced_motion
from ui.responsive import CoalescedCall, WrapLayout
from ui.scaling import apply_interface_scale
from ui.tokens import px
from tests.test_interface_scaling import FakeScreen
from tests.test_task_manager import wait_until
from tests.qt_scale_support import flush_layout, reset_interface_scaling


class ResponsiveLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        self.temporary = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temporary.name) / 'config.json')
        self.config.config['appearance']['reduce_motion'] = True
        self.config.save_config()
        if sys.platform == 'win32':
            native = patch('qframelesswindow.windows.win32gui.SetWindowPos')
            native.start()
            self.addCleanup(native.stop)
        self.window = MainWindow(self.config, network_time=False)
        self.window.show()
        QTest.qWait(120)
        self.page = self.window.workspace_page

    def tearDown(self):
        self.config.config['appearance']['ui_scale'] = '1.0'
        apply_interface_scale(self.app, FakeScreen(), '1.0')
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater()
        QTest.qWait(40)
        set_reduced_motion(False)
        self.temporary.cleanup()

    def resize(self, width, height=600):
        self.window.resize(width, height)
        flush_layout(self.app, self.window)

    def assert_layout_inside(self, layout):
        bounds = layout.geometry()
        occupied = []
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget()
            if item.spacerItem() is not None or (widget is not None and widget.isHidden()):
                continue
            rect = item.geometry()
            if rect.isEmpty():
                continue
            self.assertTrue(bounds.contains(rect), (type(widget).__name__, bounds.getRect(), rect.getRect()))
            for other in occupied:
                self.assertFalse(rect.intersects(other), (rect.getRect(), other.getRect()))
            occupied.append(rect)

    def assert_reachable(self, area, widget):
        area.ensureWidgetVisible(widget, 0, 0)
        QTest.qWait(30)
        point = widget.mapTo(area.viewport(), widget.rect().center())
        self.assertTrue(area.viewport().rect().contains(point), (widget.objectName(), point, area.viewport().size()))

    def task(self, status='waiting'):
        return dict(local_id='responsive-task', product='响应式夹具', prompt_name='响应式提示词',
                    prompt_path=str(Path(self.temporary.name) / 'fixture.txt'), status=status, images=[])

    def test_cards_wrap_and_stretch_at_four_window_widths(self):
        for width in (960, 1280, 1920, 3840):
            with self.subTest(width=width):
                self.resize(width, 900)
                summary = self.page.summary
                layout = summary.metrics_row
                available = layout.geometry().width()
                expected = min(4, max(1, (available + layout.spacing()) // (px(220) + layout.spacing())))
                expected = 2 if expected == 3 else expected
                self.assertEqual(layout.columns, expected)
                self.assert_layout_inside(layout)
                widths = [card.width() for card in summary.stat_cards]
                self.assertLessEqual(max(widths) - min(widths), 1)
                self.assertEqual(len({card.y() for card in summary.stat_cards}), (4 + expected - 1) // expected)
                self.assertLessEqual(abs(sum(widths[:expected]) + layout.spacing() * (expected - 1) - available), 1)

    def test_minimum_window_and_large_zoom_preserve_wrapped_actions(self):
        self.config.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        self.resize(960)
        self.assertEqual(self.window.size(), QSize(960, 600))
        self.assertTrue(self.window.navigationInterface.panel.isCollapsed())
        for layout in (self.page.toolbar.layout(), self.page.directory_bar.layout(),
                       self.page.task_monitor.layout(), self.page.summary.metrics_row):
            self.assert_layout_inside(layout)
        for widget in (self.page.start_button, self.page.pause_button, self.page.directory_bar.match_button,
                       self.page.queue_panel.more_button, self.page.queue_panel.empty_button):
            self.assertTrue(widget.isVisible())
            self.assert_reachable(self.page.body_scroll, widget)

    def test_empty_state_scales_and_remains_centered(self):
        icon = self.page.queue_panel.empty_icon
        self.config.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        self.resize(960)
        self.assertEqual(icon.size(), QSize(77, 77))
        host = self.page.queue_panel.empty_panel
        self.assertLessEqual(abs(icon.geometry().center().x() - host.rect().center().x()), 1)
        self.assertTrue(host.rect().contains(self.page.queue_panel.empty_button.geometry()))

    def test_queue_scroll_preserves_readable_columns_and_reaches_last_action(self):
        panel = self.page.queue_panel
        panel.update_tasks([self.task('submission_unknown')])
        self.resize(960)
        scrollbar = panel.table_scroll.horizontalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        self.assertGreaterEqual(panel._widths[1], px(190))
        scrollbar.setValue(scrollbar.maximum())
        QTest.qWait(80)
        button = panel.rows[0].more_button
        self.assertTrue(panel.table_scroll.viewport().rect().contains(button.mapTo(panel.table_scroll.viewport(), button.rect().center())))
        row = panel.rows[0]
        for column in range(len(panel._widths)):
            header = panel._header_grid.cellRect(0, column)
            cell = row.grid.cellRect(0, column)
            self.assertEqual(panel._header_host.mapTo(panel.surface, header.topLeft()).x(),
                             row.host.mapTo(panel.surface, cell.topLeft()).x())

    def test_resize_burst_coalesces_columns_without_rebuilding_rows_or_styles(self):
        panel = self.page.queue_panel
        panel.update_tasks([self.task()])
        QTest.qWait(100)
        row = panel.rows[0]
        with patch.object(panel._resize_relayout, 'callback', wraps=panel._apply_column_widths) as reflow, \
                patch.object(panel, 'update_tasks', wraps=panel.update_tasks) as rebuild, \
                patch.object(panel, 'setStyleSheet', wraps=panel.setStyleSheet) as restyle, \
                patch('ui.pages.workspace_page.StoryboardMatcher.scan_and_match') as scan:
            for width in (1280, 1400, 1500, 1600, 1920):
                self.window.resize(width, 800)
            QTest.qWait(100)
            self.assertGreater(reflow.call_count, 0)
            self.assertLessEqual(reflow.call_count, 2)
            rebuild.assert_not_called()
            restyle.assert_not_called()
            scan.assert_not_called()
        self.assertIs(panel.rows[0], row)

    def test_main_native_frame_refresh_is_coalesced(self):
        QTest.qWait(100)
        with patch.object(self.window, '_refresh_native_window_frame') as native:
            for width in (1300, 1400, 1500, 1600, 1700):
                self.window.resize(width, 800)
            QTest.qWait(100)
            native.assert_called_once()

    def test_history_actions_and_selection_remain_reachable_at_large_zoom(self):
        history = self.window.history_page
        history.update_history([self.task('failed')])
        self.window.switchTo(history)
        self.config.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        self.resize(960)
        self.assertEqual(history.table.horizontalHeaderItem(0).text(), '')
        header = history.table.horizontalHeader()
        self.assertEqual(header.checkBox.text(), '')
        self.assertGreaterEqual(header.checkBox.x(), header.sectionViewportPosition(0))
        self.assertLessEqual(header.checkBox.geometry().right(), header.sectionViewportPosition(0) + header.sectionSize(0) - 1)
        self.assertGreater(history.table.horizontalScrollBar().maximum(), 0)
        for widget in (history.filter_box, history.open_button, history.batch_resolve_button,
                       history.delete_button, history.clean_cache_button, header.checkBox):
            self.assert_reachable(history.body_scroll, widget)

    def test_history_resize_does_not_rebuild_records(self):
        history = self.window.history_page
        history.update_history([self.task('failed')])
        self.window.switchTo(history)
        QTest.qWait(80)
        with patch.object(history, 'update_history') as rebuild, \
                patch.object(history._column_relayout, 'callback', wraps=history._fit_columns) as reflow:
            for width in (960, 1280, 1920, 3840):
                self.window.resize(width, 800)
            QTest.qWait(100)
            rebuild.assert_not_called()
            self.assertLessEqual(reflow.call_count, 2)

    def test_history_resize_preserves_selection_width_and_alignment(self):
        history = self.window.history_page
        history.update_history([self.task('failed')])
        self.window.switchTo(history)
        self.resize(1400, 800)
        header_checkbox = history.selection_header.checkBox
        row_checkbox = history.table.cellWidget(0, 0).findChild(type(header_checkbox))
        for column_width, window_width in ((160, 1200), (48, 1000), (92, 1280)):
            with self.subTest(column_width=column_width, window_width=window_width):
                history.table.setColumnWidth(0, column_width)
                self.resize(window_width, 800)
                self.assertEqual(history.table.columnWidth(0), column_width)
                self.assertLessEqual(abs(header_checkbox.mapToGlobal(header_checkbox.rect().center()).x()
                                         - row_checkbox.mapToGlobal(row_checkbox.rect().center()).x()), 1)

    def test_settings_subpages_scroll_to_all_visible_actions(self):
        settings = self.window.settings_page
        self.window.switchTo(settings)
        self.config.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        self.resize(960)
        for key in settings._page_order:
            with self.subTest(page=key):
                settings._switch_page(key)
                QTest.qWait(60)
                area, content = settings._pages[key]
                for button in content.findChildren(QAbstractButton):
                    if button.isVisible() and button.width() > 0:
                        self.assert_reachable(area, button)
                        parent = button.parentWidget()
                        self.assertTrue(parent.rect().contains(button.geometry()),
                                        (key, button.text(), parent.size(), button.geometry().getRect()))

    def test_sync_controls_and_scale_card_fit_narrow_settings(self):
        settings = self.window.settings_page
        self.window.switchTo(settings)
        self.config.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        self.resize(960)
        for key, card, controls in (
                ('sync', settings.repo_sync_card, (settings.repo_root_edit, settings.repo_select_button,
                                                  settings.sync_button, settings.repo_status_label)),
                ('appearance', settings.ui_scale_card, (settings.ui_scale, settings.ui_scale_status))):
            with self.subTest(page=key):
                settings._switch_page(key)
                QTest.qWait(80)
                for widget in controls:
                    self.assertTrue(card.rect().contains(widget.mapTo(card, widget.rect().topLeft())))
                    self.assertTrue(card.rect().contains(widget.mapTo(card, widget.rect().bottomRight())),
                                    (widget.__class__.__name__, card.size(), widget.geometry().getRect()))

    def test_setting_card_headers_and_controls_use_nonoverlapping_hosts(self):
        settings = self.window.settings_page
        self.window.switchTo(settings)
        self.config.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        self.resize(960)
        for key in settings._page_order:
            with self.subTest(page=key):
                settings._switch_page(key)
                QTest.qWait(60)
                _area, content = settings._pages[key]
                for card in content.findChildren(QWidget):
                    if not hasattr(card, '_heading'):
                        continue
                    for label in (card.iconLabel, card.titleLabel, card.contentLabel):
                        self.assertIs(label.parentWidget(), card._heading)
                        if label.isVisible():
                            bounds = QRect(label.mapTo(card, QPoint()), label.size())
                            self.assertTrue(card.rect().contains(bounds), (key, bounds, card.rect()))
                    self.assertFalse(card._heading.geometry().intersects(card._responsive_controls.geometry()))
                    for index in range(card._responsive_controls.layout().count()):
                        control = card._responsive_controls.layout().itemAt(index).widget()
                        if control is not None:
                            self.assertIs(control.parentWidget(), card._responsive_controls)

    def test_details_fields_and_actions_wrap_and_scroll_at_large_zoom(self):
        self.config.config['appearance']['ui_scale'] = '1.6'
        self.window.apply_appearance()
        self.resize(960)
        task = self.task('submission_unknown')
        task['model'] = 'grok-imagine-1.5-video'
        self.page.current_task.update_task(0, task)
        self.page.open_controls()
        dialog = self.page.controls_dialog
        dialog.resize(700, 600)
        QTest.qWait(120)
        area = self.page.center_scroll
        content = area.widget()
        self.assertLessEqual(content.width(), area.viewport().width())
        for layout in content.findChildren(WrapLayout):
            self.assert_layout_inside(layout)
        params = self.page.params_card
        for widget in (self.page.current_task.resolve_button, *self.page.data_source.choose_buttons,
                       params.expand_button, params.model, params.ratio, params.resolution, params.duration,
                       params.audio, params.poll, params.retries, params.threshold, params.seed):
            self.assertTrue(widget.isVisible())
            self.assert_reachable(area, widget)
            self.assertTrue(widget.parentWidget().rect().contains(widget.geometry()),
                            (type(widget).__name__, widget.geometry().getRect(), widget.parentWidget().size()))
        self.assertGreater(len({control.parentWidget().y() for control in
                                (params.model, params.ratio, params.resolution, params.duration)}), 1)
        params.toggle_advanced()
        QTest.qWait(40)
        self.assertEqual(params._advanced_animation.duration(), 0)
        self.assertFalse(params.advanced.isVisible())
        params.toggle_advanced()
        QTest.qWait(40)
        self.assertTrue(params.advanced.isVisible())
        self.assert_reachable(area, params.seed)
        dialog.close()

    def test_screen_and_dpi_change_reflow_cards_without_changing_opacity(self):
        self.resize(1280, 900)
        opacity = self.window.windowOpacity()
        screen = FakeScreen(96, 3840, 2160)
        self.window._on_screen_changed(screen)
        QTest.qWait(100)
        self.assertEqual(self.window._interface_factors['z'], 1.6)
        self.assert_layout_inside(self.page.summary.metrics_row)
        screen.dpi = 192
        screen.screen_size = QSize(1920, 1080)
        screen.logicalDotsPerInchChanged.emit(192)
        QTest.qWait(100)
        self.assertEqual(self.window._interface_factors['z'], 1)
        self.assertEqual(self.window.windowOpacity(), opacity)
        self.assert_layout_inside(self.page.summary.metrics_row)

    def test_minimize_restore_and_maximize_preserve_opacity_and_controls(self):
        opacity = self.window.windowOpacity()
        self.window.showMinimized()
        QTest.qWait(40)
        self.window.showNormal()
        self.resize(960)
        self.assertEqual(self.window.windowOpacity(), opacity)
        self.window._studio_toggle_maximized()
        QTest.qWait(180)
        self.window._studio_toggle_maximized()
        QTest.qWait(180)
        self.assertEqual(self.window.windowOpacity(), opacity)
        self.assertTrue(self.page.start_button.isVisible())
        self.assert_reachable(self.page.body_scroll, self.page.start_button)

    def test_reduced_motion_applies_to_navigation_and_log_resize(self):
        self.assertEqual(self.window.navigationInterface.panel.expandAni.duration(), 0)
        self.page.log_drawer.toggle()
        QTest.qWait(40)
        self.assertEqual(self.page.log_drawer._animation.duration(), 0)
        self.assertFalse(self.page.log_drawer.browser.isVisible())
        self.resize(960)
        self.assertGreaterEqual(self.page.log_drawer.height(), self.page.log_drawer._header_height())
        self.config.config['appearance']['reduce_motion'] = False
        self.window.apply_appearance()
        self.assertGreater(self.window.navigationInterface.panel.expandAni.duration(), 0)


class CoalescedLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_coalesced_callback_runs_once_per_burst_and_can_run_again(self):
        parent = QWidget()
        callback = Mock()
        call = CoalescedCall(parent, callback, 20)
        for _index in range(10):
            call.schedule()
        QTest.qWait(50)
        callback.assert_called_once()
        call.schedule()
        QTest.qWait(50)
        self.assertEqual(callback.call_count, 2)
        parent.deleteLater()
        QTest.qWait(10)

    def test_hidden_control_does_not_leave_a_wrap_gap(self):
        parent = QWidget()
        layout = WrapLayout(parent)
        first, hidden, last = QWidget(), QWidget(), QWidget()
        for widget in (first, hidden, last):
            widget.setMinimumSize(100, 30)
            layout.addWidget(widget)
        hidden.hide()
        parent.resize(220, 100)
        parent.show()
        QTest.qWait(30)
        self.assertEqual(first.y(), last.y())
        self.assertGreater(last.x(), first.geometry().right())
        parent.close()
        parent.deleteLater()
        QTest.qWait(10)

    def test_pending_callback_does_not_retain_its_owner(self):
        class Receiver:
            def refresh(self):
                raise AssertionError('destroyed owner must not receive a layout callback')

        parent = QWidget()
        receiver = Receiver()
        reference = weakref.ref(receiver)
        call = CoalescedCall(parent, receiver.refresh, 20)
        call.schedule()
        del receiver
        self.assertIsNone(reference())
        QTest.qWait(50)
        self.assertFalse(call.timer.isActive())
        parent.deleteLater()
        QTest.qWait(10)
