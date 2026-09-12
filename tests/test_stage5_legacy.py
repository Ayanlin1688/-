"""Legacy task IDs require account confirmation before any network recovery."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer
from core.config_manager import ConfigManager, DEFAULT_CONFIG
from core.submission_ledger import SubmissionLedger, account_scope
from core.task_manager import TaskManager, TaskWorker
from core.task_state import task_signature
from test_http_clients import LocalServer
from test_task_manager import wait_until
from ui.pages.workspace_page import WorkspacePage
from ui.components.submission_dialog import SubmissionRecoveryDialog


class LegacyScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        prompts = self.root / 'prompts'
        prompts.mkdir()
        (prompts / '1.txt').write_text('A car beside a quiet road', encoding='utf-8')
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config['paths'].update(prompts=str(prompts), output=str(self.root / 'out'))
        self.config['prompt_detection']['enabled'] = False
        self.config['workspace']['poll_interval'] = .01
        self.config['task_strategy'].update(unmatched_prompt='仍提交文生视频', retry_interval=.01)
        self.config['_submission_ledger_path'] = str(self.root / 'submissions.sqlite3')
        self.manager = TaskManager()

    def tearDown(self):
        self.manager.cancel_all()
        wait_until(lambda: not self.manager.is_running)
        self.temp.cleanup()

    def history(self, server, *, legacy=True):
        self.config['api'].update(base_url=server.base, api_key='old-account')
        worker = TaskWorker(self.config, [])
        worker._scan()
        record = copy.deepcopy(worker.tasks[0])
        record.update(task_id='legacy-task', status='failed')
        if legacy:
            record.pop('api_scope')
            record['signature'] = task_signature(record, record['model'], self.config['workspace'], server.base, legacy=True)
        self.config['history'] = [record]
        self.config['api']['api_key'] = 'current-account'
        return record

    def run_batch(self):
        self.assertTrue(self.manager.start_tasks(self.config))
        wait_until(lambda: not self.manager.is_running, timeout=10000)
        return self.manager.tasks[0]

    def test_unscoped_legacy_blocks_post_and_get_even_without_history_after_restart(self):
        with LocalServer() as server:
            self.history(server)
            result = self.run_batch()
            self.assertEqual(server.calls, [])
            self.assertEqual(server.gets, [])
            self.assertEqual(result['status'], 'submission_unknown')
            self.assertEqual(result['task_id'], '')
            self.assertEqual(result['legacy_task_id'], 'legacy-task')
            self.config['history'] = []
            self.manager = TaskManager()
            again = self.run_batch()
            self.assertEqual(again['status'], 'submission_unknown')
            self.assertEqual(server.calls, [])
            self.assertEqual(server.gets, [])

    def test_explicit_current_account_id_confirmation_resumes_only_get(self):
        with LocalServer() as server:
            self.history(server)
            pending = self.run_batch()
            self.assertEqual(pending['status'], 'submission_unknown')
            ledger = SubmissionLedger(self.config['_submission_ledger_path'])
            ledger.resolve(pending['ledger_id'], account_scope(self.config), task_id='confirmed-current-task')
            self.manager = TaskManager()
            result = self.run_batch()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['task_id'], 'confirmed-current-task')
            self.assertEqual(server.calls, [])
            self.assertIn('/videos/confirmed-current-task', server.gets)
            self.assertNotIn('/videos/legacy-task', server.gets)

    def test_noncreation_confirmation_is_not_undone_by_old_history(self):
        with LocalServer() as server:
            self.history(server)
            pending = self.run_batch()
            self.assertEqual(pending['status'], 'submission_unknown')
            ledger = SubmissionLedger(self.config['_submission_ledger_path'])
            ledger.resolve(pending['ledger_id'], account_scope(self.config), confirmed_not_created=True)
            self.manager = TaskManager()
            result = self.run_batch()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(len(server.calls), 1)
            self.assertNotIn('/videos/legacy-task', server.gets)

    def test_explicit_other_scope_does_not_block_current_account_generation(self):
        with LocalServer() as server:
            self.history(server, legacy=False)
            result = self.run_batch()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(len(server.calls), 1)
            self.assertNotIn('/videos/legacy-task', server.gets)

    def test_confirmed_current_scope_record_still_resumes_without_post(self):
        with LocalServer() as server:
            record = self.history(server, legacy=False)
            record['api_scope'] = account_scope(self.config)
            result = self.run_batch()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(server.calls, [])
            self.assertIn('/videos/legacy-task', server.gets)

    def test_recovery_dialog_binds_only_the_explicitly_entered_current_account_id(self):
        with LocalServer() as server:
            self.history(server)
            pending = self.run_batch()
            self.assertEqual(pending['status'], 'submission_unknown')
            manager = ConfigManager(self.root / 'config.json')
            manager.config = copy.deepcopy(self.config)
            manager.config['workspace']['poll_interval'] = 3
            page = WorkspacePage(manager)
            defaults = []

            real_exec = SubmissionRecoveryDialog.exec

            def execute_dialog(widget):
                # Schedule after construction: Fluent style setup may process
                # events, so a timer queued before construction can fire early.
                def confirm():
                    defaults.append((widget.action.currentIndex(), widget.task_id.text()))
                    widget.action.setCurrentIndex(1)
                    widget.task_id.setText('current-account-confirmed-id')
                    widget.yesButton.click()
                timeout = QTimer(widget)
                timeout.setSingleShot(True)
                timeout.timeout.connect(widget.reject)
                timeout.start(3000)
                QTimer.singleShot(30, confirm)
                return real_exec(widget)

            try:
                with patch.object(SubmissionRecoveryDialog, 'exec', new=execute_dialog):
                    page.resolve_submission(pending)
                self.assertEqual(defaults, [(0, '')])
                self.assertEqual(server.calls, [])
                self.assertEqual(server.gets, [])
                recovered = next(record for record in manager.config['history'] if record.get('ledger_id') == pending['ledger_id'])
                server.polls['current-account-confirmed-id'] = 1
                page.redownload(recovered)
                wait_until(lambda: not page.jobs.busy)
                self.assertEqual(server.calls, [])
                self.assertIn('/videos/current-account-confirmed-id', server.gets)
                self.assertNotIn('/videos/legacy-task', server.gets)
            finally:
                page.shutdown()
                wait_until(lambda: not page.jobs.busy)
                page.close()
                page.deleteLater()

    def test_history_redownload_requires_confirmed_matching_scope(self):
        with LocalServer() as server:
            record = self.history(server)
            manager = ConfigManager(self.root / 'config.json')
            manager.config = copy.deepcopy(self.config)
            manager.config['workspace']['poll_interval'] = 3
            server.polls['legacy-task'] = 1
            page = WorkspacePage(manager)
            try:
                for scope in (None, 'other-account-scope'):
                    with self.subTest(scope=scope):
                        candidate = copy.deepcopy(record)
                        if scope is not None:
                            candidate['api_scope'] = scope
                        page.redownload(candidate)
                        wait_until(lambda: not page.jobs.busy)
                        self.assertEqual(server.calls, [])
                        self.assertEqual(server.gets, [])
                record['api_scope'] = account_scope(manager.config)
                page.redownload(record)
                wait_until(lambda: not page.jobs.busy)
                self.assertEqual(server.calls, [])
                self.assertIn('/videos/legacy-task', server.gets)
            finally:
                page.shutdown()
                wait_until(lambda: not page.jobs.busy)
                page.close()
                page.deleteLater()


if __name__ == '__main__':
    unittest.main()
