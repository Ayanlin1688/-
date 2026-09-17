"""Regression tests for reference editing and submission preflight (no network)."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import CaptionLabel
from core.config_manager import ConfigManager
from ui.components.match_dialog import MatchDialog
from ui.widgets.workspace_task_table import ExpandedTaskRow


class ReferenceClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ConfigManager(self.root / 'config.json')
        self.config.load_config()
        self.config.config['workspace']['model'] = 'MiniMax-H3'
        self.config.config['prompt_detection']['enabled'] = False
        prompt = self.root / '01.txt'
        prompt.write_text('Test rose blanket', encoding='utf-8')
        self.task = dict(prompt_path=str(prompt), prompt_name='01', product='rose',
                         images=[str(self.root / f'{i}.jpg') for i in range(1, 11)],
                         matched=True, match_method='序号匹配', requested_model='MiniMax-H3')
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close(); widget.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def dialog(self, **kwargs):
        dialog = MatchDialog(config_manager=self.config, matches=[self.task], **kwargs)
        self.widgets.append(dialog)
        return dialog

    def test_empty_automatic_result_clears_saved_manual_binding(self):
        dialog = self.dialog(automatic_matches=[])
        with patch.object(dialog, '_notice'):
            dialog._rematch(); dialog._save()
        self.assertEqual(dialog.bindings.get(self.task['prompt_path'], []), [])
        self.assertEqual(self.config.config['match_overrides'][self.task['prompt_path']], [])

    def test_full_binding_retained_but_cap_and_method_are_visible(self):
        dialog = self.dialog()
        self.assertEqual(dialog.picture_list.count(), 10)
        self.assertIn('9/10', dialog.binding_summary.text())
        self.assertIn('序号匹配', dialog.match_method_label.text())
        last = dialog.picture_list.itemWidget(dialog.picture_list.item(9))
        self.assertIn('不提交', last.findChild(CaptionLabel, 'pictureCaption').text())

    def test_reference_count_and_click_open_match_editor_even_when_unmatched(self):
        row = ExpandedTaskRow(0, self.task, {'model': 'MiniMax-H3'})
        self.widgets.append(row)
        actions = []; row.action_requested.connect(lambda i, action: actions.append((i, action)))
        self.assertIn('9/10', row.images_button.text())
        row.images_button.click()
        self.assertEqual(actions, [(0, 'match')])
        row.update_task(dict(self.task, images=[], matched=False, status='waiting'))
        row.set_editable(True)
        self.assertTrue(row.images_button.isEnabled())
        self.assertEqual(row.status_label.text(), '未匹配')


if __name__ == '__main__':
    unittest.main()
