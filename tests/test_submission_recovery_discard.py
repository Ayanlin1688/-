import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtWidgets import QApplication, QDialog, QWidget

from ui.components.studio_dialog import StudioDialog, safe_delete_dialog
from ui.components.submission_dialog import SubmissionRecoveryBatchDialog, SubmissionRecoveryDialog


class RecoveryDiscardDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QWidget()
        self.dialogs = []
        self.record = dict(local_id='pending-one', status='submission_unknown', task_id='')

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject()
            safe_delete_dialog(dialog)
        self.parent.close()
        self.parent.deleteLater()
        self.app.processEvents()

    def single(self, record=None):
        dialog = SubmissionRecoveryDialog(self.parent, self.record if record is None else record)
        self.dialogs.append(dialog)
        return dialog

    def batch(self, records=None):
        dialog = SubmissionRecoveryBatchDialog(records or [self.record], self.parent)
        self.dialogs.append(dialog)
        return dialog

    def test_single_discard_requires_checkbox_and_hides_task_id(self):
        dialog = self.single()
        self.assertIsInstance(dialog, StudioDialog)
        self.assertEqual(dialog.action.count(), 4)
        self.assertEqual(dialog.action.currentIndex(), 0)
        self.assertFalse(dialog.discard_confirm.isChecked())
        dialog.action.setCurrentIndex(3)
        self.assertTrue(dialog.task_id.isHidden())
        self.assertFalse(dialog.discard_confirm.isHidden())
        self.assertFalse(dialog.yesButton.isEnabled())
        self.assertEqual(dialog.reason_label.text(), '作废需确认服务端未创建，以防漏单/重复扣费')
        self.assertEqual(dialog.yesButton.toolTip(), dialog.reason_label.text())
        with patch.object(dialog, 'accept') as accept:
            dialog._accept_if_valid()
            accept.assert_not_called()
            dialog.discard_confirm.setChecked(True)
            self.assertTrue(dialog.yesButton.isEnabled())
            self.assertEqual(dialog.reason_label.text(), '将作废该条、移出待处理，不重试、不提交其他任务')
            dialog._accept_if_valid()
            accept.assert_called_once()
        dialog.discard_confirm.setChecked(False)
        self.assertFalse(dialog.yesButton.isEnabled())

    def test_batch_discard_maps_to_action_three_only_after_explicit_confirmation(self):
        dialog = self.batch()
        self.assertIsInstance(dialog, StudioDialog)
        entry = dialog._rows[0]
        self.assertEqual(entry['action'].count(), 5)
        entry['task_id'].setText('stale-hidden-input')
        entry['action'].setCurrentIndex(4)
        self.assertTrue(entry['task_id'].isHidden())
        self.assertFalse(entry['discard_confirm'].isHidden())
        self.assertFalse(dialog.yesButton.isEnabled())
        self.assertEqual(entry['reason'].text(), '作废需确认服务端未创建，以防漏单/重复扣费')
        with patch.object(dialog, 'accept') as accept:
            dialog._accept_if_valid()
            accept.assert_not_called()
            entry['discard_confirm'].setChecked(True)
            self.assertTrue(dialog.yesButton.isEnabled())
            self.assertEqual(entry['reason'].text(), '将作废该条、移出待处理，不重试、不提交其他任务')
            self.assertEqual(dialog.selections(), [(self.record, 3, '')])
            dialog._accept_if_valid()
            accept.assert_called_once()
        entry['discard_confirm'].setChecked(False)
        self.assertFalse(dialog.yesButton.isEnabled())

    def test_batch_still_requires_each_row_to_have_a_valid_explicit_choice(self):
        records = [dict(self.record, local_id=str(index)) for index in range(3)]
        dialog = self.batch(records)
        first, second, third = dialog._rows
        first['action'].setCurrentIndex(4)
        first['discard_confirm'].setChecked(True)
        self.assertFalse(dialog.yesButton.isEnabled())
        second['action'].setCurrentIndex(1)
        third['action'].setCurrentIndex(3)
        self.assertFalse(dialog.yesButton.isEnabled())
        third['task_id'].setText('remote-existing')
        self.assertTrue(dialog.yesButton.isEnabled())
        self.assertEqual([selection[1:] for selection in dialog.selections()],
                         [(3, ''), (0, ''), (1, 'remote-existing')])
        first['discard_confirm'].setChecked(False)
        self.assertFalse(dialog.yesButton.isEnabled())

    def test_known_task_id_disables_discard_and_revalidation_rejects_forced_choice(self):
        record = dict(self.record, task_id='provider-created')
        single = self.single(record)
        batch = self.batch([record])
        entry = batch._rows[0]
        for dialog, combo, checkbox, reason, index in (
                (single, single.action, single.discard_confirm, single.reason_label, 3),
                (batch, entry['action'], entry['discard_confirm'], entry['reason'], 4)):
            with self.subTest(dialog=type(dialog).__name__):
                self.assertFalse(combo.items[index].isEnabled)
                self.assertIn('已创建：仅查询下载', reason.text())
                combo.setCurrentIndex(index)
                checkbox.setChecked(True)
                self.assertFalse(checkbox.isEnabled())
                self.assertFalse(dialog.yesButton.isEnabled())
                self.assertIn('已有 task_id', reason.text())
                with patch.object(dialog, 'accept') as accept:
                    dialog._accept_if_valid()
                    accept.assert_not_called()

    def test_discard_only_available_for_uncertain_or_precreation_failure(self):
        for status in ('failed', 'submission_unknown', 'queued', 'completed', 'active', None):
            with self.subTest(status=status):
                record = dict(self.record, status=status)
                single = self.single(record)
                batch = self.batch([record])
                expected = status in {'failed', 'submission_unknown'}
                self.assertIs(single.action.items[3].isEnabled, expected)
                self.assertIs(batch._rows[0]['action'].items[4].isEnabled, expected)
                single.action.setCurrentIndex(3)
                single.discard_confirm.setChecked(True)
                batch._rows[0]['action'].setCurrentIndex(4)
                batch._rows[0]['discard_confirm'].setChecked(True)
                self.assertIs(single.yesButton.isEnabled(), expected)
                self.assertIs(batch.yesButton.isEnabled(), expected)

    def test_switching_away_from_discard_preserves_old_task_id_requirement(self):
        single = self.single()
        batch = self.batch()
        entry = batch._rows[0]
        for dialog, combo, checkbox, task_id, discard_index, lookup_index in (
                (single, single.action, single.discard_confirm, single.task_id, 3, 1),
                (batch, entry['action'], entry['discard_confirm'], entry['task_id'], 4, 3)):
            with self.subTest(dialog=type(dialog).__name__):
                combo.setCurrentIndex(discard_index)
                checkbox.setChecked(True)
                self.assertTrue(dialog.yesButton.isEnabled())
                combo.setCurrentIndex(lookup_index)
                self.assertTrue(checkbox.isHidden())
                self.assertFalse(task_id.isHidden())
                self.assertFalse(dialog.yesButton.isEnabled())
                task_id.setText('   ')
                self.assertFalse(dialog.yesButton.isEnabled())
                task_id.setText('remote-existing')
                self.assertTrue(dialog.yesButton.isEnabled())

    def test_cancelling_checked_discard_does_not_accept_or_save(self):
        single = self.single()
        batch = self.batch()
        single.action.setCurrentIndex(3)
        single.discard_confirm.setChecked(True)
        batch._rows[0]['action'].setCurrentIndex(4)
        batch._rows[0]['discard_confirm'].setChecked(True)
        for dialog in (single, batch):
            with self.subTest(dialog=type(dialog).__name__):
                finished = []
                dialog.finished.connect(finished.append)
                dialog.reject()
                self.assertEqual(finished, [QDialog.Rejected])
                self.assertEqual(dialog.result(), QDialog.Rejected)


if __name__ == '__main__':
    unittest.main()
