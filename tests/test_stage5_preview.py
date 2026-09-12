import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QImage
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import PlainTextEdit, SegmentedWidget

from core.config_manager import ConfigManager
from ui.components.match_dialog import MatchDialog
from ui.pages.settings_page import SettingsPage


H3_PROMPT = """subject_definitions:
<Subject 1> is Maya standing beside <Picture 1>.

summary:
[reference generation] Maya shows the product.

retention_analysis:
<Subject 1>: fully_preserved

detailed_description:
[Shot 1] Maya raises <Picture 1>.
[Shot 2] At 00:03.000, cut to the product in her hands.

overall_soundscape:
Quiet room tone.

non_diegetic_music:
N/A"""


class Stage5PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.json'
        self.manager = ConfigManager(self.config_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_on_safety_and_conversion_switches_persist(self):
        page = SettingsPage(self.manager, lambda *_: None)
        page.show()
        try:
            switches = (
                (page.prevent_duplicates, ('task_strategy', 'prevent_duplicates')),
                (page.auto_convert_prompt, ('prompt_conversion', 'enabled')),
                (page.preserve_original_prompt, ('prompt_conversion', 'preserve_original')),
                (page.prefer_same_format, ('prompt_conversion', 'prefer_same_format')),
            )
            self.assertTrue(all(card.isChecked() for card, _ in switches))
            for card, _ in switches:
                card.switchButton.setChecked(False)
            self.app.processEvents()
            reopened = ConfigManager(self.config_path)
            reopened.load_config()
            for _, path in switches:
                self.assertFalse(reopened.config[path[0]][path[1]])
        finally:
            page.close()
            page.deleteLater()

    def test_preview_tabs_and_unsaved_model_change_share_conversion_output(self):
        prompt_path = self.root / 'prompt.txt'
        prompt_path.write_text('\ufeff' + H3_PROMPT, encoding='utf-8')
        dialog = self._dialog(prompt_path)
        try:
            self.assertIsInstance(dialog.preview_tabs, SegmentedWidget)
            self.assertIsInstance(dialog.prompt_preview, PlainTextEdit)
            self.assertTrue(dialog.prompt_preview.isReadOnly())
            self.assertEqual(dialog.preview_tabs.currentRouteKey(), 'original')
            self.assertEqual(dialog.prompt_preview.toPlainText(), H3_PROMPT)

            dialog.model_combo.setCurrentText('video-v2')
            self.app.processEvents()
            self.assertEqual(self.manager.config['model_overrides'], {})
            dialog.preview_tabs.setCurrentItem('converted')
            self.app.processEvents()
            converted = dialog.prompt_preview.toPlainText()
            self.assertIn('人物站位：Maya standing beside @图1.', converted)
            self.assertIn('镜头2：The product in her hands.', converted)
            self.assertNotEqual(converted, H3_PROMPT)

            dialog.preview_tabs.setCurrentItem('original')
            self.app.processEvents()
            self.assertEqual(dialog.prompt_preview.toPlainText(), H3_PROMPT)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_conversion_error_is_visible_and_does_not_escape_model_slot(self):
        prompt_path = self.root / 'broken.txt'
        broken = 'subject_definitions:\n<Subject 1> is Maya.\n[Shot 1] action'
        prompt_path.write_text(broken, encoding='utf-8')
        dialog = self._dialog(prompt_path)
        try:
            dialog.model_combo.setCurrentText('video-v2')
            self.app.processEvents()
            dialog.preview_tabs.setCurrentItem('converted')
            self.app.processEvents()
            preview = dialog.prompt_preview.toPlainText()
            self.assertTrue(preview.startswith('转换失败：'))
            self.assertIn('H3', preview)
            self.assertNotEqual(preview, broken)
            self.assertIn('转换失败', dialog.preview_status.text())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_reset_manual_model_preview_recomputes_auto_without_saving(self):
        prompt_path = self.root / 'manual.txt'
        prompt_path.write_text(H3_PROMPT, encoding='utf-8')
        self.manager.config['model_overrides'][str(prompt_path)] = 'video-v2'
        dialog = self._dialog(prompt_path)
        try:
            dialog.preview_tabs.setCurrentItem('converted')
            self.assertIn('人物站位：', dialog.prompt_preview.toPlainText())
            dialog.model_combo.setCurrentText('')
            self.assertEqual(dialog.prompt_preview.toPlainText(), H3_PROMPT)
            dialog.model_combo.setCurrentText('video-v2')
            dialog.reset_models()
            self.assertEqual(dialog.prompt_preview.toPlainText(), H3_PROMPT)
            self.assertEqual(self.manager.config['model_overrides'][str(prompt_path)], 'video-v2')
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_preview_refresh_preserves_existing_image_order(self):
        prompt_path = self.root / 'prompt.txt'
        prompt_path.write_text(H3_PROMPT, encoding='utf-8')
        images = []
        for index, color in enumerate(('red', 'green', 'blue'), 1):
            path = self.root / f'{index}.png'
            image = QImage(40, 40, QImage.Format_RGB32)
            image.fill(QColor(color))
            self.assertTrue(image.save(str(path)))
            images.append(str(path))
        dialog = self._dialog(prompt_path, images)
        try:
            before = [dialog.picture_list.item(i).data(Qt.UserRole) for i in range(dialog.picture_list.count())]
            dialog.model_combo.setCurrentText('video-v2')
            dialog.preview_tabs.setCurrentItem('converted')
            self.app.processEvents()
            after = [dialog.picture_list.item(i).data(Qt.UserRole) for i in range(dialog.picture_list.count())]
            self.assertEqual(after, before)
            self.assertEqual(after, images)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_preview_uses_submission_duration_adaptation(self):
        self.manager.config['workspace']['duration'] = 2
        prompt_path = self.root / 'v2.txt'
        prompt_path.write_text(
            '【分镜】\n人物站位：Maya在左侧。\n镜头1：动作一。\n镜头2：动作二。\n'
            '【禁止项】\n固定\n【强制声明】\n固定',
            encoding='utf-8',
        )
        dialog = self._dialog(prompt_path)
        try:
            dialog.model_combo.setCurrentText('MiniMax-H3')
            dialog.preview_tabs.setCurrentItem('converted')
            self.app.processEvents()
            self.assertIn('[Shot 2] At 00:02.000, 动作二。', dialog.prompt_preview.toPlainText())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_compact_dialog_keeps_image_and_preview_surfaces_separate(self):
        prompt_path = self.root / 'prompt.txt'
        prompt_path.write_text(H3_PROMPT, encoding='utf-8')
        dialog = self._dialog(prompt_path)
        try:
            dialog.resize(900, 650)
            self.app.processEvents()
            self.assertGreaterEqual(dialog.picture_list.height(), 80)
            self.assertGreaterEqual(dialog.prompt_preview.height(), 130)
            picture_bottom = dialog.picture_list.mapTo(dialog, dialog.picture_list.rect().bottomLeft()).y()
            preview_top = dialog.prompt_preview.mapTo(dialog, dialog.prompt_preview.rect().topLeft()).y()
            self.assertLess(picture_bottom, preview_top)
        finally:
            dialog.close()
            dialog.deleteLater()

    def _dialog(self, prompt_path, images=None):
        task = dict(prompt_path=str(prompt_path), prompt_name=prompt_path.stem, images=list(images or []))
        dialog = MatchDialog(config_manager=self.manager, matches=[task])
        dialog.show()
        QTest.qWait(20)
        return dialog


if __name__ == '__main__':
    unittest.main()
