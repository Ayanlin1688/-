import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer
from qfluentwidgets import PushButton
from core.config_manager import ConfigManager
from ui.pages.workspace_page import WorkspacePage


class UnmatchedPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_each_preflight_choice_closes_dialog_with_expected_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = ConfigManager(Path(directory) / 'config.json'); cfg.load_config()
            page = WorkspacePage(cfg)
            page.closing.set()
            try:
                for text, expected in [('跳过未匹配任务', '跳过并警告'),
                                       ('全部提交为文生视频', '仍提交文生视频'), ('取消返回匹配', None)]:
                    clicked = []
                    def click_choice():
                        dialog = QApplication.activeModalWidget()
                        for button in dialog.findChildren(PushButton):
                            if button.text() == text:
                                button.click(); clicked.append(text); return
                        dialog.reject()
                    QTimer.singleShot(0, click_choice)
                    result = page._choose_unmatched_policy([dict(prompt_path='01.txt')])
                    self.assertEqual(clicked, [text])
                    self.assertEqual(result, expected)
            finally:
                page.close(); page.deleteLater()

    def test_preflight_cancel_and_text_choice_do_not_change_saved_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompts = root / 'prompts'; prompts.mkdir()
            (prompts / '01.txt').write_text('A product', encoding='utf-8')
            cfg = ConfigManager(root / 'config.json'); cfg.load_config()
            cfg.config['paths'].update(prompts=str(prompts), images='', output=str(root / 'out'))
            page = WorkspacePage(cfg)
            try:
                with patch.object(page, '_choose_unmatched_policy', return_value=None, create=True) as choice, patch.object(page.task_manager, 'start_tasks') as start:
                    page.start_generation(interactive=True)
                    choice.assert_called_once()
                    start.assert_not_called()
                with patch.object(page, '_choose_unmatched_policy', return_value='仍提交文生视频'), patch.object(page.task_manager, 'start_tasks') as start:
                    page.start_generation(interactive=True)
                    self.assertEqual(start.call_args.args[0]['task_strategy']['unmatched_prompt'], '仍提交文生视频')
                    self.assertEqual(cfg.config['task_strategy']['unmatched_prompt'], '跳过并警告')
            finally:
                page.closing.set(); page.close(); page.deleteLater()
