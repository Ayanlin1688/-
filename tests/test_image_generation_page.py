"""Offline UI tests for the standalone image generation form."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QImage
from PyQt5.QtWidgets import QApplication, QBoxLayout, QDialog, QFrame

from core.config_manager import ConfigManager
from core.i18n import set_language
from ui.pages.image_generation_page import DEFAULT_QUICK_PROMPTS, ImageGenerationPage


class ImageGenerationPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ConfigManager(self.root / 'config.json')
        self.config.model_catalog_controller = Mock()
        self.config.model_catalog_controller.snapshot_for.return_value = {
            'image-model': {
                'id': 'image-model', 'display_name': 'Image Model', 'kind': 'image',
                'available': True, 'versions': ['image-model-v1', 'image-model-v2'],
            },
        }
        self.page = ImageGenerationPage(self.config)
        self.page.resize(1440, 900)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()
        set_language('zh-CN')

    def _make_image(self, name):
        path = self.root / name
        image = QImage(4, 4, QImage.Format_RGB32)
        image.fill(QColor(40, 120, 200))
        self.assertTrue(image.save(str(path), 'PNG'))
        return str(path)

    def test_header_prompt_limit_and_clear_visibility(self):
        self.assertEqual(self.page.title_label.text(), '图片生成')
        self.assertEqual(self.page.prompt_count_label.text(), '0 / 10000')
        self.assertFalse(self.page.clear_prompt_button.isVisible())
        self.page.prompt_edit.setPlainText('x' * 10005)
        self.app.processEvents()
        self.assertEqual(len(self.page.prompt_edit.toPlainText()), 10000)
        self.assertEqual(self.page.prompt_count_label.text(), '10000 / 10000')
        self.assertTrue(self.page.clear_prompt_button.isVisible())
        self.page.clear_prompt_button.click()
        self.assertEqual(self.page.prompt_edit.toPlainText(), '')
        self.assertFalse(self.page.clear_prompt_button.isVisible())
        self.assertFalse(self.page.submit_button.isEnabled())

    def test_reference_file_selection_limit_remove_clear_and_preview(self):
        self.assertTrue(self.page.reference_drop_strip.isVisible())
        self.assertEqual(
            self.page.reference_drop_strip.text_label.text(),
            '点击上传图片 (支持 JPG、PNG、WEBP 格式，最多只能上传8张图片)')
        paths = [self._make_image(f'image-{index}.png') for index in range(10)]
        with patch('ui.pages.image_generation_page.QFileDialog.getOpenFileNames',
                   return_value=(paths, '')) as picker, \
                patch.object(self.page, '_warning') as warning, \
                patch.object(self.page, '_preview_reference') as preview:
            self.page.choose_reference_files()
        picker.assert_called_once()
        warning.assert_called_once()
        self.assertEqual(len(self.page.reference_paths), 8)
        self.assertEqual(self.page.image_grid.count(), 8)
        self.assertTrue(self.page.clear_images_button.isVisible())
        from ui.components.image_lightbox import ImageLightbox
        with patch.object(ImageLightbox, 'show_images', return_value=object()) as show_images:
            self.page.reference_grid.tile_at(2).clicked.emit(2)
        show_images.assert_called_once_with(self.page, self.page.reference_paths, 2)
        self.page.reference_grid.tile_at(0).removed.emit(0)
        self.assertEqual(len(self.page.reference_paths), 7)
        self.page.clear_images_button.click()
        self.assertEqual(self.page.reference_paths, [])
        self.assertFalse(self.page.clear_images_button.isVisible())

    def test_reference_empty_drop_strip_and_stable_add_label(self):
        strip = self.page.reference_drop_strip
        self.assertEqual(strip.objectName(), 'imageReferenceDropStrip')
        self.assertTrue(strip.isVisible())
        self.assertFalse(self.page.reference_grid.isVisible())
        self.assertEqual(strip.icon_label.text(), '⬆')
        self.assertNotIn('⬆', strip.text_label.text())
        self.assertEqual(
            strip.text_label.text(),
            '点击上传图片 (支持 JPG、PNG、WEBP 格式，最多只能上传8张图片)')
        with patch('ui.pages.image_generation_page.QFileDialog.getOpenFileNames',
                   return_value=([], '')) as choose:
            strip.clicked.emit()
        choose.assert_called_once()

        image = self._make_image('reference.png')
        self.page.set_references([image])
        self.app.processEvents()
        self.assertFalse(strip.isVisible())
        self.assertTrue(self.page.reference_grid.isVisible())
        self.assertEqual(self.page.reference_grid.add_tile().text_label.text(),
                         '＋ 继续添加 (1/8)')
        self.page.refresh_interface_scale()
        self.assertEqual(self.page.reference_grid.add_tile().text_label.text(),
                         '＋ 继续添加 (1/8)')

    def test_model_version_ratio_tier_count_and_quick_prompt(self):
        self.assertEqual(self.page.model_section_title.text(), '🐱 模型')
        self.assertIsNotNone(self.page.findChild(QFrame, 'imageModelSubcard'))
        self.assertEqual(self.page.model_combo.currentData(), 'image-model')
        self.assertEqual(self.page.version_combo.count(), 2)
        self.assertEqual(self.page.ratio_combo.count(), 8)
        self.assertEqual(self.page.tier_combo.count(), 3)
        self.assertEqual(len(self.page.quick_prompt_buttons), 6)
        self.assertEqual(tuple(button.toolTip() for button in self.page.quick_prompt_buttons),
                         DEFAULT_QUICK_PROMPTS)
        self.page.model_combo.setCurrentIndex(0)
        self.assertEqual(self.page.version_combo.itemText(0), 'image-model-v1')
        self.page.count_spin.setValue(3)
        self.assertEqual(self.page.count_hint.text(), '将循环创建 3 个相同任务')
        self.page.quick_prompt_buttons[1].click()
        self.assertEqual(self.page.prompt_edit.toPlainText(), DEFAULT_QUICK_PROMPTS[1])
        self.assertTrue(self.page.quick_prompt_buttons[1].property('selectedPrompt'))
        self.assertTrue(self.page.submit_button.isEnabled())

    def test_count_stepper_edges_and_hint(self):
        self.assertEqual(self.page.count_spin.value(), 1)
        self.assertFalse(self.page.count_spin.minus_button.isEnabled())
        self.page.count_spin.setValue(10)
        self.assertFalse(self.page.count_spin.plus_button.isEnabled())
        self.assertTrue(self.page.count_spin.minus_button.isEnabled())
        self.page.count_spin.setValue(3)
        self.assertTrue(self.page.count_spin.minus_button.isEnabled())
        self.assertTrue(self.page.count_spin.plus_button.isEnabled())
        self.assertEqual(self.page.count_hint.text(), '将循环创建 3 个相同任务')

    def test_custom_prompts_are_limited_and_persisted(self):
        class AcceptedDialog:
            values = ['my prompt', 'another prompt']

            def __init__(self, *args):
                pass

            def exec_(self):
                return QDialog.Accepted

        with patch('ui.pages.image_generation_page._CustomPromptDialog', AcceptedDialog):
            self.page.open_custom_prompts()
        self.assertEqual(self.config.config['image_generation']['custom_prompts'],
                         ['my prompt', 'another prompt'])
        self.assertEqual(len(self.page.quick_prompt_buttons), 8)
        self.assertEqual(self.page.quick_prompt_buttons[-1].toolTip(), 'another prompt')

    def test_submit_uses_prepare_and_run_in_background_and_refreshes_each_task(self):
        started, release = threading.Event(), threading.Event()

        class FakeManager:
            on_task = None

            def prepare_tasks(inner, **kwargs):
                inner.kwargs = kwargs
                records = [dict(local_id='task-1', status='pending') for _ in range(kwargs['count'])]
                for record in records:
                    if inner.on_task:
                        inner.on_task(record)
                return records

            def run_tasks(inner, records):
                started.set()
                if not release.wait(3):
                    raise AssertionError('fake task gate timed out')
                for record in records:
                    if inner.on_task:
                        inner.on_task(dict(record, status='succeeded'))

        manager = FakeManager()
        self.page._manager = manager
        self.page.prompt_edit.setPlainText('A studio photograph of a red glass vase')
        self.page.count_spin.setValue(2)
        with patch('ui.pages.image_generation_page.InfoBar.success') as success:
            began = time.monotonic()
            self.assertTrue(self.page.submit_generation())
            self.assertLess(time.monotonic() - began, 0.3)
            self.assertTrue(started.wait(2))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not success.called:
                self.app.processEvents()
                time.sleep(0.005)
            success.assert_called_once()
            self.assertIn('已提交 2 个图片生成任务', success.call_args.args[1])
            self.assertEqual(manager.kwargs['model'], 'image-model')
            self.assertEqual(manager.kwargs['model_version'], 'image-model-v1')
            self.assertEqual(manager.kwargs['ratio'], '9:16')
            self.assertEqual(manager.kwargs['tier'], '1K')
            self.assertEqual(manager.kwargs['count'], 2)
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.page._workers:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertFalse(self.page._workers)

    def test_narrow_layout_keeps_columns_and_scrolls_to_right_column(self):
        from tests.qt_scale_support import flush_layout

        self.page.resize(1000, 900)
        flush_layout(self.app, self.page)
        self.assertEqual(self.page.body_layout.direction(), QBoxLayout.LeftToRight,
                         f'actual page width={self.page.width()}, min={self.page.minimumWidth()}')
        self.assertLess(self.page.left_column.geometry().right(),
                        self.page.right_column.geometry().left())
        self.assertGreater(self.page.body_scroll.horizontalScrollBar().maximum(), 0)
        self.page.body_scroll.ensureWidgetVisible(self.page.cta_button)
        flush_layout(self.app, self.page)
        rect = self.page.cta_button.rect()
        point = self.page.cta_button.mapTo(self.page.body_scroll.viewport(), rect.center())
        self.assertTrue(self.page.body_scroll.viewport().rect().contains(point))
        self.page.resize(1600, 900)
        flush_layout(self.app, self.page)
        self.assertEqual(self.page.body_layout.direction(), QBoxLayout.LeftToRight)
        self.assertLess(self.page.left_column.geometry().right(),
                        self.page.right_column.geometry().left())

    def test_scrollable_layout_remains_unclipped_at_supported_scales(self):
        from tests.qt_scale_support import flush_layout, reset_interface_scaling
        from ui.scaling import apply_interface_scale
        from ui.tokens import px

        try:
            for scale in (1.0, 1.25, 1.5, 1.75):
                apply_interface_scale(self.app, self.app.primaryScreen(), scale)
                self.page.resize(1000, 900)
                self.page.refresh_interface_scale()
                flush_layout(self.app, self.page)

                self.assertEqual(self.page.body_layout.direction(), QBoxLayout.LeftToRight)
                self.assertEqual(self.page.body_scroll.horizontalScrollBarPolicy(),
                                 Qt.ScrollBarAsNeeded)
                self.assertGreater(self.page.body_scroll.horizontalScrollBar().maximum(), 0)
                self.assertGreater(self.page.body_content.width(),
                                     self.page.body_scroll.viewport().width())
                self.assertGreaterEqual(self.page.prompt_edit.height(), px(140))

                card_top = self.page.model_section_title.mapTo(self.page, self.page.model_section_title.rect().topLeft()).y()
                card_bottom = self.page.count_spin.mapTo(self.page, self.page.count_spin.rect().bottomRight()).y()
                subcard = self.page.findChild(QFrame, 'imageModelSubcard')
                self.assertGreaterEqual(card_top, subcard.mapTo(self.page, subcard.rect().topLeft()).y())
                self.assertLessEqual(card_bottom, subcard.mapTo(self.page, subcard.rect().bottomRight()).y())
                self.page.resize(1000, 600)
                flush_layout(self.app, self.page)
                self.assertTrue(self.page.body_scroll.verticalScrollBar().isVisible())
                self.page.body_scroll.ensureWidgetVisible(self.page.submit_button, 0, 0)
                flush_layout(self.app, self.page)
                self.assertTrue(self.page.body_scroll.viewport().rect().contains(
                    self.page.submit_button.mapTo(self.page.body_scroll.viewport(),
                                                 self.page.submit_button.rect().center())))
        finally:
            reset_interface_scaling(self.app)

    def test_retranslate_updates_page_labels(self):
        set_language('en')
        self.page.retranslate_ui()
        self.assertEqual(self.page.title_label.text(), 'Image generation')
        self.assertEqual(self.page.caption_label.text(), 'Use AI to generate high-quality images')
        self.assertEqual(self.page.prompt_edit.placeholderText(),
                         'Describe the image you want to generate...')
        set_language('zh-CN')
        self.page.retranslate_ui()
        self.assertEqual(self.page.title_label.text(), '图片生成')


    def test_reference_empty_strip_has_exactly_one_icon_in_both_languages(self):
        strip = self.page.reference_drop_strip
        for language in ('zh-CN', 'en'):
            set_language(language)
            self.page.retranslate_ui()
            self.assertEqual(strip.icon_label.text(), '⬆')
            self.assertNotIn('⬆', strip.text_label.text())
            self.assertTrue(strip.text_label.text())

    def test_catalog_refresh_keeps_model_version_and_removes_unavailable_model(self):
        self.page.version_combo.setCurrentText('image-model-v2')
        self.page.refresh_catalog()
        self.assertEqual(self.page.model_combo.currentData(), 'image-model')
        self.assertEqual(self.page.version_combo.currentText(), 'image-model-v2')
        self.config.model_catalog_controller.snapshot_for.return_value = {
            'replacement-image': dict(id='replacement-image', kind='image', available=True)}
        with patch.object(self.page, '_warning') as warning:
            self.page.refresh_catalog()
        warning.assert_called_once()
        self.assertEqual(self.page.model_combo.currentData(), 'replacement-image')
        self.assertEqual(self.page.model_combo.findData('image-model'), -1)

    def test_both_generate_buttons_click_once_and_share_busy_state_wide_and_narrow(self):
        from PyQt5.QtTest import QTest
        from tests.qt_scale_support import flush_layout
        self.page.prompt_edit.setPlainText('Offline dual button fixture')
        for width in (1600, 1000):
            self.page.resize(width, 900)
            flush_layout(self.app, self.page)
            for button in (self.page.submit_button, self.page.cta_button):
                release, started = threading.Event(), threading.Event()
                manager = Mock()
                manager.prepare_tasks.return_value = [dict(local_id='fixture', status='pending')]
                def run(_records):
                    started.set()
                    release.wait(3)
                manager.run_tasks.side_effect = run
                self.page._manager = manager
                self.page.body_scroll.ensureWidgetVisible(button)
                self.app.processEvents()
                try:
                    self.assertTrue(button.isVisible())
                    self.assertTrue(button.isEnabled())
                    with patch('ui.pages.image_generation_page.InfoBar.success'):
                        QTest.mouseClick(button, Qt.LeftButton)
                        self.assertTrue(started.wait(2))
                        self.assertFalse(self.page.submit_button.isEnabled())
                        self.assertFalse(self.page.cta_button.isEnabled())
                        self.page.prompt_edit.insertPlainText(' ')
                        self.assertFalse(self.page.cta_button.isEnabled())
                        self.assertFalse(self.page.submit_generation())
                        manager.prepare_tasks.assert_called_once()
                        manager.run_tasks.assert_called_once()
                finally:
                    release.set()
                    deadline = time.monotonic()+3
                    while self.page._workers and time.monotonic()<deadline:
                        self.app.processEvents()
                        QTest.qWait(5)
                self.assertFalse(self.page._workers)
                self.assertTrue(self.page.submit_button.isEnabled())
                self.assertTrue(self.page.cta_button.isEnabled())



if __name__ == '__main__':
    unittest.main()
