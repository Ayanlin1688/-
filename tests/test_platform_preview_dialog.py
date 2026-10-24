"""平台预览通用模板 offscreen 回归：国内桌面壳 / 海外手机壳 / 平台切换 / 占位。"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from PyQt5.QtWidgets import QApplication, QLabel

from core.ecommerce.platforms import get_platform
from core.i18n import set_language, tr
from tests.qt_scale_support import reset_interface_scaling
from ui.components.platform_preview_dialog import (
    PlatformPreviewDialog, PreviewImage, _PhoneShell, _DesktopShell,
)
from ui.components.studio_dialog import StudioDialog


DISCLAIMER = ('仅供图片效果预览，界面布局模拟自各平台公开商品页，'
              '与相关平台无隶属关系，仅用于图片效果自查')


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
        self.addCleanup(set_language, 'zh-CN')
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.images = []
        for index in range(4):
            path = self.root / f'preview-{index}.png'
            path.write_bytes(solid_png(64, 48, (30 * index, 120, 200)))
            self.images.append(str(path))
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()

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

    def dialog(self, group=None, **kwargs):
        dialog = PlatformPreviewDialog(None, self.make_group() if group is None else group,
                                       **kwargs)
        self.dialogs.append(dialog)
        return dialog

    @staticmethod
    def label_texts(widget):
        return ' '.join(label.text() for label in widget.findChildren(QLabel)
                        if label.text())


class PlatformPreviewShellTests(PlatformPreviewDialogTestBase):
    def test_dialog_uses_studio_chrome_and_exact_disclaimer(self):
        dialog = self.dialog(self.make_group(platforms=[get_platform('taobao')]))
        self.assertIsInstance(dialog, StudioDialog)
        self.assertEqual(dialog.disclaimer_label.text(), DISCLAIMER)
        self.assertEqual(dialog.disclaimer_label.text(), tr(DISCLAIMER))
        self.assertIn('平台预览', dialog.header.title_label.text())

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
        self.assertEqual(shell.name_label.text(), get_platform('amazon')['name'])
        self.assertEqual(shell.dots.count, 2)
        self.assertEqual(shell.counter_label.text(), '1/2')
        self.assertTrue(shell.main_image.has_image)
        self.assertEqual(len(shell.detail_images), 1)
        self.assertEqual(shell.cart_button.text(), tr('加入购物车'))
        self.assertEqual(shell.buy_button.text(), tr('立即购买'))

    def test_platform_combo_switches_shell_and_theme(self):
        dialog = self.dialog()
        self.assertEqual(dialog.platform_combo.count(), 2)
        self.assertEqual(dialog.platform_combo.currentText(),
                         get_platform('taobao')['name'])
        self.assertEqual(dialog.shell_kind, 'desktop')
        taobao = get_platform('taobao')
        self.assertEqual(dialog.primary_color.name(), taobao['primaryColor'].lower())
        self.assertEqual(dialog.accent_color.name(), taobao['accentColor'].lower())

        dialog.platform_combo.setCurrentIndex(1)
        self.assertEqual(dialog.shell_kind, 'mobile')
        self.assertEqual(dialog.shell.name_label.text(), get_platform('amazon')['name'])
        amazon = get_platform('amazon')
        self.assertEqual(dialog.primary_color.name(), amazon['primaryColor'].lower())

        dialog.platform_combo.setCurrentIndex(0)
        self.assertEqual(dialog.shell_kind, 'desktop')
        self.assertEqual(dialog.scroll.verticalScrollBar().value(), 0)

    def test_set_platform_index_helper(self):
        dialog = self.dialog()
        dialog.set_platform_index(1)
        self.assertTrue(dialog.is_mobile)
        dialog.set_platform_index(0)
        self.assertFalse(dialog.is_mobile)

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


class PlatformPreviewPlaceholderTests(PlatformPreviewDialogTestBase):
    def test_no_generated_images_shows_placeholder(self):
        group = dict(group_id='empty-group', product_name='空任务',
                     platforms=[get_platform('taobao')], config={},
                     manifest={'items': []}, thumbs=[])
        dialog = self.dialog(group)
        self.assertEqual(dialog.shell_kind, 'desktop')
        self.assertIsNone(dialog.shell.main_image)
        self.assertIn(tr('暂无预览图片'), self.label_texts(dialog))

    def test_no_generated_images_overseas_shows_placeholder(self):
        group = dict(group_id='empty-overseas', product_name='空任务',
                     platforms=[get_platform('amazon')], config={},
                     manifest={'items': []}, thumbs=[])
        dialog = self.dialog(group)
        self.assertTrue(dialog.is_mobile)
        self.assertIsNone(dialog.shell.main_image)
        self.assertIn(tr('暂无预览图片'), self.label_texts(dialog))

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
        self.assertEqual(dialog.platform_combo.count(), 1)
        self.assertEqual(dialog.shell_kind, 'desktop')
        self.assertIn(tr('暂无预览图片'), self.label_texts(dialog))
        self.assertEqual(dialog.disclaimer_label.text(), DISCLAIMER)

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
    def test_retranslate_ui_rebuilds_shell_without_raising(self):
        dialog = self.dialog()
        set_language('en-US')
        try:
            dialog.retranslate_ui()
            self.assertEqual(dialog.disclaimer_label.text(), tr(DISCLAIMER))
            self.assertEqual(dialog.shell_kind, 'desktop')
            self.assertEqual(dialog.platform_combo.count(), 2)
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
        self.assertEqual(dialog.platform_combo.count(), 1)

    def test_dialog_exposes_generated_image_counts(self):
        dialog = self.dialog()
        self.assertEqual(len(dialog.carousel), 2)
        self.assertEqual(len(dialog.detail), 1)
        self.assertEqual(len(dialog.shell.detail_images), 1)


if __name__ == '__main__':
    unittest.main()
