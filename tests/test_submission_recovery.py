import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget
from core.config_manager import ConfigManager
from ui.components.submission_dialog import SubmissionRecoveryDialog
from ui.pages.workspace_page import WorkspacePage


class RecoveryDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_default_cannot_authorize_resubmission_and_id_must_be_nonempty(self):
        parent = QWidget()
        parent.resize(800, 600)
        dialog = SubmissionRecoveryDialog(parent)
        try:
            self.assertEqual(dialog.action.currentIndex(), 0)
            self.assertFalse(dialog.yesButton.isEnabled())
            dialog.action.setCurrentIndex(1)
            dialog.task_id.setText('  ')
            self.assertFalse(dialog.yesButton.isEnabled())
            dialog.task_id.setText('existing-task-id')
            self.assertTrue(dialog.yesButton.isEnabled())
            dialog.action.setCurrentIndex(0)
            self.assertFalse(dialog.yesButton.isEnabled())
            dialog.action.setCurrentIndex(2)
            self.assertTrue(dialog.yesButton.isEnabled())
        finally:
            dialog.close()
            parent.close()
            dialog.deleteLater()
            parent.deleteLater()

    def test_mouse_selection_in_recovery_menu_enables_confirmation(self):
        parent = QWidget()
        parent.resize(800, 600)
        dialog = SubmissionRecoveryDialog(parent)
        dialog.show()
        try:
            QTest.qWait(40)
            QTest.mouseClick(dialog.action, Qt.LeftButton)
            QTest.qWait(80)
            menu = dialog.action.dropMenu
            item = menu.view.model().index(2, 0)
            QTest.mouseClick(menu.view.viewport(), Qt.LeftButton,
                             pos=menu.view.visualRect(item).center())
            QTest.qWait(40)
            self.assertEqual(dialog.action.currentIndex(), 2)
            self.assertTrue(dialog.yesButton.isEnabled())
            self.assertIsNone(dialog.action.dropMenu)
        finally:
            dialog.close()
            parent.close()
            dialog.deleteLater()
            parent.deleteLater()


class WorkspaceRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_confirmed_noncreation_scopes_restart_to_current_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prompts = root / 'prompts'
            prompts.mkdir()
            current = prompts / 'current.txt'
            other = prompts / 'other.txt'
            current.write_text('current prompt', encoding='utf-8')
            other.write_text('other prompt', encoding='utf-8')
            manager = ConfigManager(root / 'config.json')
            manager.config['paths'].update(prompts=str(prompts), output=str(root / 'output'))
            manager.config['api']['api_key'] = 'fixture-key'
            page = WorkspacePage(manager)
            page.task_manager.start_tasks = Mock(return_value=True)
            try:
                page._resume_confirmed_submission({'prompt_path': str(current)})
                page.task_manager.start_tasks.assert_called_once()
                runtime = page.task_manager.start_tasks.call_args.args[0]
                self.assertEqual(runtime['_only_prompt_paths'], [str(current)])
                self.assertNotIn('_only_prompt_paths', manager.config)
                self.assertNotIn('_only_prompt_paths', manager.config['paths'])
                self.assertNotIn(str(other), runtime['_only_prompt_paths'])
            finally:
                page.shutdown()
                page.close()
                page.deleteLater()
