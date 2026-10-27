"""生成中提交格与共用异步缩略图的离线 offscreen 回归。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PyQt5.QtCore import Qt, QThreadPool
from PyQt5.QtGui import QImage
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication

from core.i18n import set_language, tr
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from tests.test_ecommerce_task_list import solid_png
from ui import materials, tokens
from ui.components import image_async
from ui.components.ecommerce_submission_view import EcommerceSubmissionView
from ui.components.ecommerce_task_list import _Thumbnail


class EcommerceSubmissionViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        set_language('zh-CN')
        QThreadPool.globalInstance().waitForDone(3000)
        self.app.processEvents()
        image_async.clear_image_cache()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'local-image.png'
        self.path.write_bytes(solid_png(640, 360, (20, 90, 160)))
        self.view = EcommerceSubmissionView()
        self.view.resize(600, 500)
        self.view.show()

    def tearDown(self):
        QThreadPool.globalInstance().waitForDone(3000)
        self.app.processEvents()
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()
        set_language('zh-CN')
        reset_interface_scaling(self.app)
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=2000):
        deadline = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), '后台缩略图未在限定时间内完成')

    @staticmethod
    def items():
        return [dict(kind='detail' if index >= 6 else 'carousel',
                     seq=index - 5 if index >= 6 else index + 1, status='pending')
                for index in range(10)]

    def test_ten_cells_are_ordered_numbered_and_badged_without_rebuild(self):
        self.view.set_items(list(reversed(self.items())))
        self.assertEqual(len(self.view.cells), 10)
        for index, key in enumerate(sorted(self.view.cells,
                                          key=lambda key: (key[0] != 'carousel', key[1])), 1):
            cell = self.view.cells[key]
            self.assertEqual(cell.number_label.text(), str(index))
            self.assertEqual(cell.badge_label.text(), ('轮' if key[0] == 'carousel' else '详') + str(key[1]))
            self.assertEqual(cell.property('cellState'), 'pending')
            self.assertEqual(cell.status_label.text(), '排队中')
            self.assertEqual(cell.objectName(), 'ecommerceSubmitCell')
        previous = {key: id(cell) for key, cell in self.view.cells.items()}
        self.view.set_items(self.items())
        self.assertEqual(previous, {key: id(cell) for key, cell in self.view.cells.items()})

    def test_running_failed_and_retry_use_row_key_signals(self):
        self.view.set_items([dict(kind='carousel', seq=1, status='running'),
                             dict(kind='detail', seq=2, status='failed', error='本地失败')])
        running = self.view.cells[('carousel', 1)]
        failed = self.view.cells[('detail', 2)]
        self.assertEqual(running.property('cellState'), 'running')
        self.assertEqual(running.status_label.text(), '生成中')
        self.assertEqual(failed.property('cellState'), 'failed')
        self.assertEqual(failed.status_label.text(), '失败')
        self.assertEqual(failed.toolTip(), '本地失败')
        spy = QSignalSpy(self.view.retry_requested)
        QTest.mouseClick(failed.retry_button, Qt.LeftButton)
        self.assertEqual(list(spy), [[('detail', 2)]])
        QTest.mouseClick(failed, Qt.LeftButton)
        self.assertEqual(list(spy), [[('detail', 2)], [('detail', 2)]])

    def test_item_updates_and_identical_snapshots_do_not_rebuild_or_repaint_other_cells(self):
        records = self.items()
        self.view.set_items(records)
        previous = {key: id(cell) for key, cell in self.view.cells.items()}
        untouched = self.view.cells[('carousel', 2)]
        changed = self.view.cells[('carousel', 1)]
        with patch.object(self.view, '_arrange_cells', wraps=self.view._arrange_cells) as arrange, \
                patch.object(untouched, 'update_record', wraps=untouched.update_record) as other_update, \
                patch.object(changed.thumbnail, '_render', wraps=changed.thumbnail._render) as paint:
            self.view.set_items(records)
            self.view.update_item(dict(records[0], status='running'))
            records[0] = dict(records[0], status='running')
            self.view.set_items(records)
            renders = paint.call_count
            changed.update_record(records[0])
            self.assertEqual(paint.call_count, renders)
            arrange.assert_not_called()
            other_update.assert_not_called()
        self.assertEqual(previous, {key: id(cell) for key, cell in self.view.cells.items()})
        self.assertEqual(changed.property('cellState'), 'running')

    def test_success_uses_background_decode_and_keeps_gui_reads_zero(self):
        gui_thread = threading.get_ident()
        reads = []
        release = threading.Event()
        original = Path.read_bytes

        def background_read(path):
            reads.append(threading.get_ident())
            release.wait(2)
            return original(path)

        with patch.object(Path, 'read_bytes', background_read):
            try:
                self.view.set_items([dict(kind='carousel', seq=1, status='succeeded', file=str(self.path))])
                thumb = self.view.cells[('carousel', 1)].thumbnail
                self.assertFalse(thumb.has_image)
                release.set()
                self.wait_for(lambda: thumb.has_image)
                self.assertFalse(thumb.pixmap().isNull())
                self.assertEqual(thumb.property('thumbLoaded'), True)
                for size in (110, 135, 160):
                    thumb.resize(size, 80)
                    self.app.processEvents()
            finally:
                release.set()
                QThreadPool.globalInstance().waitForDone(3000)
        self.assertEqual(len(reads), 1)
        self.assertTrue(all(thread != gui_thread for thread in reads))
        cached = image_async.cached_image(image_async.image_cache_key(str(self.path)))
        self.assertLessEqual(max(cached.width(), cached.height()), 240)

    def test_task_list_and_submission_share_one_pending_decode(self):
        release = threading.Event()
        original = image_async._decode_image

        def blocked(path, size):
            release.wait(2)
            return original(path, size)

        task_thumb = _Thumbnail(self.view)
        with patch.object(image_async, '_decode_image', side_effect=blocked) as decode:
            try:
                task_thumb.set_source(str(self.path))
                self.view.set_items([dict(kind='carousel', seq=1, status='succeeded', file=str(self.path))])
                thumb = self.view.cells[('carousel', 1)].thumbnail
                release.set()
                self.wait_for(lambda: task_thumb.has_image and thumb.has_image)
                self.assertEqual(decode.call_count, 1)
            finally:
                release.set()
                QThreadPool.globalInstance().waitForDone(3000)

    def test_success_resize_keeps_grid_height_stable_without_recursive_render(self):
        self.view.set_items([dict(kind='carousel', seq=1, status='succeeded', file=str(self.path))])
        thumb = self.view.cells[('carousel', 1)].thumbnail
        self.wait_for(lambda: thumb.has_image)
        flush_layout(self.app, self.view)
        baseline = self.view.minimumSizeHint().height()
        with patch.object(thumb, 'setStyleSheet', wraps=thumb.setStyleSheet) as restyle:
            for width in (520, 680, 600) * 5:
                self.view.resize(width, 500)
                flush_layout(self.app, self.view)
                self.assertEqual(self.view.height(), 500)
                self.assertLessEqual(thumb.height(), self.view.height())
                self.assertEqual(self.view.minimumSizeHint().height(), baseline)
            restyle.assert_not_called()

    def test_missing_and_corrupt_success_files_show_plain_missing_placeholder(self):
        corrupt = Path(self.temp.name) / 'corrupt.png'
        corrupt.write_bytes(b'local-invalid-image-fixture')
        for path in (Path(self.temp.name) / 'missing.png', corrupt):
            with self.subTest(path=str(path)):
                self.view.set_items([dict(kind='carousel', seq=1, status='succeeded', file=str(path))])
                thumb = self.view.cells[('carousel', 1)].thumbnail
                self.wait_for(lambda: thumb.text() == tr('缺失'))
                self.assertFalse(thumb.has_image)
                self.assertEqual(thumb.textFormat(), Qt.PlainText)
                self.assertEqual(thumb.toolTip(), str(path))
        self.view.set_items([dict(kind='carousel', seq=1, status='succeeded', file='')])
        thumb = self.view.cells[('carousel', 1)].thumbnail
        self.assertEqual(thumb.text(), tr('缺失'))
        self.assertFalse(thumb.has_image)

    def test_progress_cancel_and_clear(self):
        self.view.set_items(self.items())
        self.view.set_progress(10, 6, 2)
        self.assertEqual(self.view.progress_label.text(), '已完成 6/10（失败 2）')
        spy = QSignalSpy(self.view.cancel_requested)
        QTest.mouseClick(self.view.cancel_button, Qt.LeftButton)
        self.assertEqual(len(spy), 1)
        self.view.clear()
        self.assertEqual(self.view.cells, {})
        self.assertEqual(self.view.progress_label.text(), '已完成 0/0（失败 0）')

    def test_resize_debounces_columns_and_supported_scales(self):
        self.view.set_items(self.items())
        for scale in (1.0, 1.25, 1.5, 1.75):
            with self.subTest(scale=scale):
                tokens.set_interface_scale(scale)
                self.view.refresh_interface_scale()
                self.view.resize(tokens.px(380), tokens.px(700))
                QTest.qWait(130)
                flush_layout(self.app, self.view)
                self.assertEqual(self.view.column_count, 3)
                self.assertEqual(self.view.title_label.font().pixelSize(), tokens.px(13))
                self.assertEqual(self.view.cells[('carousel', 1)].badge_label.font().pixelSize(), tokens.px(11))
                narrow = self.view.grid_layout.getItemPosition(
                    self.view.grid_layout.indexOf(self.view.cells[('carousel', 4)]))
                self.assertEqual(narrow[:2], (1, 0))
                self.view.resize(tokens.px(760), tokens.px(700))
                QTest.qWait(130)
                flush_layout(self.app, self.view)
                self.assertEqual(self.view.column_count, 6)
                wide = self.view.grid_layout.getItemPosition(
                    self.view.grid_layout.indexOf(self.view.cells[('carousel', 4)]))
                self.assertEqual(wide[:2], (0, 3))
                self.assertTrue(all(cell.geometry().right() <= self.view.width()
                                    for cell in self.view.cells.values()))

    def test_language_bus_retranslates_progress_badges_and_failure(self):
        self.view.set_items([dict(kind='carousel', seq=2, status='pending'),
                             dict(kind='detail', seq=1, status='failed')])
        self.view.set_progress(3, 1, 1)
        set_language('en-US')
        self.assertEqual(self.view.title_label.text(), 'Generating')
        self.assertEqual(self.view.cancel_button.text(), tr('取消'))
        self.assertEqual(self.view.progress_label.text(), tr('已完成 {done}/{total}（失败 {failed}）').format(
            done=1, total=3, failed=1))
        self.assertEqual(self.view.cells[('carousel', 2)].badge_label.text(), tr('轮{seq}').format(seq=2))
        self.assertEqual(self.view.cells[('detail', 1)].retry_button.text(), tr('重试'))

    def test_public_loader_delivers_gui_callbacks_reuses_cache_and_invalidates_mtime(self):
        callbacks = []
        gui_thread = threading.get_ident()

        def complete(key, path, image):
            callbacks.append((key, path, image, threading.get_ident()))

        key = image_async.load_image_async(str(self.path), (240, 240), complete)
        self.wait_for(lambda: len(callbacks) == 1)
        self.assertEqual(callbacks[0][3], gui_thread)
        self.assertFalse(callbacks[0][2].isNull())
        with patch.object(image_async, '_decode_image', side_effect=AssertionError('cache miss')):
            image_async.load_image_async(str(self.path), (240, 240), complete)
        self.assertEqual(len(callbacks), 2)
        self.assertEqual(callbacks[1][3], gui_thread)
        previous = self.path.stat().st_mtime_ns
        self.path.write_bytes(solid_png(120, 60, (190, 40, 50)))
        os.utime(self.path, ns=(previous + 1_000_000, previous + 1_000_000))
        next_key = image_async.load_image_async(str(self.path), (240, 240), complete)
        self.assertNotEqual(key, next_key)
        self.wait_for(lambda: len(callbacks) == 3)
        self.assertEqual(callbacks[-1][2].pixelColor(0, 0).getRgb()[:3], (190, 40, 50))

    def test_decode_exception_returns_missing_and_clears_pending_subscription(self):
        with patch.object(image_async, '_decode_image', side_effect=OSError('local fixture')):
            self.view.set_items([dict(kind='detail', seq=1, status='succeeded', file=str(self.path))])
            thumb = self.view.cells[('detail', 1)].thumbnail
            self.wait_for(lambda: thumb.text() == tr('缺失'))
        self.assertEqual(thumb.toolTip(), str(self.path))
        with image_async._LOCK:
            self.assertNotIn(image_async.image_cache_key(str(self.path)), image_async._PENDING)

    def test_same_name_replacement_during_async_request_accepts_latest_version(self):
        original = image_async.load_image_async
        previous = self.path.stat().st_mtime_ns
        replaced = False

        def replace_before_request(path, size, callback):
            nonlocal replaced
            if not replaced:
                replaced = True
                self.path.write_bytes(solid_png(160, 80, (210, 30, 50)))
                os.utime(self.path, ns=(previous + 1_000_000, previous + 1_000_000))
            return original(path, size, callback)

        with patch.object(image_async, 'load_image_async', side_effect=replace_before_request):
            self.view.set_items([dict(kind='carousel', seq=1, status='succeeded', file=str(self.path))])
            thumb = self.view.cells[('carousel', 1)].thumbnail
            self.wait_for(lambda: thumb.has_image)
        self.assertEqual(thumb._key, image_async.image_cache_key(str(self.path)))
        self.assertEqual(thumb._image.pixelColor(0, 0).getRgb()[:3], (210, 30, 50))


if __name__ == '__main__':
    unittest.main()
