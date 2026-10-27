"""Theme, responsive generation layouts and page-switch performance gates."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt5.QtCore import QCoreApplication, QEvent, QPoint, QRectF, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import QApplication, QBoxLayout, QFrame
from qfluentwidgets import SettingCard, ToolButton

from core import i18n
from core.config_manager import ConfigManager
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from ui import materials, palettes
from ui.components.platform_preview_dialog import PlatformPreviewDialog
from ui.pages.video_creation_page import _HistoryRow
from ui.theme import apply_ui_mode
from ui.tokens import px


class ThemeConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        i18n.set_language('zh-CN')
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.load_config()
        from ui.main_window import MainWindow
        self.window = MainWindow(self.config, network_time=False, model_sync=False)
        self.window.resize(1300, 900)
        self.window.show()
        QTest.qWait(150)
        flush_layout(self.app, self.window)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        i18n.set_language('zh-CN')
        reset_interface_scaling(self.app)
        apply_ui_mode('light')
        self.temp.cleanup()

    @property
    def pages(self):
        w = self.window
        return [w.workspace_page, w.image_generation_page, w.video_creation_page,
                w.ecommerce_page, w.history_page, w.settings_page]

    def test_auto_reads_windows_preference_and_falls_back_to_light(self):
        import winreg
        for value, expected in [(0, 'dark'), (1, 'light')]:
            with patch.object(winreg, 'OpenKey'), patch.object(winreg, 'QueryValueEx', return_value=(value, 4)):
                self.assertEqual(palettes.resolve('auto'), expected)
                apply_ui_mode('auto')
                self.assertEqual(materials.THEME_ID, expected)
        with patch.object(winreg, 'OpenKey', side_effect=OSError('registry unavailable')):
            self.assertEqual(palettes.resolve('auto'), 'light')
        values, labels = palettes.combo_choices()
        self.assertEqual(labels[values.index('auto')], '跟随系统')

    def test_all_pages_and_preview_use_current_surface_and_input_tokens(self):
        w = self.window
        w.ecommerce_page.task_list.update_groups([dict(
            group_id='theme-group', product_name='主题测试', status='completed',
            created_at='2026-10-26 12:00', platforms=[], config={}, manifest={}, thumbs=[])])
        dialog = PlatformPreviewDialog(w, dict(product_name='主题测试', platforms=[], config={}, manifest={}))
        try:
            shell = dialog.shell
            for mode in ('light', 'dark', 'light'):
                apply_ui_mode(mode)
                image, video = w.image_generation_page, w.video_creation_page
                self.assertIn(materials.ecommerce_color('image_generation_input'), image.prompt_edit.styleSheet())
                self.assertIn(materials.ecommerce_color('image_generation_surface'), image.form_card.styleSheet())
                self.assertIn(materials.ecommerce_color('video_creation_input'), video.prompt_edit.styleSheet())
                settings_card = video.findChild(QFrame, 'videoCreationSettingsCard')
                self.assertIn(materials.ecommerce_color('video_creation_surface'), settings_card.styleSheet())
                self.assertIn(materials.ecommerce_color('video_creation_preview_bg'), video.preview_area.styleSheet())
                self.assertIn(materials.ecommerce_color('preview_canvas'), dialog.canvas.styleSheet())
                self.assertIn(materials.ecommerce_color('preview_sidebar_bg'), dialog.selection_panel.styleSheet())
                self.assertIn(materials.ecommerce_color('preview_card'), dialog.shell.nav_bar.styleSheet())
                self.assertIs(dialog.shell, shell, 'theme changes must refresh the shell in place')
                for page in self.pages:
                    self.assertTrue(page.property('isStackedTransparent'), page.objectName())
                self.assertEqual(w.stackedWidget.styleSheet(),
                                 'StackedWidget {background:transparent; border:0;}')
                card = w.ecommerce_page.task_list.card_for('theme-group')
                self.assertIn(materials.palette()['text1'], card.name_label.styleSheet())
                inactive = w.ecommerce_page.task_list.tab_buttons['generating']
                self.assertIn(materials.ecommerce_color('single_card'), inactive.styleSheet())
                colors = [w.history_page._type_colors({'kind': kind})
                          for kind in ('ec', 'manual_image', 'manual_video', 'video')]
                self.assertTrue(all(color == colors[0] for color in colors))
                expected = materials.ecommerce_color('image_generation_input')
                self.assertNotEqual(expected.upper(), '#000000' if mode == 'light' else '#FFFFFF')
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_prompt_colors_change_immediately_and_survive_language_switch(self):
        image, video = self.window.image_generation_page, self.window.video_creation_page
        apply_ui_mode('light')
        old = [page.prompt_edit.styleSheet() for page in (image, video)]
        apply_ui_mode('dark')
        new = [page.prompt_edit.styleSheet() for page in (image, video)]
        self.assertTrue(all(a != b for a, b in zip(old, new)))
        i18n.set_language('English')
        self.assertEqual([page.prompt_edit.styleSheet() for page in (image, video)], new)
        self.assertEqual(self.window.history_page.type_filter_box.itemText(3), 'Video creation')
        self.assertEqual(self.window.history_page.type_filter_box.itemText(4), 'Ecommerce Image Maker')

    def test_tall_generation_forms_keep_compact_control_spacing(self):
        w = self.window
        for page in (w.image_generation_page, w.video_creation_page):
            w.switchTo(page)
            offsets = []
            for height in (900, 1400):
                w.resize(1600, height)
                flush_layout(self.app, w)
                offsets.append(page.prompt_edit.mapTo(page.body_content, QPoint()).y())
            self.assertLessEqual(abs(offsets[1] - offsets[0]), px(2), (page.objectName(), offsets))
        video = w.video_creation_page
        self.assertLessEqual(video.model_combo.y() - video.model_label.geometry().bottom(), px(14))

    def test_native_settings_cards_do_not_add_backing_store_shadow(self):
        cards = self.window.settings_page.findChildren(SettingCard)
        self.assertTrue(cards)
        for card in cards:
            self.assertIsNone(card.graphicsEffect(), card.objectName())

    def test_workspace_text_survives_deferred_theme_polish(self):
        w = self.window
        for mode in ('light', 'dark', 'light'):
            w.switchTo(w.settings_page)
            apply_ui_mode(mode)
            w.switchTo(w.workspace_page)
            flush_layout(self.app, w)
            label = w.workspace_page.title_label
            expected = QColor(materials.map_text_color('#F4F5F7'))
            self.assertEqual(label.palette().color(QPalette.WindowText), expected)
            self.assertEqual(label.lightColor, QColor(materials.TEXT_COLOR_MAP['#F4F5F7']))
            self.assertEqual(label.darkColor, QColor('#F4F5F7'))

    def test_video_preview_contrast_and_history_icons_survive_deferred_polish(self):
        w = self.window
        video = w.video_creation_page
        row = _HistoryRow({'local_id': 'contrast', 'status': 'succeeded'})
        row.resize(640, 80)
        row.show()
        try:
            for mode in ('light', 'dark', 'light'):
                w.switchTo(w.settings_page)
                apply_ui_mode(mode)
                w.switchTo(video)
                for button in (video.open_video_button, video.retry_button):
                    video.preview_stack.setCurrentWidget(button.parentWidget())
                    button.show()
                    flush_layout(self.app, w)
                    self.assertEqual(button.palette().color(QPalette.ButtonText),
                                     QColor(materials.ecommerce_color('video_creation_preview_text')))
                row._refresh_style()
                for button in row.findChildren(ToolButton):
                    rectangles = []
                    original = button._drawIcon
                    def draw_icon(icon, painter, rect, *args):
                        # Copy only geometry: retaining a native QPainter in a
                        # Mock past paintEvent corrupts its paint-device lifetime.
                        rectangles.append(QRectF(rect))
                        return original(icon, painter, rect, *args)
                    with patch.object(button, '_drawIcon', new=draw_icon):
                        button.grab()
                    self.assertTrue(rectangles, button.objectName())
                    rect = rectangles[-1]
                    self.assertTrue(QRectF(button.rect()).contains(rect), (button.objectName(), rect))
                    self.assertAlmostEqual(rect.center().x(), button.width() / 2, delta=1)
                    self.assertAlmostEqual(rect.center().y(), button.height() / 2, delta=1)
        finally:
            row.close()
            row.deleteLater()

    def test_navigation_logs_first_frame_after_paint_at_debug_level(self):
        with self.assertLogs('ui.main_window', level='DEBUG') as logs:
            self.window.switchTo(self.window.image_generation_page)
            flush_layout(self.app, self.window)
        self.assertTrue(any('页面首帧 imageGenerationPage:' in message for message in logs.output), logs.output)
        self.assertIsNone(self.window._first_frame_target)

    def test_generation_columns_at_1300_and_four_interface_scales(self):
        w = self.window
        for scale in ('1.0', '1.25', '1.5', '1.75'):
            self.config.config['appearance']['ui_scale'] = scale
            w._apply_interface_scale()
            # Preserve the two-column canvas even when navigation reduces the viewport.
            w.resize(px(1300), px(900))
            for page in (w.image_generation_page, w.video_creation_page):
                w.switchTo(page)
                flush_layout(self.app, w)
                self.assertEqual(page.body_layout.direction(), QBoxLayout.LeftToRight, (scale, page.objectName()))
                self.assertEqual(page.body_scroll.horizontalScrollBar().maximum(),
                                 max(0, page.body_content.width() - page.body_scroll.viewport().width()))
            self.assertEqual(w.image_generation_page.right_column.width(), px(480))
            settings_card = w.video_creation_page.findChild(QFrame, 'videoCreationSettingsCard')
            self.assertEqual(settings_card.width(), px(372))
        self.config.config['appearance']['ui_scale'] = '1.0'
        w._apply_interface_scale()
        w.resize(1000, 900)
        for page in (w.image_generation_page, w.video_creation_page):
            w.switchTo(page)
            flush_layout(self.app, w)
            self.assertEqual(page.body_layout.direction(), QBoxLayout.LeftToRight)
            self.assertGreater(page.body_scroll.horizontalScrollBar().maximum(), 0)
            page.body_scroll.ensureWidgetVisible(page.right_column, 0, 0)
            self.app.processEvents()
            self.assertGreater(page.body_scroll.horizontalScrollBar().value(), 0)
        self.assertEqual(w.image_generation_page.reference_grid.columns, 4)

    def test_repeated_page_switches_and_language_theme_do_not_rebuild_lists(self):
        w = self.window
        self.assertTrue(w.image_generation_page._quick_prompts_dirty)
        self.assertTrue(w.video_creation_page._history_dirty)
        for page in self.pages:
            w.switchTo(page)
            self.app.processEvents()
        with patch.object(w.history_page, 'refresh_records', wraps=w.history_page.refresh_records) as history, \
                patch.object(w.video_creation_page, '_render_history', wraps=w.video_creation_page._render_history) as video, \
                patch.object(w.ecommerce_page.task_list, '_render', wraps=w.ecommerce_page.task_list._render) as tasks:
            for _ in range(3):
                for page in self.pages:
                    w.switchTo(page)
                    self.app.processEvents()
            apply_ui_mode('dark')
            i18n.set_language('English')
            history.assert_not_called()
            video.assert_not_called()
            tasks.assert_not_called()
        self.assertFalse(w.image_generation_page._quick_prompts_dirty)
        self.assertFalse(w.video_creation_page._history_dirty)

    def test_switch_and_theme_latency_without_growth(self):
        # QApplication palettes and Fluent's global style registry include
        # standalone controls left alive by other test fixtures. Benchmark one
        # real app in a fresh interpreter so suite order cannot change its load.
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', '-m', 'tests.theme_performance_probe'],
            cwd=Path(__file__).resolve().parent.parent,
            env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
            capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('SEG10_GATE', result.stdout)
        print(result.stdout.strip())

    def measure_switch_and_theme_latency_without_growth(self):
        """Same interaction counts and budgets, called by the isolated probe."""
        samples = []
        # First show is included: lazy lists must still fit the click budget.
        for _ in range(20):
            for page in self.pages:
                start = time.perf_counter()
                self.window.switchTo(page)
                self.app.processEvents()
                samples.append((time.perf_counter() - start) * 1000)
        self.assertLess(max(samples), 250, samples)
        self.assertLess(statistics.mean(samples[-20:]), statistics.mean(samples[:20]) + 50)
        start = time.perf_counter()
        for mode in ('dark', 'light', 'dark', 'light', 'dark'):
            apply_ui_mode(mode)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2.0)
        print('SEG10_GATE switch_mean_ms=%.2f switch_max_ms=%.2f theme_5_ms=%.2f' %
              (statistics.mean(samples), max(samples), elapsed * 1000))


if __name__ == '__main__':
    unittest.main()
