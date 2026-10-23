"""Offline coverage for cached, debounced ecommerce result thumbnails."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QElapsedTimer, QEventLoop
from PyQt5.QtGui import QColor, QImage, QResizeEvent
from PyQt5.QtWidgets import QApplication

from core.i18n import set_language
from tests.qt_scale_support import reset_interface_scaling
from ui import materials
from ui.components.ecommerce_result_panel import (
    EcommerceResultCard, EcommerceResultPanel, _THUMBNAIL_CACHE, _load_thumbnail_image,
)
from ui.tokens import px, set_interface_scale


class EcommerceResultPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        set_language('zh-CN')
        _THUMBNAIL_CACHE.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(_THUMBNAIL_CACHE.clear)
        self.addCleanup(reset_interface_scaling, self.app)
        self.widgets = []
        self.addCleanup(self._dispose_widgets)

    def _dispose_widgets(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def _wait_for(self, predicate, timeout=3000):
        clock = QElapsedTimer()
        clock.start()
        while not predicate() and clock.elapsed() < timeout:
            self.app.processEvents(QEventLoop.AllEvents, 20)
        self.assertTrue(predicate(), 'thumbnail did not reach its settled state')

    def _image(self, name='portrait.png', color='#dc2345', size=(1024, 1820)):
        path = Path(self.temporary.name, name)
        image = QImage(*size, QImage.Format_RGB32)
        image.fill(QColor(color))
        self.assertTrue(image.save(str(path)))
        return path

    def _card(self, path, **fields):
        record = dict(item_id='item', kind='carousel', seq=1, status='succeeded',
                      file_path=str(path))
        record.update(fields)
        card = EcommerceResultCard(record)
        self.widgets.append(card)
        return card

    def _settle(self, card):
        self.app.processEvents()
        self._wait_for(lambda: not card.thumbnail._resize_timer.isActive())

    def test_resize_burst_is_debounced_and_decodes_only_once(self):
        path = self._image()
        read_bytes = Path.read_bytes
        with patch.object(Path, 'read_bytes', autospec=True, side_effect=read_bytes) as read:
            card = self._card(path)
            card.resize(300, 750)
            card.show()
            self._settle(card)
            renders = []
            card.thumbnail.render_requested.connect(lambda: renders.append(True))
            for index in range(10):
                previous = card.thumbnail.size()
                card.thumbnail.resize(270 + index, card.thumbnail.height())
                card.thumbnail.resizeEvent(QResizeEvent(card.thumbnail.size(), previous))
            self.assertEqual(renders, [])
            self._wait_for(lambda: len(renders) == 1)
            self.assertEqual(read.call_count, 1)
            card._render_thumbnail()
            self.assertEqual(read.call_count, 1)

    def test_regeneration_mtime_invalidates_same_filename_without_stale_resident_entry(self):
        path = self._image(color='#dc2345')
        read_bytes = Path.read_bytes
        with patch.object(Path, 'read_bytes', autospec=True, side_effect=read_bytes) as read:
            first = _load_thumbnail_image(path)
            old_stat = path.stat()
            self._image(color='#2255dc')
            os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))
            second = _load_thumbnail_image(path)
            self.assertEqual(first.pixelColor(0, 0), QColor('#dc2345'))
            self.assertEqual(second.pixelColor(0, 0), QColor('#2255dc'))
            self.assertEqual(read.call_count, 2)
            self.assertEqual(len(_THUMBNAIL_CACHE), 1)

    def test_decode_cache_is_bounded_lru_and_limits_long_edge(self):
        first = self._image('first.png', size=(2400, 2000))
        decoded = _load_thumbnail_image(first)
        self.assertEqual(max(decoded.width(), decoded.height()), 1600)
        for index in range(23):
            _load_thumbnail_image(self._image(f'{index}.png', size=(12, 10)))
        _load_thumbnail_image(first)
        _load_thumbnail_image(self._image('last.png', size=(12, 10)))
        paths = [key[0] for key in _THUMBNAIL_CACHE]
        self.assertEqual(len(paths), 24)
        self.assertIn(str(first.resolve()), paths)
        self.assertNotIn(str(Path(self.temporary.name, '0.png').resolve()), paths)

    def test_adaptive_cover_and_fixed_height_contain_preserve_rounding_and_image(self):
        card = self._card(self._image())
        card.resize(360, 800)
        card.show()
        self._settle(card)
        red = QColor('#dc2345')
        cover = card.thumbnail.pixmap().toImage()
        self.assertEqual(cover.pixelColor(10, cover.height() // 2), red)
        self.assertEqual(cover.pixelColor(0, 0).alpha(), 0)
        card.apply_adaptive(False)
        self._settle(card)
        self.assertFalse(card.thumbnail.hasHeightForWidth())
        self.assertFalse(card.thumbnail.sizePolicy().hasHeightForWidth())
        self.assertEqual(card.thumbnail.height(), px(260))
        contained = card.thumbnail.pixmap().toImage()
        self.assertEqual(contained.pixelColor(10, contained.height() // 2),
                         QColor(materials.palette()['bg2']))
        self.assertEqual(contained.pixelColor(contained.width() // 2, contained.height() // 2), red)
        card.apply_adaptive(True)
        self._settle(card)
        self.assertTrue(card.thumbnail.hasHeightForWidth())
        self.assertEqual(card.thumbnail.height(), card.thumbnail.heightForWidth(card.thumbnail.width()))

    def test_adaptive_switch_reads_and_persists_setting_and_applies_to_every_card(self):
        config = {'ecommerce': {'thumbnail_adaptive': False}}
        manager = SimpleNamespace(config=config, update=Mock())
        panel = EcommerceResultPanel(config_manager=manager)
        self.widgets.append(panel)
        for index, status in enumerate(('pending', 'failed', 'succeeded')):
            panel.update_item(dict(item_id=str(index), kind='carousel', seq=index + 1,
                                   status=status, file_path=str(self._image(f'{index}.png'))))
        self.assertFalse(panel.adaptive_switch.isChecked())
        self.assertTrue(all(not card.thumbnail.hasHeightForWidth() for card in panel.cards.values()))
        panel.adaptive_switch.setChecked(True)
        manager.update.assert_called_once_with(('ecommerce', 'thumbnail_adaptive'), True)
        self.assertTrue(all(card.thumbnail.hasHeightForWidth() for card in panel.cards.values()))
        panel.set_adaptive(False)
        self.assertTrue(all(card.thumbnail.minimumHeight() == px(260) == card.thumbnail.maximumHeight()
                            for card in panel.cards.values()))
        self.assertEqual(manager.update.call_count, 2)

    def test_fixed_height_is_reapplied_at_each_interface_scale(self):
        card = self._card(self._image())
        card.apply_adaptive(False)
        for scale in (1.0, 1.25, 1.5, 1.75):
            with self.subTest(scale=scale):
                set_interface_scale(scale)
                card.refresh_interface_scale()
                self.assertEqual(card.thumbnail.minimumHeight(), round(260 * scale))
                self.assertEqual(card.thumbnail.maximumHeight(), round(260 * scale))

    def test_dpr_render_uses_physical_pixels_with_logical_thumbnail_dimensions(self):
        card = self._card(self._image())
        card.thumbnail.resize(200, 250)
        with patch.object(card, 'devicePixelRatioF', return_value=2.0):
            card._render_thumbnail()
        pixmap = card.thumbnail.pixmap()
        self.assertEqual(pixmap.devicePixelRatioF(), 2.0)
        self.assertEqual((pixmap.width(), pixmap.height()), (400, 500))

    def test_missing_file_placeholder_and_clear_stop_pending_timer(self):
        panel = EcommerceResultPanel()
        self.widgets.append(panel)
        panel.update_item(dict(item_id='missing', kind='carousel', seq=1,
                               status='succeeded', file_path=str(Path(self.temporary.name, 'missing.png'))))
        card = panel.cards['missing']
        self.assertEqual(card.thumbnail.text(), '文件缺失')
        card.thumbnail._resize_timer.start()
        panel.clear()
        self.assertFalse(card.thumbnail._resize_timer.isActive())
        self.assertEqual(panel.cards, {})


if __name__ == '__main__':
    unittest.main()
