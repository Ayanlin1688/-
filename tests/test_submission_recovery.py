import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget, QAbstractButton
from core.config_manager import ConfigManager
from ui.components.submission_dialog import SubmissionRecoveryDialog, SubmissionRecoveryBatchDialog
from ui.pages.history_page import HistoryPage
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

    def test_cancel_keeps_unknown_without_submit_path(self):
        parent = QWidget()
        dialog = SubmissionRecoveryDialog(parent, dict(status='submission_unknown'))
        try:
            with patch.object(dialog, 'accept') as accept:
                dialog.cancelButton.click()
                accept.assert_not_called()
            self.assertEqual(dialog.action.currentIndex(), 0)
            self.assertFalse(dialog.yesButton.isEnabled())
            self.assertIn('关闭', dialog.reason_label.text())
        finally:
            dialog.close()
            parent.close()
            dialog.deleteLater()
            parent.deleteLater()

    def test_batch_requires_each_explicit_choice_and_preserves_safe_action_mapping(self):
        parent = QWidget()
        records = [
            dict(local_id='one', product='A', prompt_name='one.txt', model='video-v3', status='submission_unknown'),
            dict(local_id='two', product='B', prompt_name='two.txt', model='video-v3', status='submission_unknown'),
        ]
        dialog = SubmissionRecoveryBatchDialog(records, parent)
        try:
            self.assertFalse(dialog.yesButton.isEnabled())
            dialog._rows[0]['action'].setCurrentIndex(1)  # explicit keep pending
            self.assertFalse(dialog.yesButton.isEnabled())
            dialog._rows[1]['action'].setCurrentIndex(2)  # retry only this row
            self.assertTrue(dialog.yesButton.isEnabled())
            self.assertEqual(
                [(record.get('local_id'), action, task_id)
                 for record, action, task_id in dialog.selections()],
                [('one', 0, ''), ('two', 2, '')])
            dialog._rows[1]['action'].setCurrentIndex(3)
            self.assertFalse(dialog.yesButton.isEnabled())
            dialog._rows[1]['task_id'].setText('remote-two')
            self.assertTrue(dialog.yesButton.isEnabled())
            self.assertEqual(dialog.selections()[1][1:], (1, 'remote-two'))
        finally:
            dialog.close()
            parent.close()
            dialog.deleteLater()
            parent.deleteLater()

    def test_batch_keep_pending_is_explicit_and_maps_to_no_resolution(self):
        parent = QWidget()
        records = [dict(local_id='one', status='submission_unknown')]
        dialog = SubmissionRecoveryBatchDialog(records, parent)
        try:
            dialog._rows[0]['action'].setCurrentIndex(1)
            self.assertTrue(dialog.yesButton.isEnabled())
            self.assertEqual(dialog.selections(), [(records[0], 0, '')])
            self.assertIn('不调用提交接口', dialog._rows[0]['reason'].text())
        finally:
            dialog.close()
            parent.close()
            dialog.deleteLater()
            parent.deleteLater()


class HistoryIncrementalUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_incremental_refresh_enables_download_when_task_id_arrives(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ConfigManager(Path(folder) / 'config.json')
            page = HistoryPage(lambda *_: None, manager)
            task = dict(local_id='history-one', prompt_name='one', model='video-v3',
                        status='failed', task_id='', result_path='')
            try:
                page.update_history([task])
                row = page.table.cellWidget(0, 8)
                retry = next(button for button in row.findChildren(QAbstractButton)
                             if '没有可查询的远端 task_id' in button.toolTip())
                self.assertFalse(retry.isEnabled())
                updated = dict(task, task_id='remote-one')
                page.update_history([updated])
                row = page.table.cellWidget(0, 8)
                retry = next(button for button in row.findChildren(QAbstractButton)
                             if '查询并下载' in button.toolTip())
                self.assertTrue(retry.isEnabled())
            finally:
                page.close()
                page.deleteLater()

    def test_refresh_rebuilds_when_row_identity_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ConfigManager(Path(folder) / 'config.json')
            page = HistoryPage(lambda *_: None, manager)
            first = dict(local_id='history-one', prompt_name='one', model='video-v3',
                         status='failed', task_id='', result_path='')
            second = dict(local_id='history-two', prompt_name='two', model='video-v3',
                          status='completed', task_id='remote-two', result_path='video.mp4')
            try:
                page.update_history([first])
                first_row = page.table.cellWidget(0, 8)
                page.update_history([second])
                self.assertEqual(page.records[0]['local_id'], 'history-two')
                self.assertEqual(page.table.item(0, 2).text(), 'two')
                self.assertIsNot(page.table.cellWidget(0, 8), first_row)
            finally:
                page.close()
                page.deleteLater()


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

    def test_batch_resolution_skips_keep_pending_without_resolving(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ConfigManager(Path(folder) / 'config.json')
            page = WorkspacePage(manager)
            records = [dict(local_id='one', status='submission_unknown', ledger_id='ledger-one')]
            try:
                with patch('ui.pages.workspace_page.SubmissionRecoveryBatchDialog') as dialog_cls, \
                     patch('ui.pages.workspace_page.SubmissionLedger') as ledger_cls:
                    dialog_cls.return_value.exec.return_value = True
                    dialog_cls.return_value.selections.return_value = [(records[0], 0, '')]
                    page.resolve_submissions(records)
                    ledger_cls.return_value.resolve.assert_not_called()
            finally:
                page.shutdown()
                page.close()
                page.deleteLater()
