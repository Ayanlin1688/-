"""Offline coverage for the full-window preview, shells and image selection."""
import gc
import json
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt5.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QRect, Qt
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QLabel, QScrollArea, QWidget

from core.ecommerce.platforms import get_platform
from core.i18n import set_language, tr
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from tests.test_interface_scaling import FakeScreen
from ui.components.platform_preview_dialog import (
    PlatformPreviewDialog, PreviewImage, _PhoneShell, _DesktopShell,
)
from ui.components.preview_selection_panel import PREVIEW_IMAGE_MIME
from ui.scaling import apply_interface_scale
from ui.tokens import px
from ui import materials
from ui.theme import apply_ui_mode


DISCLAIMER = ('仅供图片效果预览，界面布局模拟自各平台公开商品页，'
              '与相关平台无隶属关系，不代表任何平台官方立场。')


def solid_png(width, height, rgb):
    def chunk(name, payload):
        return (struct.pack('!I', len(payload)) + name + payload +
                struct.pack('!I', zlib.crc32(name + payload) & 0xFFFFFFFF))

    scanline = b'\x00' + bytes(rgb) * width
    raw = scanline * height
    header = struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) +
            chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


class PlatformPreviewDialogTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        reset_interface_scaling(self.app)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.images = []
        for index in range(4):
            path = self.root / f'preview-{index}.png'
            path.write_bytes(solid_png(64, 48, (30 * index, 120, 200)))
            self.images.append(str(path))
        self.dialogs = []
        self.parents = []
        self.network_patch = patch(
            'requests.sessions.Session.request',
            side_effect=AssertionError('预览测试禁止联网'))
        self.network = self.network_patch.start()

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        for parent in self.parents:
            parent.close()
            parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        gc.collect()
        self.network.assert_not_called()
        self.network_patch.stop()
        set_language('zh-CN')
        reset_interface_scaling(self.app)
        self.temp.cleanup()

    def make_group(self, platforms=None, **overrides):
        group = dict(
            group_id='preview-group',
            product_name='陶瓷茶杯', platforms=platforms if platforms is not None else [
                get_platform('taobao'), get_platform('amazon')],
            config=dict(carousel_count=2, detail_count=1),
            manifest={'items': [
                dict(kind='carousel', seq=1, status='succeeded',
                     file=self.images[0]),
                dict(kind='carousel', seq=2, status='succeeded',
                     file=self.images[1]),
                dict(kind='detail', seq=1, status='succeeded',
                     file=self.images[2]),
            ]}, thumbs=[])
        group.update(overrides)
        return group

    def parent_window(self, width=1600, height=900):
        parent = QWidget()
        parent.setGeometry(60, 40, width, height)
        parent.show()
        flush_layout(self.app, parent)
        self.parents.append(parent)
        return parent

    def dialog(self, group=None, *, parent=None, show=False, **kwargs):
        dialog = PlatformPreviewDialog(parent, self.make_group() if group is None else group,
                                       **kwargs)
        self.dialogs.append(dialog)
        if show:
            dialog.show()
            flush_layout(self.app, dialog)
        return dialog

    def mobile_dialog(self, *, show=False, parent=None, **overrides):
        return self.dialog(self.make_group(platforms=[get_platform('amazon')], **overrides),
                           parent=parent, show=show)

    @staticmethod
    def label_texts(widget):
        return ' '.join(label.text() for label in widget.findChildren(QLabel)
                        if label.text())

    @staticmethod
    def rect_in(widget, parent):
        return QRect(widget.mapTo(parent, QPoint()), widget.size())

    def assert_no_horizontal_overflow(self, dialog):
        for scroll in dialog.findChildren(QScrollArea):
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0,
                             scroll.objectName())
            if scroll.widget() is not None:
                self.assertLessEqual(scroll.widget().width(), scroll.viewport().width(),
                                     scroll.objectName())


class PlatformPreviewShellTests(PlatformPreviewDialogTestBase):
    def test_all_profiles_have_correct_shell_locale_currency_and_missing_text(self):
        import re
        from core.ecommerce.platforms import all_platforms
        from ui.components.platform_profiles import PLATFORM_PROFILES
        self.assertEqual(set(PLATFORM_PROFILES), {p['id'] for p in all_platforms()})
        for platform in all_platforms():
            with self.subTest(platform=platform['id']):
                profile = PLATFORM_PROFILES[platform['id']]
                domestic = platform['group'] == 'domestic'
                self.assertEqual(profile.shell, 'desktop' if domestic else 'phone')
                self.assertEqual(profile.language, 'zh' if domestic else 'en')
                self.assertTrue(profile.price().startswith(profile.currency + ' '))
                if not domestic:
                    self.assertFalse(re.search(r'[\u3400-\u9fff]', ' '.join(profile.labels.values())))
                else:
                    self.assertEqual(profile.text('加入购物车'), '加入购物车')
                    self.assertEqual(profile.text('商品详情'), '商品详情')
                cls = _DesktopShell if domestic else _PhoneShell
                shell = cls(platform, '陶瓷茶杯', ['missing.png'], [])
                self.parents.append(shell)
                self.assertEqual(shell.price_label.text(), profile.price())
                self.assertEqual(shell.old_price_label.text(), profile.price(old=True))
                self.assertTrue(shell.old_price_label.font().strikeOut())
                self.assertEqual(shell.main_image.missing_text, profile.text('缺失'))
                self.assertEqual(len(shell.variant_chips), 2)
                self.assertEqual(shell.quantity_input.value(), 1)
                if not domestic:
                    text = self.label_texts(shell)
                    self.assertFalse(re.search(r'[\u3400-\u9fff]', text), text)

    def test_dialog_uses_overlay_chrome_and_exact_disclaimer(self):
        dialog = self.dialog(self.make_group(platforms=[get_platform('taobao')]))
        self.assertEqual(dialog.disclaimer_label.text(), DISCLAIMER)
        self.assertEqual(dialog.disclaimer_label.text(), tr(DISCLAIMER))
        self.assertEqual(dialog.disclaimer_label.textFormat(), Qt.PlainText)
        self.assertTrue(dialog.titleBar.isHidden())
        self.assertEqual(dialog.titleBar.height(), 0)
        self.assertIn(get_platform('taobao')['name'], dialog.platform_button.text())

    def test_domestic_platform_uses_desktop_shell_with_search_box(self):
        dialog = self.dialog(self.make_group(platforms=[get_platform('taobao')]))
        self.assertEqual(dialog.shell_kind, 'desktop')
        self.assertFalse(dialog.is_mobile)
        self.assertIsInstance(dialog.shell, _DesktopShell)
        shell = dialog.shell
        self.assertEqual(shell.name_label.text(), get_platform('taobao')['name'])
        self.assertIsNotNone(shell.search_edit)
        self.assertEqual(shell.search_edit.placeholderText(), '陶瓷茶杯')
        self.assertEqual(shell.search_button.text(), tr('搜索'))
        self.assertIsNotNone(shell.main_image)
        self.assertTrue(shell.main_image.has_image)
        self.assertEqual(shell.price_label.text(), '¥ 199.00')
        self.assertEqual(len(shell.detail_images), 1)
        self.assertIn('陶瓷茶杯', self.label_texts(dialog))
        self.assertIn(tr('商品详情'), self.label_texts(dialog))

    def test_overseas_platform_uses_phone_shell_with_ios_status_bar(self):
        dialog = self.dialog(self.make_group(platforms=[get_platform('amazon')]))
        self.assertEqual(dialog.shell_kind, 'mobile')
        self.assertTrue(dialog.is_mobile)
        self.assertIsInstance(dialog.shell, _PhoneShell)
        shell = dialog.shell
        self.assertEqual(shell.status_bar.time_label.text(), '9:41')
        self.assertIsNotNone(shell.status_bar.icons)
        self.assertEqual(shell.name_label.text(), 'Product Details')
        self.assertIn(get_platform('amazon')['name'], dialog.platform_button.text())
        self.assertEqual(shell.dots.count, 2)
        self.assertEqual(shell.counter_label.text(), '1/2')
        self.assertTrue(shell.main_image.has_image)
        self.assertEqual(len(shell.detail_images), 1)
        self.assertEqual(shell.cart_button.text(), 'Add to Cart')
        self.assertEqual(shell.buy_button.text(), 'Buy Now')
        self.assertEqual(shell.sale_label.text(), 'SALE')
        self.assertEqual(shell.sold_label.text(), '12.8K sold')
        for text in ('Home', 'Chat', 'Save', 'Free Shipping', '30-day Returns', 'Fast Delivery'):
            self.assertIn(text, self.label_texts(shell))
        self.assertEqual(shell.width(), px(400))
        self.assertEqual(shell.height(), px(730))

    def test_platform_menu_switches_shell_and_theme(self):
        dialog = self.dialog()
        actions = dialog.platform_menu.actions()
        self.assertEqual(len(actions), 2)
        self.assertIn(get_platform('taobao')['name'], dialog.platform_button.text())
        self.assertEqual(dialog.shell_kind, 'desktop')
        taobao = get_platform('taobao')
        self.assertEqual(dialog.primary_color.name(), taobao['primaryColor'].lower())
        self.assertEqual(dialog.accent_color.name(), taobao['accentColor'].lower())

        old_shell = dialog.shell
        actions[1].trigger()
        self.assertIsNot(dialog.shell, old_shell)
        self.assertEqual(dialog.current_index, 1)
        self.assertEqual(dialog.current_platform, get_platform('amazon'))
        self.assertEqual(dialog.shell_kind, 'mobile')
        self.assertIn(get_platform('amazon')['name'], dialog.platform_button.text())
        amazon = get_platform('amazon')
        self.assertEqual(dialog.primary_color.name(), amazon['primaryColor'].lower())

        actions[0].trigger()
        self.assertEqual(dialog.shell_kind, 'desktop')
        self.assertEqual(dialog.scroll.verticalScrollBar().value(), 0)

    def test_set_platform_index_helper(self):
        dialog = self.dialog()
        dialog.set_platform_index(1)
        self.assertTrue(dialog.is_mobile)
        self.assertEqual(dialog.current_index, 1)
        dialog.set_platform_index(0)
        self.assertFalse(dialog.is_mobile)
        self.assertEqual(dialog.current_index, 0)

    def test_carousel_switch_updates_main_image_and_dots(self):
        dialog = self.dialog(self.make_group(platforms=[get_platform('amazon')]))
        shell = dialog.shell
        self.assertEqual(shell.main_image.image_path, self.images[0])
        shell.set_carousel_index(1)
        self.assertEqual(shell.main_image.image_path, self.images[1])
        self.assertEqual(shell.dots.index, 1)
        self.assertEqual(shell.counter_label.text(), '2/2')
        shell.set_carousel_index(5)
        self.assertEqual(shell.carousel_index, 1)

    def test_manifest_images_are_sorted_carousel_then_detail(self):
        group = self.make_group(platforms=[get_platform('taobao')], manifest={'items': [
            dict(kind='carousel', seq=2, status='succeeded', file=self.images[1]),
            dict(kind='carousel', seq=1, status='succeeded', file=self.images[0]),
            dict(kind='detail', seq=2, status='failed', file=''),
            dict(kind='detail', seq=1, status='succeeded', file=self.images[2]),
            dict(kind='carousel', seq=3, status='running', file=self.images[3]),
        ]})
        dialog = self.dialog(group)
        self.assertEqual(dialog.carousel, [self.images[0], self.images[1]])
        self.assertEqual(dialog.detail, [self.images[2]])
        self.assertEqual(dialog.selected_images('carousel'), dialog.carousel)
        self.assertEqual(dialog.selected_images('detail'), dialog.detail)


class PlatformPreviewPlaceholderTests(PlatformPreviewDialogTestBase):
    def test_no_generated_images_shows_placeholder(self):
        group = dict(group_id='empty-group', product_name='空任务',
                     platforms=[get_platform('taobao')], config={},
                     manifest={'items': []}, thumbs=[])
        dialog = self.dialog(group)
        self.assertEqual(dialog.shell_kind, 'desktop')
        self.assertIsNone(dialog.shell.main_image)
        self.assertIn(tr('暂无生成图片'), self.label_texts(dialog))

    def test_no_generated_images_overseas_shows_placeholder(self):
        group = dict(group_id='empty-overseas', product_name='空任务',
                     platforms=[get_platform('amazon')], config={},
                     manifest={'items': []}, thumbs=[])
        dialog = self.dialog(group)
        self.assertTrue(dialog.is_mobile)
        self.assertIsNone(dialog.shell.main_image)
        self.assertIn(tr('暂无生成图片'), self.label_texts(dialog))
        self.assertTrue(dialog.shell.counter_label.isHidden())

    def test_thumbs_are_used_when_manifest_has_no_items(self):
        group = dict(group_id='thumb-group', product_name='图片任务',
                     platforms=[get_platform('taobao')], config={},
                     manifest={}, thumbs=[self.images[0], self.images[1]])
        dialog = self.dialog(group)
        self.assertEqual(dialog.carousel, [self.images[0], self.images[1]])
        self.assertIsNotNone(dialog.shell.main_image)
        self.assertTrue(dialog.shell.main_image.has_image)

    def test_missing_group_fields_are_tolerated(self):
        dialog = self.dialog({})
        self.assertEqual(len(dialog.platform_menu.actions()), 1)
        self.assertEqual(dialog.current_index, 0)
        self.assertEqual(dialog.shell_kind, 'desktop')
        self.assertIn(tr('暂无生成图片'), self.label_texts(dialog))
        self.assertEqual(dialog.disclaimer_label.text(), DISCLAIMER)

    def test_null_manifest_items_use_thumbs_or_empty_placeholder(self):
        for thumbs in ([], self.images[:2]):
            with self.subTest(thumbs=thumbs):
                dialog = self.mobile_dialog(manifest={'items': None}, thumbs=thumbs)
                self.assertEqual(dialog.selected_images('carousel'), thumbs)
                self.assertEqual(dialog.selected_count('detail'), 0)
                if thumbs:
                    self.assertEqual(dialog.shell.main_image.image_path, thumbs[0])
                else:
                    self.assertIsNone(dialog.shell.main_image)
                    self.assertIn(tr('暂无生成图片'), self.label_texts(dialog))

    def test_broken_image_file_falls_back_to_placeholder(self):
        broken = self.root / 'broken.png'
        broken.write_bytes(b'not-a-real-image')
        group = self.make_group(platforms=[get_platform('taobao')], manifest={'items': [
            dict(kind='carousel', seq=1, status='succeeded', file=str(broken)),
        ]})
        dialog = self.dialog(group)
        self.assertFalse(dialog.shell.main_image.has_image)

    def test_preview_image_widget_reports_image_state(self):
        widget = PreviewImage(self.images[0], height=120)
        try:
            self.assertTrue(widget.has_image)
            self.assertEqual(widget.image_path, self.images[0])
            widget.set_image(self.images[1])
            self.assertEqual(widget.image_path, self.images[1])
            widget.set_image('')
            self.assertFalse(widget.has_image)
        finally:
            widget.deleteLater()


class PlatformPreviewDialogStateTests(PlatformPreviewDialogTestBase):
    def test_retranslate_ui_updates_shell_in_place(self):
        dialog = self.dialog()
        shell = dialog.shell
        set_language('en-US')
        try:
            dialog.retranslate_ui()
            self.assertIs(dialog.shell, shell)
            self.assertEqual(shell.search_button.text(), '搜索')
            self.assertEqual(dialog.disclaimer_label.text(), tr(DISCLAIMER))
            self.assertEqual(dialog.shell_kind, 'desktop')
            self.assertEqual(len(dialog.platform_menu.actions()), 2)
            self.assertEqual(dialog.selection_panel.hint_label.text(),
                             tr('点击勾选 · 拖拽排序'))
            self.assertNotEqual(dialog.selection_panel.hint_label.text(),
                                '点击勾选 · 拖拽排序')
            self.assertEqual(dialog.selection_panel.clear_buttons['carousel'].text(),
                             tr('取消全选'))
            dialog.set_platform_index(1)
            self.assertEqual(dialog.shell.name_label.text(), 'Product Details')
            self.assertEqual(dialog.shell.cart_button.text(), 'Add to Cart')
            phone = dialog.shell
            set_language('zh-CN')
            self.assertIs(dialog.shell, phone)
            self.assertEqual(phone.cart_button.text(), 'Add to Cart')
        finally:
            set_language('zh-CN')
            dialog.retranslate_ui()

    def test_refresh_colors_keeps_shell_alive(self):
        dialog = self.dialog()
        dialog.refresh_colors()
        self.assertIsNotNone(dialog.shell)
        self.assertEqual(dialog.shell_kind, 'desktop')

    def test_platform_without_group_field_treated_as_domestic(self):
        group = self.make_group(platforms=[dict(name='未知平台')])
        dialog = self.dialog(group)
        self.assertEqual(dialog.shell_kind, 'desktop')
        self.assertEqual(dialog.shell.name_label.text(), '未知平台')
        self.assertEqual(len(dialog.platform_menu.actions()), 1)

    def test_dialog_exposes_generated_image_counts(self):
        dialog = self.dialog()
        self.assertEqual(len(dialog.carousel), 2)
        self.assertEqual(len(dialog.detail), 1)
        self.assertEqual(len(dialog.shell.detail_images), 1)
        self.assertEqual(dialog.selected_count('carousel'), 2)
        self.assertEqual(dialog.selected_count('detail'), 1)


class PlatformPreviewOverlayLayoutTests(PlatformPreviewDialogTestBase):
    def test_overlay_matches_parent_geometry_without_default_title_bar(self):
        parent = self.parent_window()
        dialog = self.mobile_dialog(parent=parent, show=True)
        expected = QRect(parent.mapToGlobal(QPoint()), parent.size())
        self.assertEqual(dialog.geometry(), expected)
        self.assertTrue(dialog.isModal())
        self.assertTrue(dialog.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(dialog.titleBar.isHidden())
        self.assertEqual(dialog.titleBar.height(), 0)
        self.assertEqual(dialog.overlay_header.height(), px(48))
        self.assertEqual(dialog.disclaimer_bar.height(), px(28))
        self.assertEqual(dialog.selection_panel.width(), px(260))
        self.assertEqual(dialog.selection_panel.columns, 3)
        self.assert_no_horizontal_overflow(dialog)

    def test_close_button_closes_overlay(self):
        dialog = self.mobile_dialog(parent=self.parent_window(), show=True)
        self.assertTrue(dialog.isVisible())
        QTest.mouseClick(dialog.close_button, Qt.LeftButton)
        self.assertFalse(dialog.isVisible())

    def test_phone_backgrounds_keep_status_dark_and_product_info_white(self):
        dialog = self.mobile_dialog(parent=self.parent_window(), show=True)
        status = dialog.shell.status_bar.grab().toImage()
        info = dialog.shell.product_label.parentWidget().grab().toImage()
        self.assertEqual(status.pixelColor(status.width() // 2, status.height() // 2).name(),
                         materials.ecommerce_color('preview_pill_bg').lower())
        self.assertEqual(info.pixelColor(1, 1).name(),
                         materials.ecommerce_color('preview_card').lower())

    def test_escape_closes_overlay(self):
        dialog = self.mobile_dialog(parent=self.parent_window(), show=True)
        QTest.keyClick(dialog, Qt.Key_Escape)
        self.assertFalse(dialog.isVisible())

    def test_header_sidebar_and_shell_fit_at_all_supported_scales(self):
        for scale in (1.0, 1.25, 1.5, 1.75):
            with self.subTest(scale=scale):
                apply_interface_scale(self.app, FakeScreen(), scale)
                parent = self.parent_window()
                dialog = self.mobile_dialog(parent=parent, show=True)
                flush_layout(self.app, dialog)
                self.assertEqual(dialog.size(), parent.size())
                self.assertEqual(dialog.overlay_header.height(), px(48))
                self.assertEqual(dialog.disclaimer_bar.height(), px(28))
                expected_width = px(232) if dialog.width() < px(1100) else px(260)
                self.assertEqual(dialog.selection_panel.width(), expected_width)
                self.assertEqual(dialog.shell.width(), px(400))
                self.assertEqual(dialog.shell.height(), px(730))
                header = self.rect_in(dialog.overlay_header, dialog)
                disclaimer = self.rect_in(dialog.disclaimer_bar, dialog)
                sidebar = self.rect_in(dialog.selection_panel, dialog)
                center = self.rect_in(dialog.scroll, dialog)
                self.assertLess(header.bottom(), disclaimer.top())
                self.assertLess(disclaimer.bottom(), sidebar.top())
                self.assertLess(center.right(), sidebar.left())
                for rect in (header, disclaimer, sidebar, center):
                    self.assertTrue(dialog.rect().contains(rect), rect)
                pill = self.rect_in(dialog.platform_button, dialog.overlay_header)
                close = self.rect_in(dialog.close_button, dialog.overlay_header)
                self.assertLess(pill.right(), close.left())
                self.assertTrue(dialog.overlay_header.rect().contains(pill))
                self.assertTrue(dialog.overlay_header.rect().contains(close))
                self.assert_no_horizontal_overflow(dialog)
                dialog.close()
                parent.close()

    def test_narrow_window_uses_two_thumbnail_columns_without_horizontal_scroll(self):
        dialog = self.mobile_dialog(parent=self.parent_window(1000, 900), show=True)
        panel = dialog.selection_panel
        self.assertEqual(panel.width(), px(232))
        self.assertEqual(panel.columns, 2)
        self.assert_no_horizontal_overflow(dialog)
        for tiles in panel.tiles.values():
            for tile in tiles:
                self.assertTrue(panel.scroll_area.widget().rect().contains(
                    self.rect_in(tile, panel.scroll_area.widget())))

    def test_short_window_scrolls_to_show_entire_phone_shell(self):
        dialog = self.mobile_dialog(parent=self.parent_window(1600, 620), show=True)
        scroll = dialog.scroll
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        self.app.processEvents()
        shell_rect = self.rect_in(dialog.shell, scroll.viewport())
        self.assertLessEqual(shell_rect.bottom(), scroll.viewport().height())
        self.assert_no_horizontal_overflow(dialog)


class PlatformPreviewFooterGeometryTests(PlatformPreviewDialogTestBase):
    def test_phone_footer_and_scrolling_rows_stay_inside_shell_at_all_scales(self):
        for scale in (1.0, 1.25, 1.5, 1.75):
            apply_interface_scale(self.app, FakeScreen(), scale)
            for mode in ('light', 'dark'):
                with self.subTest(scale=scale, theme=mode):
                    apply_ui_mode(mode)
                    dialog = self.mobile_dialog(parent=self.parent_window(), show=True)
                    shell = dialog.shell
                    footer = self.rect_in(shell.bottom_bar, shell)
                    viewport = self.rect_in(shell.scroll.viewport(), shell)
                    self.assertEqual(footer.height(), px(60))
                    self.assertTrue(shell.rect().contains(footer))
                    self.assertLess(viewport.bottom(), footer.top())
                    self.assertLess(self.rect_in(shell.nav_bar, shell).bottom(), viewport.top())
                    self.assertFalse(shell.mask().isEmpty())
                    self.assertFalse(shell.mask().contains(QPoint(0, 0)))
                    before = shell.bottom_bar.geometry()
                    shell.scroll.ensureWidgetVisible(shell.variant_row, 0, px(8))
                    flush_layout(self.app, dialog)
                    for chip in shell.variant_chips:
                        self.assertTrue(shell.scroll.viewport().rect().contains(
                            self.rect_in(chip, shell.scroll.viewport())))
                        self.assertGreaterEqual(chip.height(), px(30))
                        self.assertGreaterEqual(chip.width(), chip.minimumSizeHint().width())
                        self.assertGreaterEqual(chip.width(), chip.fontMetrics().horizontalAdvance(chip.text()) + px(16))
                    self.assertEqual(shell.bottom_bar.geometry(), before)
                    for button in (shell.cart_button, shell.buy_button):
                        self.assertTrue(shell.bottom_bar.rect().contains(self.rect_in(button, shell.bottom_bar)))
                        self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()))
                    for row in (shell.store_label, shell.rating_label, shell.variant_label, shell.discount_label):
                        self.assertIn('background:transparent', row.styleSheet())
                    for child in shell.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
                        if child.isVisible():
                            self.assertTrue(shell.rect().contains(child.geometry()), (child.objectName(), child.geometry()))
                    self.assert_no_horizontal_overflow(dialog)
                    dialog.close()

    def test_domestic_info_buttons_and_details_do_not_overlap_at_100_and_175(self):
        for scale in (1.0, 1.75):
            apply_interface_scale(self.app, FakeScreen(), scale)
            for mode in ('light', 'dark'):
                with self.subTest(scale=scale, theme=mode):
                    apply_ui_mode(mode)
                    dialog = self.dialog(self.make_group(platforms=[get_platform('taobao')]),
                                         parent=self.parent_window(), show=True)
                    shell = dialog.shell
                    main = shell.main_layout.parentWidget()
                    self.assertLess(self.rect_in(main, shell).bottom(), self.rect_in(shell.detail_title, shell).top())
                    for parent in (shell, main):
                        for child in parent.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
                            if child.isVisible():
                                self.assertTrue(parent.rect().contains(child.geometry()), (child.objectName(), child.geometry()))
                    self.assert_no_horizontal_overflow(dialog)
                    dialog.close()


class PlatformPreviewSelectionTests(PlatformPreviewDialogTestBase):
    @staticmethod
    def drag_event(source, payload, *, mime_type=PREVIEW_IMAGE_MIME):
        mime = QMimeData()
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
        mime.setData(mime_type, data)
        event = Mock()
        event.source.return_value = source
        event.mimeData.return_value = mime
        return event

    def test_default_selection_groups_badges_and_hint_match_manifest(self):
        dialog = self.mobile_dialog()
        panel = dialog.selection_panel
        self.assertEqual(dialog.selected_images('carousel'), self.images[:2])
        self.assertEqual(dialog.selected_images('detail'), self.images[2:3])
        self.assertEqual([tile.badge_label.text() for tile in panel.tiles['carousel']],
                         [tr('轮{seq}').format(seq=1), tr('轮{seq}').format(seq=2)])
        self.assertEqual([tile.badge_label.text() for tile in panel.tiles['detail']],
                         [tr('详{seq}').format(seq=1)])
        self.assertTrue(all(tile.checked for tiles in panel.tiles.values() for tile in tiles))
        self.assertEqual(panel.title_labels['carousel'].text(), tr('轮播图'))
        self.assertEqual(panel.title_labels['detail'].text(), tr('详情图'))
        self.assertEqual(panel.hint_label.text(), tr('点击勾选 · 拖拽排序'))

    def test_click_thumbnail_updates_selection_and_phone_main_image(self):
        dialog = self.mobile_dialog(parent=self.parent_window(), show=True)
        QTest.mouseClick(dialog.selection_panel.tiles['carousel'][0], Qt.LeftButton)
        self.assertEqual(dialog.selected_count('carousel'), 1)
        self.assertEqual(dialog.selected_images('carousel'), [self.images[1]])
        self.assertEqual(dialog.shell.main_image.image_path, self.images[1])
        self.assertEqual(dialog.shell.counter_label.text(), '1/1')
        QTest.mouseClick(dialog.selection_panel.tiles['carousel'][0], Qt.LeftButton)
        self.assertEqual(dialog.selected_count('carousel'), 2)
        self.assertEqual(dialog.selected_images('carousel'), self.images[:2])
        self.assertEqual(dialog.shell.main_image.image_path, self.images[0])

    def test_clear_carousel_selection_shows_placeholder_and_hides_page_counter(self):
        dialog = self.mobile_dialog(parent=self.parent_window(), show=True)
        QTest.mouseClick(dialog.selection_panel.clear_buttons['carousel'], Qt.LeftButton)
        self.assertEqual(dialog.selected_count('carousel'), 0)
        self.assertEqual(dialog.selected_images('carousel'), [])
        self.assertIsNone(dialog.shell.main_image)
        self.assertTrue(dialog.shell.counter_label.isHidden())
        self.assertIn('Select carousel images', self.label_texts(dialog.shell))
        self.assertEqual(dialog.selected_count('detail'), 1)

    def test_detail_selection_only_changes_detail_images(self):
        dialog = self.mobile_dialog()
        dialog.clear_selection('detail')
        self.assertEqual(dialog.selected_images('detail'), [])
        self.assertEqual(dialog.selected_count('detail'), 0)
        self.assertEqual(dialog.selected_images('carousel'), self.images[:2])
        self.assertEqual(dialog.shell.main_image.image_path, self.images[0])
        self.assertFalse(any(isinstance(image, PreviewImage)
                             for image in dialog.shell.detail_images))

    def test_move_image_updates_selected_order_badges_and_phone_main_image(self):
        dialog = self.mobile_dialog()
        dialog.move_image('carousel', 0, 1)
        self.assertEqual(dialog.selected_images('carousel'), [self.images[1], self.images[0]])
        self.assertEqual(dialog.shell.main_image.image_path, self.images[1])
        self.assertEqual([tile.seq for tile in dialog.selection_panel.tiles['carousel']], [2, 1])
        dialog.shell.set_carousel_index(1)
        self.assertEqual(dialog.shell.main_image.image_path, self.images[0])
        self.assertEqual(dialog.shell.counter_label.text(), '2/2')

    def test_platform_switch_preserves_selection_and_sort_order(self):
        dialog = self.dialog()
        dialog.move_image('carousel', 0, 1)
        dialog.toggle_image('carousel', 1)
        dialog.clear_selection('detail')
        dialog.set_platform_index(1)
        self.assertTrue(dialog.is_mobile)
        self.assertEqual(dialog.selected_images('carousel'), [self.images[1]])
        self.assertEqual(dialog.selected_images('detail'), [])
        self.assertEqual(dialog.shell.main_image.image_path, self.images[1])
        dialog.set_platform_index(0)
        self.assertFalse(dialog.is_mobile)
        self.assertEqual(dialog.shell.main_image.image_path, self.images[1])
        self.assertEqual(dialog.selected_count('carousel'), 1)

    def test_refreshes_and_language_change_preserve_selection(self):
        dialog = self.mobile_dialog()
        dialog.move_image('carousel', 0, 1)
        dialog.toggle_image('carousel', 1)
        dialog.clear_selection('detail')
        set_language('en-US')
        dialog.retranslate_ui()
        dialog.refresh_colors()
        dialog.refresh_interface_scale()
        self.assertEqual(dialog.selected_images('carousel'), [self.images[1]])
        self.assertEqual(dialog.selected_images('detail'), [])
        self.assertEqual(dialog.shell.main_image.image_path, self.images[1])
        self.assertEqual(dialog.selection_panel.hint_label.text(), tr('点击勾选 · 拖拽排序'))

    def test_empty_image_groups_show_group_placeholders(self):
        dialog = self.mobile_dialog(manifest={'items': []}, thumbs=[])
        for kind in ('carousel', 'detail'):
            self.assertEqual(dialog.selected_count(kind), 0)
            self.assertEqual(dialog.selection_panel.tiles[kind], [])
            self.assertFalse(dialog.selection_panel.empty_labels[kind].isHidden())
            self.assertEqual(dialog.selection_panel.empty_labels[kind].text(), tr('暂无生成图片'))

    def test_click_phone_main_image_cycles_only_selected_carousel(self):
        dialog = self.mobile_dialog(parent=self.parent_window(), show=True)
        QTest.mouseClick(dialog.shell.main_image, Qt.LeftButton)
        self.assertEqual(dialog.shell.main_image.image_path, self.images[1])
        self.assertEqual(dialog.shell.counter_label.text(), '2/2')
        QTest.mouseClick(dialog.shell.main_image, Qt.LeftButton)
        self.assertEqual(dialog.shell.main_image.image_path, self.images[0])
        dialog.toggle_image('carousel', 0)
        QTest.mouseClick(dialog.shell.main_image, Qt.LeftButton)
        self.assertEqual(dialog.shell.main_image.image_path, self.images[1])
        self.assertEqual(dialog.shell.counter_label.text(), '1/1')

    def test_drop_same_group_moves_image_and_clears_insertion_line(self):
        dialog = self.mobile_dialog()
        panel = dialog.selection_panel
        source, target = panel.tiles['carousel']
        changed = QSignalSpy(panel.selection_changed)
        event = self.drag_event(source, {'kind': 'carousel', 'index': 0})
        target.dragEnterEvent(event)
        self.assertTrue(target._insert_line)
        target.dropEvent(event)
        self.assertFalse(target._insert_line)
        self.assertEqual(event.acceptProposedAction.call_count, 2)
        event.ignore.assert_not_called()
        self.assertEqual(len(changed), 1)
        self.assertEqual(dialog.selected_images('carousel'), [self.images[1], self.images[0]])
        self.assertEqual(dialog.shell.main_image.image_path, self.images[1])

    def test_drop_rejects_cross_group_foreign_panel_and_non_tile_sources(self):
        dialog = self.mobile_dialog()
        other = self.mobile_dialog()
        panel = dialog.selection_panel
        target = panel.tiles['carousel'][1]
        changed = QSignalSpy(panel.selection_changed)
        sources = (
            panel.tiles['detail'][0],
            other.selection_panel.tiles['carousel'][0],
            QWidget(dialog),
            None,
        )
        for source in sources:
            with self.subTest(source=type(source).__name__):
                event = self.drag_event(source, {'kind': 'carousel', 'index': 0})
                target.dragEnterEvent(event)
                target.dropEvent(event)
                event.acceptProposedAction.assert_not_called()
                self.assertEqual(event.ignore.call_count, 2)
                self.assertFalse(target._insert_line)
                self.assertEqual(dialog.selected_images('carousel'), self.images[:2])
                self.assertEqual(len(changed), 0)

    def test_drop_rejects_invalid_mime_and_stale_or_out_of_range_indices(self):
        dialog = self.mobile_dialog()
        panel = dialog.selection_panel
        source, target = panel.tiles['carousel']
        changed = QSignalSpy(panel.selection_changed)
        invalid = (
            ('text/plain', {'kind': 'carousel', 'index': 0}),
            (PREVIEW_IMAGE_MIME, b'{broken json'),
            (PREVIEW_IMAGE_MIME, {'kind': 'detail', 'index': 0}),
            (PREVIEW_IMAGE_MIME, {'kind': 'carousel', 'index': -1}),
            (PREVIEW_IMAGE_MIME, {'kind': 'carousel', 'index': 100}),
            (PREVIEW_IMAGE_MIME, {'kind': 'carousel', 'index': True}),
            (PREVIEW_IMAGE_MIME, {'kind': 'carousel', 'index': 1}),
        )
        for mime_type, payload in invalid:
            with self.subTest(mime=mime_type, payload=payload):
                event = self.drag_event(source, payload, mime_type=mime_type)
                target.dragEnterEvent(event)
                target.dropEvent(event)
                event.acceptProposedAction.assert_not_called()
                self.assertEqual(event.ignore.call_count, 2)
                self.assertFalse(target._insert_line)
                self.assertEqual(dialog.selected_images('carousel'), self.images[:2])
                self.assertEqual(len(changed), 0)


if __name__ == '__main__':
    unittest.main()
