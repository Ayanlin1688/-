import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication, QWidget
from ui.components.submission_dialog import SubmissionRecoveryDialog


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
