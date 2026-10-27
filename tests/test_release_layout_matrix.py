"""Release geometry checks: preserve columns, font size and scroll access."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import (
    QApplication, QBoxLayout, QGridLayout, QLabel, QStyle, QStyleOptionButton,
    QVBoxLayout, QWidget,
)

from tests import test_image_generation_page as images
from tests import test_video_creation_page as videos
from tests import test_ecommerce_single_layout as ecommerce
from tests import test_ecommerce_batch_page as batch
from tests import test_history_page as history
from tests import test_settings_card_layout as settings
from tests import test_platform_preview_dialog as platform_preview
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from tests.test_interface_scaling import FakeScreen
from ui.components.custom_widgets import ensure_ui_font
from ui.components.image_upload_grid import ImageUploadGrid
from ui.fonts import resolve_font_families
from ui.scaling import apply_interface_scale
from ui.theme import apply_ui_mode
from ui.tokens import px

SIZES = ((1366, 768), (1280, 800), (1600, 900), (1920, 1080), (1024, 700))
SCALES = ('1.0', '1.25', '1.5', '1.75')


class ReleaseLayoutMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        ensure_ui_font()

    def _matrix(self, fixture_class, kind):
        fixture_class.setUpClass()
        fixture = fixture_class()
        fixture.setUp()
        page = fixture.page
        try:
            for scale in SCALES:
                reset_interface_scaling(self.app)
                apply_interface_scale(self.app, FakeScreen(), scale)
                for theme in ('light', 'dark'):
                    apply_ui_mode(theme)
                    (page._refresh_colors if kind == 'ecommerce' else page.refresh_colors)()
                    for size in SIZES:
                        with self.subTest(page=kind, scale=scale, theme=theme, size=size):
                            page.resize(*size)
                            flush_layout(self.app, page)
                            if kind == 'ecommerce':
                                layout = page.single_columns_layout
                                left, right = page.body_scroll, page.result_scroll
                                scroll = page.single_columns_scroll
                                target = page.empty_title_label
                                self.assertAlmostEqual(left.width(), right.width(), delta=2)
                                self.assertEqual(page.analysis_model_picker.x(), page.image_model_picker.x())
                                self.assertEqual(page.analysis_model_picker.width(), page.image_model_picker.width())
                            else:
                                layout = page.body_layout
                                left, right = page.left_column, page.right_column
                                scroll = page.body_scroll
                                target = page.cta_button if kind == 'image' else page.preview_area
                                if kind == 'image':
                                    self.assertEqual(right.width(), px(480))
                                    self.assertGreaterEqual(left.width(), px(640))
                                else:
                                    self.assertEqual(left.width(), px(372))
                                    self.assertGreaterEqual(right.width(), px(640))
                            self.assertEqual(layout.direction(), QBoxLayout.LeftToRight)
                            self.assertLess(left.geometry().right(), right.x())
                            self.assertAlmostEqual(left.y(), right.y(), delta=1)
                            self.assertTrue(left.isVisible())
                            self.assertTrue(right.isVisible())
                            # Reach the right column without moving it below the form.
                            scroll.ensureWidgetVisible(target, 0, 0)
                            flush_layout(self.app, page)
                            center = target.mapTo(scroll.viewport(), target.rect().center())
                            self.assertGreaterEqual(center.x(), 0)
                            self.assertLess(center.x(), scroll.viewport().width())
                            overflow = scroll.widget().width() - scroll.viewport().width()
                            self.assertEqual(scroll.horizontalScrollBar().maximum(), max(0, overflow))
        finally:
            fixture.tearDown()
            reset_interface_scaling(self.app)

    def test_image_columns_all_sizes_scales_and_themes(self):
        self._matrix(images.ImageGenerationPageTests, 'image')

    def test_video_columns_all_sizes_scales_and_themes(self):
        self._matrix(videos.VideoCreationPageTests, 'video')

    def test_ecommerce_columns_all_sizes_scales_and_themes(self):
        self._matrix(ecommerce.EcommerceSingleLayoutTests, 'ecommerce')

    def test_batch_shared_defaults_and_rows_remain_stacked_in_matrix(self):
        batch._BatchCase.setUpClass()
        fixture = batch._BatchCase()
        fixture.setUp()
        page = fixture.page
        page.show()
        try:
            shared_panel = page._body_layout.itemAt(0).widget()
            rows_panel = page._body_layout.itemAt(1).widget()
            for scale in SCALES:
                reset_interface_scaling(self.app)
                apply_interface_scale(self.app, FakeScreen(), scale)
                page.refresh_interface_scale()
                for theme in ('light', 'dark'):
                    apply_ui_mode(theme)
                    page.refresh_colors()
                    for size in SIZES:
                        with self.subTest(page='batch', scale=scale, theme=theme, size=size):
                            page.resize(*size)
                            flush_layout(self.app, page)
                            self.assertEqual(page._body_layout.direction(), QBoxLayout.TopToBottom)
                            self.assertLess(shared_panel.y(), rows_panel.y())
                            self.assertLess(shared_panel.geometry().bottom(), rows_panel.y())
                            for panel in (shared_panel, rows_panel):
                                self.assertTrue(page.body_content.rect().contains(panel.geometry()))
                            page.body_scroll.ensureWidgetVisible(page.rows[0], 0, 0)
                            flush_layout(self.app, page)
                            row_center = page.rows[0].mapTo(
                                page.body_scroll.viewport(), page.rows[0].rect().center())
                            self.assertGreaterEqual(row_center.y(), 0)
                            self.assertLess(row_center.y(), page.body_scroll.viewport().height())
        finally:
            fixture.tearDown()
            reset_interface_scaling(self.app)
            apply_ui_mode('light')

    def test_history_filters_and_table_stay_reachable_in_matrix(self):
        history.HistoryPageManualImageTests.setUpClass()
        fixture = history.HistoryPageManualImageTests()
        fixture.setUp()
        page = fixture.page
        page.show()
        try:
            for scale in SCALES:
                reset_interface_scaling(self.app)
                apply_interface_scale(self.app, FakeScreen(), scale)
                page.refresh_interface_scale()
                for theme in ('light', 'dark'):
                    apply_ui_mode(theme)
                    page.refresh_colors()
                    for size in SIZES:
                        with self.subTest(page='history', scale=scale, theme=theme, size=size):
                            page.resize(*size)
                            flush_layout(self.app, page)
                            self.assertTrue(page.filter_box.isVisible())
                            self.assertTrue(page.type_filter_box.isVisible())
                            self.assertTrue(page.table.isVisible())
                            self.assertGreaterEqual(page.table.width(), 1)
                            self.assertLessEqual(page.table.geometry().right(),
                                                 page.body_content.width())
                            page.body_scroll.ensureWidgetVisible(page.table, 0, 0)
                            flush_layout(self.app, page)
                            table_center = page.table.mapTo(
                                page.body_scroll.viewport(), page.table.rect().center())
                            self.assertGreaterEqual(table_center.y(), 0)
                            self.assertLess(table_center.y(), page.body_scroll.viewport().height())
                            self.assertGreaterEqual(page.table.horizontalScrollBar().maximum(), 0)
        finally:
            fixture._cleanup_page()
            fixture.temp.cleanup()
            reset_interface_scaling(self.app)
            apply_ui_mode('light')

    def test_settings_rail_and_content_remain_separate_in_matrix(self):
        settings.SettingsResultBarTests.setUpClass()
        fixture = settings.SettingsResultBarTests()
        fixture.setUp()
        page = fixture.page
        try:
            for scale in SCALES:
                reset_interface_scaling(self.app)
                apply_interface_scale(self.app, FakeScreen(), scale)
                for theme in ('light', 'dark'):
                    apply_ui_mode(theme)
                    page._switch_page('appearance', highlight=True)
                    for size in SIZES:
                        with self.subTest(page='settings', scale=scale, theme=theme, size=size):
                            fixture.root.resize(*size)
                            flush_layout(self.app, fixture.root)
                            self.assertEqual(page.page_stack.currentWidget(),
                                             page._pages['appearance'][0])
                            self.assertEqual(page.rail_scroll.width(), px(196))
                            self.assertLess(page.rail_scroll.geometry().right(),
                                            page.page_stack.x())
                            self.assertGreaterEqual(page.page_stack.width(), 1)
                            area, content = page._pages['appearance']
                            self.assertTrue(area.isVisible())
                            self.assertTrue(content.isVisible())
                            self.assertLessEqual(area.width(), page.page_stack.width())
                            area.ensureWidgetVisible(content, 0, 0)
                            flush_layout(self.app, fixture.root)
                            content_center = content.mapTo(area.viewport(), content.rect().center())
                            self.assertGreaterEqual(content_center.x(), 0)
                            self.assertLess(content_center.x(), area.viewport().width())
        finally:
            fixture.tearDown()
            apply_ui_mode('light')

    def test_platform_preview_shells_resize_with_parent_across_scale_and_theme(self):
        platform_preview.PlatformPreviewDialogTestBase.setUpClass()
        for scale in ('1.0', '1.75'):
            for theme in ('light', 'dark'):
                reset_interface_scaling(self.app)
                apply_interface_scale(self.app, FakeScreen(), scale)
                apply_ui_mode(theme)
                fixture = platform_preview.PlatformPreviewDialogTestBase()
                fixture.setUp()
                parent = fixture.parent_window()
                dialog = fixture.dialog(parent=parent, show=True)
                try:
                    for size in ((1366, 768), (1024, 700)):
                        parent.resize(*size)
                        flush_layout(self.app, dialog)
                        self.assertEqual(dialog.size(), parent.size())
                        for platform_index in (0, 1):
                            with self.subTest(scale=scale, theme=theme, size=size,
                                              platform=platform_index):
                                dialog.set_platform_index(platform_index)
                                flush_layout(self.app, dialog)
                                shell = dialog.shell
                                margins = dialog.shell_layout.contentsMargins()
                                inner_width = (dialog.shell_host.width()
                                               - margins.left() - margins.right())
                                self.assertAlmostEqual(
                                    shell.x() - margins.left(),
                                    (inner_width - shell.width()) / 2, delta=1)
                                fixture.assert_no_horizontal_overflow(dialog)

                                if dialog.is_mobile:
                                    viewport = fixture.rect_in(shell.scroll.viewport(), shell)
                                    footer = fixture.rect_in(shell.bottom_bar, shell)
                                    self.assertLess(viewport.bottom(), footer.top())
                                    self.assertTrue(shell.rect().contains(footer))
                                    shell.scroll.ensureWidgetVisible(shell.variant_row, 0, px(8))
                                    flush_layout(self.app, dialog)
                                    for chip in shell.variant_chips:
                                        chip_rect = fixture.rect_in(chip, shell.scroll.viewport())
                                        self.assertTrue(
                                            shell.scroll.viewport().rect().contains(chip_rect))
                                        self.assertLessEqual(chip.width(),
                                                             shell.scroll.viewport().width())
                                else:
                                    self.assertEqual(shell.main_layout.direction(),
                                                     QBoxLayout.LeftToRight)
                                    main = shell.main_layout.parentWidget()
                                    self.assertLess(
                                        fixture.rect_in(main, shell).bottom(),
                                        fixture.rect_in(shell.detail_title, shell).top())
                                for child in shell.findChildren(
                                        QWidget, options=Qt.FindDirectChildrenOnly):
                                    if child.isVisible():
                                        self.assertTrue(shell.rect().contains(child.geometry()),
                                                        (child.objectName(), child.geometry()))
                finally:
                    fixture.tearDown()
            reset_interface_scaling(self.app)
        apply_ui_mode('light')


class LateScaleLayoutCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_layouts_created_at_high_scale_keep_logical_baselines(self):
        reset_interface_scaling(self.app)
        apply_interface_scale(self.app, FakeScreen(), '1.75')
        root = QWidget()
        vertical = QVBoxLayout(root)
        vertical.setContentsMargins(*(round(value * 1.75) for value in (12, 8, 12, 8)))
        vertical.setSpacing(round(10 * 1.75))
        vertical.addSpacing(round(20 * 1.75))
        grid_host = QWidget(root)
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(*(round(value * 1.75) for value in (4, 6, 8, 10)))
        grid.setHorizontalSpacing(round(7 * 1.75))
        grid.setVerticalSpacing(round(9 * 1.75))
        grid.addWidget(QLabel('late grid', grid_host), 0, 0)
        vertical.addWidget(grid_host)
        vertical.addWidget(QLabel('late vertical', root))
        root.show()
        try:
            flush_layout(self.app, root)
            self.assertIn(vertical, self.app._interface_scaler.layouts)
            self.assertIn(grid, self.app._interface_scaler.layouts)

            for choice, factor in (('1.0', 1.0), ('1.25', 1.25), ('1.75', 1.75), ('1.0', 1.0)):
                with self.subTest(scale=choice):
                    apply_interface_scale(self.app, FakeScreen(), choice)
                    flush_layout(self.app, root)
                    margins = vertical.contentsMargins()
                    self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()),
                                     tuple(round(value * factor) for value in (12, 8, 12, 8)))
                    self.assertEqual(vertical.spacing(), round(10 * factor))
                    self.assertEqual(vertical.itemAt(0).sizeHint().height(), round(20 * factor))
                    margins = grid.contentsMargins()
                    self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()),
                                     tuple(round(value * factor) for value in (4, 6, 8, 10)))
                    self.assertEqual(grid.horizontalSpacing(), round(7 * factor))
                    self.assertEqual(grid.verticalSpacing(), round(9 * factor))
        finally:
            root.close()
            root.deleteLater()
            reset_interface_scaling(self.app)
            apply_ui_mode('light')

    def test_submit_buttons_have_identical_solid_pixels_and_state(self):
        images.ImageGenerationPageTests.setUpClass()
        fixture = images.ImageGenerationPageTests()
        fixture.setUp()
        page = fixture.page
        try:
            for theme in ('light', 'dark'):
                apply_ui_mode(theme)
                page.refresh_colors()
                for text in ('', '一只陶瓷杯'):
                    page.prompt_edit.setPlainText(text)
                    flush_layout(self.app, page)
                    self.assertEqual(page.submit_button.isEnabled(), bool(text))
                    self.assertEqual(page.cta_button.isEnabled(), bool(text))
                    self.assertEqual(page.submit_button.styleSheet(), page.cta_button.styleSheet())
                    from ui import materials
                    token = 'image_generation_submit' if text else 'image_generation_submit_disabled'
                    expected = QColor(materials.ecommerce_color(token))
                    for button in (page.submit_button, page.cta_button):
                        image = button.grab().toImage()
                        self.assertEqual(image.pixelColor(20, image.height() // 2), expected)
                        self.assertEqual(button.height(), px(44))
                        self.assertEqual(button.iconSize().width(), px(16))
                        option = QStyleOptionButton()
                        button.initStyleOption(option)
                        contents = button.style().subElementRect(
                            QStyle.SE_PushButtonContents, option, button)
                        text = button.fontMetrics().boundingRect(
                            contents, Qt.AlignCenter, button.text())
                        icon_right = (12 + (button.width() - button.minimumSizeHint().width()) // 2
                                      + button.iconSize().width())
                        self.assertLess(icon_right, text.left())
        finally:
            fixture.tearDown()
            apply_ui_mode('light')

    def test_upload_tiles_and_labels_stay_inside_grid_after_scale_and_rebuild(self):
        from core.i18n import set_language
        from PyQt5.QtCore import QEvent, QCoreApplication
        grid = ImageUploadGrid('product', 6, columns=4, uniform=True)
        grid.resize(600, 250)
        grid.show()
        try:
            for scale in SCALES:
                apply_interface_scale(self.app, FakeScreen(), scale)
                grid.resize(px(600), px(250))
                for language in ('zh-CN', 'en-US'):
                    set_language(language)
                    for paths in ([], ['missing.png'] * 4):
                        grid.set_paths(paths)
                        flush_layout(self.app, grid)
                        widgets = grid._tiles + [grid.add_tile()]
                        for tile in widgets:
                            self.assertEqual(tile.width(), px(96))
                            self.assertEqual(tile.width(), tile.height())
                            self.assertTrue(grid.rect().contains(tile.geometry()))
                        tile = grid.add_tile()
                        for label in (tile.plus_label, tile.text_label):
                            self.assertTrue(tile.rect().contains(label.geometry()))
                            text_rect = label.fontMetrics().boundingRect(
                                label.rect(), Qt.AlignCenter | Qt.TextWordWrap, label.text())
                            self.assertLessEqual(text_rect.height(), label.height(),
                                (scale, language, paths, label.text(), label.size(), label.font().toString()))
                        for tile in grid._tiles:
                            self.assertTrue(tile.rect().contains(tile.remove_button.geometry()))
                            self.assertTrue(tile.rect().contains(tile.number_label.geometry()))
                        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        finally:
            grid.close()
            grid.deleteLater()
            set_language('zh-CN')
            reset_interface_scaling(self.app)


class FontFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        ensure_ui_font()

    def test_only_installed_families_are_selected_when_preferred_fonts_missing(self):
        for installed, expected in ((['SimSun'], ['SimSun']),
                                    (['Noto Sans CJK SC'], ['Noto Sans CJK SC']),
                                    (['Segoe UI'], ['Segoe UI'])):
            with self.subTest(installed=installed), patch('ui.fonts.QFontDatabase') as database:
                database.return_value.families.return_value = installed
                self.assertEqual(resolve_font_families(), expected)

    def test_empty_or_unavailable_database_retains_qt_default(self):
        with patch('ui.fonts.QFontDatabase') as database:
            database.return_value.families.return_value = []
            self.assertEqual(resolve_font_families(), [self.app.font().family()])
            database.return_value.families.side_effect = RuntimeError('unavailable')
            self.assertEqual(resolve_font_families(), [self.app.font().family()])

    def test_registered_fallback_has_chinese_glyphs_and_nonzero_metrics(self):
        font = QFont()
        font.setFamilies(resolve_font_families())
        metrics = QFontMetrics(font)
        for char in '图片生成视频制作电商图任务历史设置':
            self.assertTrue(metrics.inFont(char), char)
            self.assertGreater(metrics.horizontalAdvance(char), 0)

    def test_latin_only_font_database_keeps_chinese_metrics_nonzero(self):
        with patch('ui.fonts.QFontDatabase') as database:
            database.return_value.families.return_value = ['Segoe UI']
            families = resolve_font_families()

        self.assertEqual(families, ['Segoe UI'])
        font = QFont()
        font.setFamilies(families)
        metrics = QFontMetrics(font)
        text = '图片生成'
        self.assertGreater(metrics.horizontalAdvance(text), 0)
        self.assertGreater(metrics.boundingRect(text).width(), 0)

    def test_startup_keeps_application_font_when_database_is_unavailable(self):
        from ui.components import custom_widgets
        original = QFont(self.app.font())
        for error in (RuntimeError('unavailable'), OSError('unavailable')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(custom_widgets, '_font_loaded', False), \
                    patch.object(custom_widgets, '_font_result', ([], False, '')), \
                    patch('ui.components.custom_widgets.Path.is_file', return_value=False), \
                    patch('ui.components.custom_widgets.QFontDatabase') as database:
                database.return_value.families.side_effect = error
                self.assertEqual(custom_widgets.ensure_ui_font(), ([], False, ''))
                self.assertEqual(self.app.font(), original)

    def test_startup_survives_failed_font_registration(self):
        from ui.components import custom_widgets
        for error in (RuntimeError('unavailable'), OSError('unavailable')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(custom_widgets, '_font_loaded', False), \
                    patch.object(custom_widgets, '_font_result', ([], False, '')), \
                    patch('ui.components.custom_widgets.Path.is_file', return_value=True), \
                    patch('ui.components.custom_widgets.QFontDatabase') as database:
                database.addApplicationFont.side_effect = error
                database.return_value.families.return_value = ['SimSun']
                families, registered, source = custom_widgets.ensure_ui_font()
                self.assertEqual(families[0], 'SimSun')
                self.assertFalse(registered)
                self.assertEqual(source, '')
