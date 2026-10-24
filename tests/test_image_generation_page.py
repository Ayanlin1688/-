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
from PyQt5.QtWidgets import QApplication, QBoxLayout, QDialog

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
        empty_tile = self.page.reference_grid.add_tile()
        self.assertIsNotNone(empty_tile)
        self.assertIn('最多只能上传8张图片', empty_tile.text_label.text())
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

    def test_model_version_ratio_tier_count_and_quick_prompt(self):
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

    def test_narrow_layout_stacks_right_column_and_wide_layout_restores(self):
        from tests.qt_scale_support import flush_layout

        self.page.resize(1000, 900)
        flush_layout(self.app, self.page)
        self.assertEqual(self.page.body_layout.direction(), QBoxLayout.TopToBottom,
                         f'actual page width={self.page.width()}, min={self.page.minimumWidth()}')
        self.assertGreater(self.page.right_column.geometry().top(),
                           self.page.left_column.geometry().bottom())
        self.page.resize(1600, 900)
        flush_layout(self.app, self.page)
        self.assertEqual(self.page.body_layout.direction(), QBoxLayout.LeftToRight)
        self.assertLess(self.page.left_column.geometry().right(),
                        self.page.right_column.geometry().left())

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


if __name__ == '__main__':
    unittest.main()
