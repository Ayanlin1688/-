"""段1：上传区 / 灯箱 / 三个图片选择器 / 结果图灯箱接线 的 offscreen 回归。"""
import base64
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QKeyEvent, QWheelEvent
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QDialog, QGraphicsView

from core.config_manager import ConfigManager
from core.i18n import set_language
from ui.components.ecommerce_result_panel import EcommerceResultPanel
from ui.components.image_lightbox import ImageLightbox
from ui.components.image_pickers import (
    GalleryPickerDialog, UrlImageDialog, UsedImagePickerDialog,
    parse_input_lines, resolve_input_entries, scan_gallery_images,
)
from ui.components.image_upload_grid import ImageUploadGrid

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==')


def solid_png(width, height, rgb=(42, 157, 102)):
    def chunk(name, payload):
        return (struct.pack('!I', len(payload)) + name + payload +
                struct.pack('!I', zlib.crc32(name + payload) & 0xFFFFFFFF))

    scanline = b'\x00' + bytes(rgb) * width
    raw = scanline * height
    header = struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) +
            chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


class _QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def image(self, name='a.png', size=(64, 64)):
        path = self.root / name
        path.write_bytes(solid_png(size[0], size[1]))
        return str(path)


class ImageUploadGridTests(_QtCase):
    def test_product_grid_order_add_tile_and_remove(self):
        grid = ImageUploadGrid('product', 6)
        grid.resize(900, 200)
        paths = [self.image(f'{index}.png') for index in range(3)]
        changed = QSignalSpy(grid.changed)
        grid.set_paths(paths)
        self.assertEqual(grid.paths(), paths)
        self.assertEqual(grid.count(), 3)
        self.assertEqual(len(changed), 1)
        self.assertEqual(grid.add_tile().text_label.text(), '继续添加 3/6')
        removed = QSignalSpy(grid.preview_requested)
        grid.tile_at(1).remove_button.clicked.emit()
        self.assertEqual(grid.paths(), [paths[0], paths[2]])
        self.assertEqual(len(removed), 0)
        self.assertEqual(grid.add_tile().text_label.text(), '继续添加 2/6')

    def test_product_grid_max_six_and_style_max_one(self):
        product = ImageUploadGrid('product', 6)
        product.resize(900, 300)
        paths = [self.image(f'p{index}.png') for index in range(8)]
        product.set_paths(paths)
        self.assertEqual(product.count(), 6)
        self.assertEqual(product.paths(), paths[:6])
        self.assertIsNone(product.add_tile())

        style = ImageUploadGrid('style', 1)
        style.resize(400, 120)
        style_paths = [self.image('s1.png'), self.image('s2.png')]
        style.set_paths(style_paths)
        self.assertEqual(style.count(), 1)
        self.assertEqual(style.paths(), style_paths[:1])
        self.assertIsNone(style.add_tile())
        style.clear()
        self.assertIsNotNone(style.add_tile())
        self.assertEqual(style.add_tile().text_label.text(), '+ 上传参考图')

    def test_preview_and_add_signals(self):
        grid = ImageUploadGrid('product', 6)
        grid.resize(900, 300)
        previews, adds = QSignalSpy(grid.preview_requested), QSignalSpy(grid.add_requested)
        path = self.image('one.png')
        grid.set_paths([path])
        QTest.mouseClick(grid.tile_at(0), Qt.LeftButton)
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0][0], 0)
        QTest.mouseClick(grid.add_tile(), Qt.LeftButton)
        self.assertEqual(len(adds), 1)

    def test_missing_file_tile_is_safe(self):
        grid = ImageUploadGrid('product', 6)
        grid.resize(900, 200)
        missing = str(self.root / 'missing.png')
        grid.set_paths([missing])
        tile = grid.tile_at(0)
        self.assertTrue(tile._pixmap.isNull())
        self.assertEqual(tile.toolTip(), missing)
        grid.refresh_colors()
        grid.refresh_interface_scale()

    def test_grid_shrinks_tiles_for_narrow_width(self):
        grid = ImageUploadGrid('product', 6)
        grid.resize(960, 300)
        grid.show()
        self.app.processEvents()
        grid.set_paths([self.image('w.png')])
        wide = grid.tile_at(0).width()
        grid.resize(480, 300)
        self.assertLess(grid.tile_at(0).width(), wide)
        self.assertGreaterEqual(grid.tile_at(0).width(), 56)


class ImageLightboxTests(_QtCase):
    def make(self, count=2):
        paths = [self.image(f'lb{index}.png', (120, 80)) for index in range(count)]
        dialog = ImageLightbox(None, paths, 0)
        dialog.resize(1000, 700)
        dialog.show()
        self.app.processEvents()
        return dialog, paths

    def test_counter_and_keyboard_navigation(self):
        dialog, paths = self.make(3)
        try:
            self.assertEqual(dialog.current_index(), 0)
            self.assertEqual(dialog.counter_label.text(), '1 / 3')
            dialog.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
            self.assertEqual(dialog.current_index(), 1)
            self.assertEqual(dialog.counter_label.text(), '2 / 3')
            dialog.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
            dialog.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
            self.assertEqual(dialog.current_index(), 2)
            self.assertFalse(dialog.next_button.isEnabled())
            dialog.previous_image()
            self.assertEqual(dialog.current_path(), paths[1])
            dialog.previous_image()
            dialog.previous_image()
            self.assertEqual(dialog.current_path(), paths[0])
            self.assertFalse(dialog.prev_button.isEnabled())
        finally:
            dialog.close()

    def test_escape_closes_and_emits_once(self):
        dialog, _paths = self.make(1)
        closed = QSignalSpy(dialog.closed)
        dialog.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.assertEqual(len(closed), 1)
        self.assertEqual(dialog.result(), QDialog.Rejected)

    def test_arrow_keys_switch_images_while_zoomed_view_has_focus(self):
        dialog, paths = self.make(3)
        try:
            dialog.view.zoom_by(4.0)
            dialog.view.setFocus()
            QTest.keyClick(dialog.view, Qt.Key_Right)
            self.assertEqual(dialog.current_path(), paths[1])
            QTest.keyClick(dialog.view, Qt.Key_Left)
            self.assertEqual(dialog.current_path(), paths[0])
        finally:
            dialog.close()

    def test_wheel_zoom_and_reset(self):
        dialog, _paths = self.make(1)
        try:
            self.assertAlmostEqual(dialog.view.zoom_ratio(), 1.0, places=2)
            event = QWheelEvent(QPoint(150, 120), QPoint(150, 120), QPoint(0, 0),
                                QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
                                Qt.NoScrollPhase, False)
            dialog.view.wheelEvent(event)
            self.assertGreater(dialog.view.zoom_ratio(), 1.0)
            for _ in range(8):
                dialog.view.wheelEvent(QWheelEvent(
                    QPoint(150, 120), QPoint(150, 120), QPoint(0, 0), QPoint(0, -120),
                    Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
            self.assertGreaterEqual(dialog.view.zoom_ratio(), 0.79)
            dialog.view.reset_zoom()
            self.assertAlmostEqual(dialog.view.zoom_ratio(), 1.0, places=2)
            self.assertEqual(dialog.view.dragMode(), QGraphicsView.ScrollHandDrag)
        finally:
            dialog.close()

    def test_zoom_creates_pan_range(self):
        dialog, _paths = self.make(1)
        try:
            dialog.view.zoom_by(4.0)
            self.app.processEvents()
            scrollbars = (dialog.view.horizontalScrollBar(), dialog.view.verticalScrollBar())
            self.assertTrue(any(bar.maximum() > 0 for bar in scrollbars))
        finally:
            dialog.close()

    def test_missing_file_is_safe_and_has_no_image(self):
        missing = str(self.root / 'absent.png')
        dialog = ImageLightbox(None, [missing], 0)
        dialog.resize(800, 600)
        dialog.show()
        self.app.processEvents()
        try:
            self.assertEqual(dialog.counter_label.text(), '1 / 1')
            self.assertIsNone(dialog.view._item)
            dialog.view.zoom_by(1.25)
            self.assertAlmostEqual(dialog.view.zoom_ratio(), 1.0, places=2)
        finally:
            dialog.close()


    def test_parent_sizing_metadata_arrows_and_reset_at_four_scales(self):
        from PyQt5.QtWidgets import QWidget
        from tests.qt_scale_support import reset_interface_scaling
        from ui.tokens import px, set_interface_scale
        from ui.theme import apply_ui_mode
        parent = QWidget()
        try:
            for scale in (1., 1.25, 1.5, 1.75):
                set_interface_scale(scale)
                parent.resize(px(1300), px(900))
                parent.show()
                paths = [self.image('wide.png', (240,120)), self.image('tall.png', (90,160))]
                dialog = ImageLightbox.show_images(parent, paths)
                self.app.processEvents()
                try:
                    self.assertEqual(dialog.size(), parent.frameGeometry().size())
                    self.assertIn('240 × 120', dialog.metadata_label.text())
                    self.assertIn('KB', dialog.metadata_label.text())
                    self.assertFalse(dialog.prev_button.isEnabled())
                    for button in (dialog.prev_button, dialog.next_button):
                        self.assertEqual(button.width(), px(40))
                        self.assertLessEqual(abs(button.geometry().center().y()-dialog.body.rect().center().y()), 1)
                    self.assertEqual(dialog.prev_button.x(), px(24))
                    self.assertEqual(dialog.body.width()-dialog.next_button.geometry().right()-1, px(24))
                    QTest.mouseClick(dialog.next_button, Qt.LeftButton)
                    self.app.processEvents()
                    self.assertIn('90 × 160', dialog.metadata_label.text())
                    self.assertEqual(dialog.counter_label.text(), '2 / 2')
                    self.assertFalse(dialog.next_button.isEnabled())
                    image_center = dialog.view.mapFromScene(
                        dialog.view._item.sceneBoundingRect().center())
                    viewport_center = dialog.view.viewport().rect().center()
                    self.assertLessEqual(abs(image_center.x() - viewport_center.x()), 2)
                    self.assertLessEqual(abs(image_center.y() - viewport_center.y()), 2)
                    dialog.view.zoom_by(2)
                    QTest.mouseDClick(dialog.view.viewport(), Qt.LeftButton)
                    self.assertAlmostEqual(dialog.view.zoom_ratio(), 1., places=2)
                    for theme in ('light','dark'):
                        apply_ui_mode(theme)
                        dialog.refresh_colors()
                        self.assertTrue(dialog.view.styleSheet())
                    dialog.set_images([str(self.root/'missing.png')])
                    self.assertEqual(dialog.metadata_label.text(), '— · —')
                finally:
                    dialog.close()
                    dialog.deleteLater()
                    self.app.processEvents()
        finally:
            parent.close()
            reset_interface_scaling(self.app)
            apply_ui_mode('light')



class ImagePickerHelperTests(_QtCase):
    def test_parse_input_lines(self):
        self.assertEqual(parse_input_lines(' a.png \n\n  b.jpg\nc'), ['a.png', 'b.jpg', 'c'])
        self.assertEqual(parse_input_lines(''), [])

    def test_scan_gallery_images(self):
        gallery = self.root / 'gallery'
        gallery.mkdir()
        for name in ('b.JPG', 'a.png', 'note.txt', 'sub'):
            target = gallery / name
            if name != 'sub':
                target.write_bytes(PNG)
        (gallery / 'sub').mkdir()
        images = [Path(path).name for path in scan_gallery_images(gallery)]
        self.assertEqual(images, ['a.png', 'b.JPG'])
        self.assertEqual(scan_gallery_images(self.root / 'absent'), [])

    def test_resolve_local_files_urls_errors_and_limit(self):
        local = self.image('local.png')
        cache = self.root / 'cache'
        downloads = []

        def fetcher(url):
            downloads.append(url)
            return solid_png(10, 10), 'image/png'

        paths, errors = resolve_input_entries(
            [local, 'https://example.invalid/a.png'], cache, fetcher=fetcher)
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], local)
        self.assertTrue(Path(paths[1]).is_file())
        self.assertEqual(errors, [])
        self.assertEqual(downloads, ['https://example.invalid/a.png'])

        paths, errors = resolve_input_entries(
            [str(self.root / 'missing.png')], cache, fetcher=fetcher)
        self.assertEqual(paths, [])
        self.assertTrue(errors)
        self.assertIn('找不到本地图片', errors[0])

        def broken(url):
            raise ValueError('boom')

        paths, errors = resolve_input_entries(
            ['https://example.invalid/b.png'], cache, fetcher=broken)
        self.assertEqual(paths, [])
        self.assertTrue(errors)
        self.assertIn('图片链接下载失败', errors[0])

        paths, errors = resolve_input_entries(
            [local, local, local], cache, fetcher=fetcher, max_count=1)
        self.assertEqual(paths, [local])
        self.assertTrue(any('最多选择 1 张' in message for message in errors))


class PickerDialogTests(_QtCase):
    def setUp(self):
        super().setUp()
        self.config = ConfigManager(self.root / 'config.json')

    def test_gallery_dialog_lists_images_and_persists_directory(self):
        gallery = self.root / 'gallery'
        gallery.mkdir()
        (gallery / 'a.png').write_bytes(PNG)
        (gallery / 'b.png').write_bytes(PNG)
        dialog = GalleryPickerDialog(None, self.config, max_count=6)
        try:
            self.assertEqual(dialog.grid.tiles(), [])
            with patch('ui.components.image_pickers.QFileDialog.getExistingDirectory',
                       return_value=str(gallery)):
                dialog.choose_directory()
            self.assertEqual(self.config.config['ecommerce']['gallery_dir'], str(gallery))
            self.assertEqual(len(dialog.grid.tiles()), 2)
            tile = dialog.grid.tiles()[0]
            QTest.mouseClick(tile, Qt.LeftButton)
            self.assertEqual(dialog.collect(), [tile.path])
            dialog.grid.clear_selection()
            self.assertEqual(dialog.collect(), [])
        finally:
            dialog.close()

    def test_used_image_picker_reads_succeeded_items_only(self):
        store = self.config.ecommerce_store()
        output = self.root / 'output'
        output.mkdir()
        good = self.image('result.png')
        store.upsert_group(dict(group_id='g1', status='succeeded', product_name='茶杯',
                                platforms=[], output_dir=str(output),
                                config=dict(product_info='信息', carousel_count=1,
                                            detail_count=0)))
        store.upsert_items_many([
            dict(group_id='g1', kind='carousel', seq=1, status='succeeded', file_path=good),
            dict(group_id='g1', kind='carousel', seq=2, status='failed', file_path=''),
        ])
        dialog = UsedImagePickerDialog(None, store, max_count=6)
        try:
            tiles = dialog.grid.tiles()
            self.assertEqual(len(tiles), 1)
            self.assertEqual(tiles[0].path, good)
            QTest.mouseClick(tiles[0], Qt.LeftButton)
            self.assertEqual(dialog.collect(), [good])
            self.assertIn('茶杯', tiles[0].label_text)
        finally:
            dialog.close()

    def test_url_dialog_resolves_and_reports_chinese_errors(self):
        local = self.image('local.png')

        def fetcher(url):
            return solid_png(20, 20), 'image/png'

        dialog = UrlImageDialog(None, self.root / 'cache', fetcher=fetcher, max_count=6)
        try:
            dialog.editor.setPlainText(
                'https://example.invalid/a.png\n' + local)
            self.assertEqual(len(dialog.collect()), 2)
            dialog.editor.setPlainText(str(self.root / 'missing.png'))
            self.assertEqual(dialog.collect(), [])
            self.assertTrue(dialog.error_label.isVisibleTo(dialog))
            self.assertIn('找不到本地图片', dialog.error_label.text())
            dialog.editor.setPlainText('')
            self.assertEqual(dialog.collect(), [])
            self.assertIn('请输入至少一个', dialog.error_label.text())
        finally:
            dialog.close()


class ResultPanelPreviewTests(_QtCase):
    def test_thumbnail_click_requests_lightbox_with_group_images(self):
        panel = EcommerceResultPanel()
        paths = [self.image(f'r{index}.png', (40, 40)) for index in range(3)]
        for index, path in enumerate(paths):
            panel.update_item(dict(item_id=f'g1:carousel:{index + 1}', group_id='g1',
                                   kind='carousel', seq=index + 1, status='succeeded',
                                   file_path=path, warnings=[]))
        panel.resize(900, 900)
        panel.show()
        self.app.processEvents()
        try:
            previews = QSignalSpy(panel.preview_requested)
            card = panel.ordered_cards()[1]
            self.assertTrue(card._has_image)
            QTest.mouseClick(card.thumbnail, Qt.LeftButton)
            self.assertEqual(len(previews), 1)
            emitted_paths, emitted_index = previews[0][0], previews[0][1]
            self.assertEqual(list(emitted_paths), paths)
            self.assertEqual(emitted_index, 1)
        finally:
            panel.close()
            panel.deleteLater()
            self.app.processEvents()

    def test_whole_card_click_previews_successful_group_in_carousel_detail_order(self):
        panel = EcommerceResultPanel()
        paths = [self.image(f'ordered-{i}.png') for i in range(4)]
        for index, (kind,seq,status) in enumerate((('detail',2,'succeeded'),('carousel',2,'succeeded'),
                                                  ('carousel',1,'succeeded'),('detail',1,'failed'))):
            panel.update_item(dict(item_id=str(index),kind=kind,seq=seq,status=status,file_path=paths[index]))
        panel.resize(600,400)
        panel.show()
        self.app.processEvents()
        previews = QSignalSpy(panel.preview_requested)
        QTest.mouseClick(panel.cards['1'],Qt.LeftButton,pos=QPoint(1,10))
        self.assertEqual(len(previews),1)
        self.assertEqual(previews[0],[ [paths[2],paths[1],paths[0]], 1])
        panel.close()
        panel.deleteLater()

    def test_failed_card_click_does_not_request_preview(self):
        panel = EcommerceResultPanel()
        panel.update_item(dict(item_id='g2:carousel:1', group_id='g2', kind='carousel',
                               seq=1, status='failed', error='失败', warnings=[]))
        previews = QSignalSpy(panel.preview_requested)
        card = panel.ordered_cards()[0]
        self.assertFalse(card._has_image)
        QTest.mouseClick(card.thumbnail, Qt.LeftButton)
        self.assertEqual(len(previews), 0)
        panel.deleteLater()
        self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
