"""Recovery regressions using Qt window-coordinate dispatch and real modal loops."""

import copy
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5 import sip
from PyQt5.QtCore import QEvent, QObject, QThread, Qt, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QAbstractButton, QDialog, QScrollArea, QWidget

from core.config_manager import ConfigManager
from core.submission_ledger import account_scope
from ui.components.submission_dialog import SubmissionRecoveryBatchDialog, SubmissionRecoveryDialog
from ui.main_window import MainWindow


class DialogSurfaceObserver(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.overlaps = []

    def eventFilter(self, watched, event):
        if isinstance(watched, QDialog) and event.type() == QEvent.Show:
            visible = [widget for widget in QApplication.topLevelWidgets()
                       if isinstance(widget, QDialog) and widget.isVisible() and widget is not watched]
            if visible:
                self.overlaps.append([type(widget).__name__ for widget in visible] + [type(watched).__name__])
        return False


class RecoveryCoordinateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = ExitStack()
        self.worker_releases = []
        self.callback_errors = []
        self.exec_results = []
        self.quit_on_close = self.app.quitOnLastWindowClosed()
        self.app.setQuitOnLastWindowClosed(False)
        self.network = self.patches.enter_context(patch(
            'requests.sessions.Session.request',
            side_effect=AssertionError('coordinate recovery tests must never access the network')))
        self.patches.enter_context(patch('sys.excepthook', side_effect=self._qt_exception))
        for dialog_class in (SubmissionRecoveryDialog, SubmissionRecoveryBatchDialog):
            original_exec = dialog_class.exec

            def observed_exec(dialog, original=original_exec):
                result = original(dialog)
                self.exec_results.append((type(dialog), result))
                return result

            self.patches.enter_context(patch.object(dialog_class, 'exec', observed_exec))
        root = Path(self.temp.name)
        prompts = root / 'prompts'
        prompts.mkdir()
        output = root / 'output'
        output.mkdir()
        self.config = ConfigManager(root / 'config.json')
        self.config.config['paths'].update(prompts=str(prompts), output=str(output))
        self.config.config['api'].update(base_url='https://recovery.invalid/v1', api_key='fixture-only')
        self.config.config['updates']['check_on_start'] = False
        self.config.config['task_strategy'].update(watch_interval=0, open_folder_after_download=False)
        self.config.save_config()
        self.records = []
        for number in range(1, 5):
            prompt = prompts / f'fan-{number}.txt'
            prompt.write_text(f'isolated prompt {number}', encoding='utf-8')
            self.records.append(dict(
                local_id=f'local-{number}', ledger_id=f'ledger-{number}', ledger_state='unknown',
                product='车载四头风扇', prompt_name=prompt.stem, prompt_path=str(prompt),
                model='video-v3', status='submission_unknown', task_id='', images=[],
                created_at='2026-10-07T15:24:07', api_scope=account_scope(self.config.config),
                api_base_url=self.config.config['api']['base_url']))
        self.window = MainWindow(self.config, network_time=False, model_sync=False)
        self.page = self.window.workspace_page
        self.surface_observer = DialogSurfaceObserver(self.window)
        self.app.installEventFilter(self.surface_observer)
        self.ledger_class = self.patches.enter_context(patch('ui.pages.workspace_page.SubmissionLedger'))
        self.ledger = self.ledger_class.return_value
        self.ledger.resolve.side_effect = self._resolve_record
        self.start_tasks = self.patches.enter_context(patch.object(
            self.page.task_manager, 'start_tasks', return_value=True))
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.move(0, 0)
        QTest.qWait(100)
        self.assertTrue(self.page.isVisible())
        self._set_pending(self.records[:3])

    def tearDown(self):
        try:
            for release in self.worker_releases:
                release.set()
            self.window._close_timer.stop()
            self.page.shutdown()
            self._wait(lambda: not self.window._background_busy(), 'workers did not finish during cleanup')
            self.window.close()
            self.assertEqual(self.surface_observer.overlaps, [], 'dialogs were visible simultaneously')
            self.app.removeEventFilter(self.surface_observer)
            self.window.deleteLater()
            QTest.qWait(50)
            self.app.sendPostedEvents(None, QEvent.DeferredDelete)
            self._assert_no_dialogs_or_masks()
            self.network.assert_not_called()
            self.assertEqual(self.callback_errors, [], 'unexpected exception in a Qt callback')
        finally:
            self.patches.close()
            self.app.setQuitOnLastWindowClosed(self.quit_on_close)
            self.temp.cleanup()

    def _qt_exception(self, exception_type, exception, traceback):
        self.callback_errors.append(exception)
        dialog = getattr(self.page, '_active_recovery_dialog', None)
        if dialog is not None and not sip.isdeleted(dialog):
            dialog.reject()

    def _wait(self, predicate, message, timeout_ms=2500):
        deadline = time.monotonic() + timeout_ms / 1000
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), message)

    def _set_pending(self, records):
        self.page.task_manager.tasks = copy.deepcopy(records)
        self.page._publish_history(copy.deepcopy(records))
        self.page._tasks_updated(self.page.task_manager.tasks)
        if records:
            self.page._current_changed(0, self.page.task_manager.tasks[0])
        QTest.qWait(20)

    def _resolve_record(self, record_id, scope, *, task_id='', confirmed_not_created=False):
        self.assertEqual(scope, account_scope(self.config.config))
        self.assertTrue(bool(task_id) != bool(confirmed_not_created))
        record = copy.deepcopy(next(row for row in self.records if row['ledger_id'] == record_id))
        record.update(task_id=task_id, ledger_state='active' if task_id else 'released',
                      status='queued' if task_id else 'failed', confirmed_not_created=confirmed_not_created)
        return record

    @staticmethod
    def _visible_dialogs():
        return [widget for widget in QApplication.topLevelWidgets()
                if isinstance(widget, QDialog) and widget.isVisible()]

    def _assert_no_dialogs_or_masks(self):
        self.assertEqual(self._visible_dialogs(), [])
        masks = [widget for widget in QApplication.allWidgets() if widget.isVisible() and
                 ('windowmask' in widget.objectName().lower() or
                  type(widget).__name__ in {'MaskDialogBase', 'MessageBoxBase'})]
        self.assertEqual(masks, [], 'a visible mask survived closing the dialog')

    def _assert_hit(self, widget, global_point=None):
        ancestor = widget.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                ancestor.ensureWidgetVisible(widget, 8, 8)
            ancestor = ancestor.parentWidget()
        QTest.qWait(20)
        self.assertTrue(widget.isVisible(), f'{type(widget).__name__} is not visible')
        point = global_point if global_point is not None else widget.mapToGlobal(widget.rect().center())
        top = widget.window()
        screen = self.app.primaryScreen().availableGeometry()
        if not screen.contains(point):
            offset = screen.center() - point
            top.move(top.pos() + offset)
            QTest.qWait(20)
            point += offset
        local_point = top.mapFromGlobal(point)
        self.assertTrue(top.rect().contains(local_point), 'control lies outside its top-level window')
        hit = QApplication.widgetAt(point)
        if hit is not widget:
            ancestors = []
            ancestor = widget.parentWidget()
            while ancestor is not None:
                ancestors.append(ancestor)
                ancestor = ancestor.parentWidget()
            self.assertIn(hit, ancestors, f'global hit was {hit!r}, expected {widget!r}')
            self.assertIs(top.childAt(local_point), widget,
                          'offscreen parent hit must be corroborated by exact childAt')
        self.assertIsNotNone(top.windowHandle())
        return top, local_point

    def _mouse(self, widget, global_point=None):
        top, point = self._assert_hit(widget, global_point)
        QTest.mouseClick(top.windowHandle(), Qt.LeftButton, pos=point)

    def _choose(self, combo, index):
        self._mouse(combo)
        QTest.qWait(120)
        menu = combo.dropMenu
        self.assertIsNotNone(menu, 'coordinate click did not open the choice menu')
        model_index = menu.view.model().index(index, 0)
        menu.view.scrollTo(model_index)
        QTest.qWait(20)
        point = menu.view.viewport().mapToGlobal(menu.view.visualRect(model_index).center())
        self._mouse(menu.view.viewport(), point)
        QTest.qWait(120)
        self.assertEqual(combo.currentIndex(), index)
        self.assertIsNone(combo.dropMenu)

    def _type_id(self, field, text):
        self._mouse(field)
        QTest.keyClick(field, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClicks(field, text)
        self.assertEqual(field.text(), text)

    def _assert_single_surface(self, dialog):
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.move(30, 30)
        QTest.qWait(40)
        self.assertIs(dialog.parentWidget(), self.window)
        self.assertIs(dialog.window(), dialog)
        self.assertEqual(self._visible_dialogs(), [dialog])
        self.assertFalse(self.page.controls_dialog.isVisible())
        match = self.page.data_source.active_match_dialog
        self.assertTrue(match is None or not match.isVisible())

    def _drive_recovery(self, trigger, interaction):
        completed = []
        errors = []
        poll = QTimer(self.window)
        poll.setInterval(10)
        watchdog = QTimer(self.window)
        watchdog.setSingleShot(True)

        def drive():
            dialog = self.page._active_recovery_dialog
            if dialog is None or not dialog.isVisible():
                return
            poll.stop()
            try:
                self._assert_single_surface(dialog)
                interaction(dialog)
                completed.append(True)
            except BaseException as error:
                errors.append(error)
                if not sip.isdeleted(dialog):
                    dialog.reject()

        def timeout():
            errors.append(AssertionError('coordinate action did not finish the real modal loop'))
            self.page._close_child_dialogs()

        poll.timeout.connect(drive)
        watchdog.timeout.connect(timeout)
        poll.start()
        watchdog.start(4000)
        try:
            trigger()
            self._wait(lambda: bool(completed or errors), 'recovery never appeared', 4500)
            if errors:
                raise errors[0]
            self._wait(lambda: self.page._active_recovery_dialog is None,
                       'recovery exec() did not return')
            QTest.qWait(30)
            self._assert_no_dialogs_or_masks()
        finally:
            poll.stop()
            watchdog.stop()
            poll.deleteLater()
            watchdog.deleteLater()

    def _assert_rejected_without_resolution(self):
        self.assertEqual(self.exec_results, [(SubmissionRecoveryDialog, QDialog.Rejected)])
        self.ledger.resolve.assert_not_called()
        self.start_tasks.assert_not_called()
        self.assertTrue(all(row['status'] == 'submission_unknown' for row in self.page.task_manager.tasks))
        self.assertTrue(self.page.task_monitor.details_button.isEnabled())

    def test_cancel_coordinate_returns_rejected_and_preserves_unknown(self):
        def cancel(dialog):
            for button in (dialog.cancelButton, dialog.retry_not_created_button, dialog.yesButton, dialog.action):
                self._assert_hit(button)
            self.assertFalse(dialog.yesButton.isEnabled())
            self.assertTrue(dialog.reason_label.isVisible())
            self.assertTrue(dialog.reason_label.text())
            self._mouse(dialog.yesButton)
            self.assertTrue(dialog.isVisible())
            self._mouse(dialog.cancelButton)

        self._drive_recovery(lambda: self.page.resolve_submission(self.records[0]), cancel)
        self._assert_rejected_without_resolution()

    def test_escape_returns_rejected_and_preserves_unknown(self):
        self._drive_recovery(lambda: self.page.resolve_submission(self.records[0]),
                             lambda dialog: QTest.keyClick(dialog, Qt.Key_Escape))
        self._assert_rejected_without_resolution()

    def test_native_window_escape_returns_before_deferred_dialog_deletion(self):
        self._drive_recovery(lambda: self.page.resolve_submission(self.records[0]),
                             lambda dialog: QTest.keyClick(dialog.windowHandle(), Qt.Key_Escape))
        self._assert_rejected_without_resolution()

    def test_title_close_coordinate_returns_rejected_and_preserves_unknown(self):
        self._drive_recovery(lambda: self.page.resolve_submission(self.records[0]),
                             lambda dialog: self._mouse(dialog.header.close_button))
        self._assert_rejected_without_resolution()

    def test_retry_coordinate_accepts_and_restarts_only_chosen_prompt(self):
        choices = []

        def retry(dialog):
            self._mouse(dialog.retry_not_created_button)
            choices.append(dialog.action.currentIndex())

        self._drive_recovery(lambda: self.page.resolve_submission(self.records[1]), retry)
        self.assertEqual(choices, [2])
        self.assertEqual(self.exec_results, [(SubmissionRecoveryDialog, QDialog.Accepted)])
        self.ledger.resolve.assert_called_once_with(
            'ledger-2', account_scope(self.config.config), task_id='', confirmed_not_created=True)
        self.start_tasks.assert_called_once()
        self.assertEqual(self.start_tasks.call_args.args[0]['_only_prompt_paths'], [self.records[1]['prompt_path']])
        self.assertEqual([row['status'] for row in self.page.task_manager.tasks],
                         ['submission_unknown', 'failed', 'submission_unknown'])

    def test_existing_id_coordinate_requires_id_and_only_queries_downloads(self):
        output = Path(self.temp.name) / 'output' / 'existing.mp4'
        output.write_bytes(b'local download fixture')
        with patch('ui.pages.workspace_page.ApiClient') as client_class, \
                patch('ui.pages.workspace_page.VideoDownloader') as downloader_class:
            client = client_class.return_value
            client.query_task.return_value = dict(status='completed', result_url='https://fixture.invalid/video')
            downloader_class.return_value.download_video.return_value = str(output)
            choices = []

            def existing(dialog):
                self._mouse(dialog.lookup_existing_button)
                self.assertEqual(dialog.action.currentIndex(), 1)
                self.assertFalse(dialog.yesButton.isEnabled())
                self._type_id(dialog.task_id, '   ')
                self.assertFalse(dialog.yesButton.isEnabled())
                self._mouse(dialog.yesButton)
                self.assertTrue(dialog.isVisible())
                self._type_id(dialog.task_id, 'remote-existing-2')
                self.assertTrue(dialog.yesButton.isEnabled())
                choices.append(dialog.action.currentIndex())
                self._mouse(dialog.yesButton)

            self._drive_recovery(lambda: self.page.resolve_submission(self.records[1]), existing)
            self._wait(lambda: self.page.task_manager.tasks[1]['status'] == 'completed',
                       'existing task was not queried and downloaded')
            self.assertEqual(choices, [1])
            self.assertEqual(self.exec_results, [(SubmissionRecoveryDialog, QDialog.Accepted)])
            self.ledger.resolve.assert_called_once_with(
                'ledger-2', account_scope(self.config.config), task_id='remote-existing-2', confirmed_not_created=False)
            client.query_task.assert_called_once()
            self.assertEqual(client.query_task.call_args.args[:2], ('remote-existing-2', 'video-v3'))
            self.assertEqual([call[0] for call in client.method_calls], ['query_task', 'close'])
            downloader_class.return_value.download_video.assert_called_once()
            self.start_tasks.assert_not_called()
            self.assertEqual(self.page.task_manager.tasks[0]['status'], 'submission_unknown')
            self.assertEqual(self.page.task_manager.tasks[2]['status'], 'submission_unknown')

    def test_dropdown_coordinate_can_select_retry_and_save(self):
        def select(dialog):
            self._choose(dialog.action, 2)
            self.assertTrue(dialog.yesButton.isEnabled())
            self._mouse(dialog.yesButton)

        self._drive_recovery(lambda: self.page.resolve_submission(self.records[0]), select)
        self.assertEqual(self.exec_results[-1][1], QDialog.Accepted)
        self.assertEqual(self.start_tasks.call_args.args[0]['_only_prompt_paths'], [self.records[0]['prompt_path']])

    def test_batch_coordinates_validate_every_row_and_preserve_action_mapping(self):
        selections = []
        with patch.object(self.page, 'redownload', return_value=True) as download:
            def choose(dialog):
                self.assertIsInstance(dialog, SubmissionRecoveryBatchDialog)
                self.assertFalse(dialog.yesButton.isEnabled())
                for entry in dialog._rows:
                    self._assert_hit(entry['action'])
                self._mouse(dialog.yesButton)
                self.assertTrue(dialog.isVisible())
                self._choose(dialog._rows[0]['action'], 1)
                self._choose(dialog._rows[1]['action'], 2)
                self.assertFalse(dialog.yesButton.isEnabled())
                self._choose(dialog._rows[2]['action'], 3)
                self.assertFalse(dialog.yesButton.isEnabled())
                self._type_id(dialog._rows[2]['task_id'], '   ')
                self.assertFalse(dialog.yesButton.isEnabled())
                self._type_id(dialog._rows[2]['task_id'], 'remote-three')
                self.assertTrue(dialog.yesButton.isEnabled())
                selections.extend(dialog.selections())
                self._mouse(dialog.yesButton)

            self._drive_recovery(lambda: self.page.resolve_submissions(self.records[:3]), choose)
            self.assertEqual([(row['ledger_id'], action, task_id) for row, action, task_id in selections],
                             [('ledger-1', 0, ''), ('ledger-2', 2, ''), ('ledger-3', 1, 'remote-three')])
            self.assertEqual(self.exec_results, [(SubmissionRecoveryBatchDialog, QDialog.Accepted)])
            self.assertEqual([call.args[0] for call in self.ledger.resolve.call_args_list], ['ledger-2', 'ledger-3'])
            download.assert_called_once()
            self.assertEqual(download.call_args.args[0]['task_id'], 'remote-three')
            self.start_tasks.assert_not_called()
            download.call_args.args[1](True, download.call_args.args[0])
            self._wait(lambda: self.start_tasks.called, 'selected retry did not resume after serial download')
            self.assertEqual(self.start_tasks.call_args.args[0]['_only_prompt_paths'], [self.records[1]['prompt_path']])
            self.assertEqual(self.page.task_manager.tasks[0]['status'], 'submission_unknown')

    def test_batch_cancel_coordinates_never_resolve_or_restart(self):
        self._drive_recovery(lambda: self.page.resolve_submissions(self.records[:3]),
                             lambda dialog: self._mouse(dialog.cancelButton))
        self.assertEqual(self.exec_results, [(SubmissionRecoveryBatchDialog, QDialog.Rejected)])
        self.ledger.resolve.assert_not_called()
        self.start_tasks.assert_not_called()

    def test_batch_title_close_coordinates_never_resolve_or_restart(self):
        self._drive_recovery(lambda: self.page.resolve_submissions(self.records[:3]),
                             lambda dialog: self._mouse(dialog.header.close_button))
        self.assertEqual(self.exec_results, [(SubmissionRecoveryBatchDialog, QDialog.Rejected)])
        self.ledger.resolve.assert_not_called()
        self.start_tasks.assert_not_called()

    def test_batch_escape_never_resolves_or_restarts(self):
        self._drive_recovery(lambda: self.page.resolve_submissions(self.records[:3]),
                             lambda dialog: QTest.keyClick(dialog, Qt.Key_Escape))
        self.assertEqual(self.exec_results, [(SubmissionRecoveryBatchDialog, QDialog.Rejected)])
        self.ledger.resolve.assert_not_called()
        self.start_tasks.assert_not_called()

    def test_batch_all_task_id_fields_are_reachable_and_downloads_are_serial(self):
        release = threading.Event()
        self.worker_releases.append(release)
        query_ids = []
        query_threads = []
        output = Path(self.temp.name) / 'output' / 'batch.mp4'
        output.write_bytes(b'local batch fixture')

        def query(task_id, model, catalog):
            query_ids.append(task_id)
            query_threads.append(QThread.currentThread() is self.app.thread())
            if len(query_ids) == 1:
                if not release.wait(3):
                    raise AssertionError('serial download fixture was not released')
            return dict(status='completed', result_url='https://fixture.invalid/video')

        with patch('ui.pages.workspace_page.ApiClient') as client_class, \
                patch('ui.pages.workspace_page.VideoDownloader') as downloader_class:
            client = client_class.return_value
            client.query_task.side_effect = query
            downloader_class.return_value.download_video.return_value = str(output)

            def fill(dialog):
                for number, entry in enumerate(dialog._rows, 1):
                    self._choose(entry['action'], 3)
                    self.assertFalse(dialog.yesButton.isEnabled())
                    self._type_id(entry['task_id'], f'remote-{number}')
                self.assertTrue(dialog.yesButton.isEnabled())
                self._mouse(dialog.yesButton)

            self._drive_recovery(lambda: self.page.resolve_submissions(self.records[:3]), fill)
            self._wait(lambda: bool(query_ids), 'first existing task was not queried')
            QTest.qWait(60)
            self.assertEqual(query_ids, ['remote-1'], 'downloads overlapped instead of running serially')
            release.set()
            self._wait(lambda: all(row['status'] == 'completed' for row in self.page.task_manager.tasks),
                       'batch existing tasks were not all downloaded')
            self.assertEqual(query_ids, ['remote-1', 'remote-2', 'remote-3'])
            self.assertEqual(query_threads, [False, False, False])
            self.assertEqual([call[0] for call in client.method_calls], ['query_task', 'close'] * 3)
            self.assertEqual(downloader_class.return_value.download_video.call_count, 3)
            self.assertEqual(self.ledger.resolve.call_count, 3)
            self.start_tasks.assert_not_called()
            self.assertFalse(self.page.recovery_bar.isVisible())

    def test_controls_card_coordinate_closes_details_before_recovery(self):
        self.page.open_controls()
        self.page.controls_dialog.resize(1100, 900)
        QTest.qWait(40)
        self.assertTrue(self.page.controls_dialog.isVisible())
        self.assertTrue(self.page.current_task.resolve_button.isEnabled())
        self._drive_recovery(lambda: self._mouse(self.page.current_task.resolve_button),
                             lambda dialog: self._mouse(dialog.cancelButton))
        self._assert_rejected_without_resolution()
        self.assertFalse(self.page.controls_dialog.isVisible())
        self._mouse(self.page.task_monitor.details_button)
        QTest.qWait(40)
        self.assertTrue(self.page.controls_dialog.isVisible(), 'workspace details did not recover after cancellation')
        self._mouse(self.page.controls_dialog.header.close_button)
        self._assert_no_dialogs_or_masks()

    def test_match_modal_is_rejected_before_one_recovery_despite_reentry(self):
        match_results = []
        timer = QTimer(self.window)
        timer.setInterval(10)

        def from_match():
            match = self.page.data_source.active_match_dialog
            if match is None or not match.isVisible():
                return
            timer.stop()
            try:
                match.finished.connect(match_results.append)
                self.page.task_monitor.resolve_clicked.emit(self.records[0])
                self.assertFalse(match.isVisible())
                self.assertTrue(self.page._recovery_open_pending)
                self.page.task_monitor.resolve_clicked.emit(self.records[0])
            except BaseException as error:
                self.callback_errors.append(error)
                match.reject()

        def cancel(dialog):
            self.assertEqual(match_results, [QDialog.Rejected])
            self.page.task_monitor.resolve_clicked.emit(self.records[0])
            self.assertEqual(self._visible_dialogs(), [dialog])
            self.assertIs(self.page._active_recovery_dialog, dialog)
            self._mouse(dialog.cancelButton)

        timer.timeout.connect(from_match)
        timer.start()
        try:
            self._drive_recovery(self.page.data_source.open_match_dialog, cancel)
            self.assertEqual(len(self.exec_results), 1)
            self._assert_rejected_without_resolution()
        finally:
            timer.stop()
            timer.deleteLater()

    def test_history_coordinate_after_match_closed_reaches_cancel(self):
        timer = QTimer(self.window)
        timer.setInterval(10)

        def close_match():
            match = self.page.data_source.active_match_dialog
            if match is None or not match.isVisible():
                return
            timer.stop()
            try:
                self._mouse(match.header.close_button)
            except BaseException as error:
                self.callback_errors.append(error)
                match.reject()

        timer.timeout.connect(close_match)
        timer.start()
        try:
            self.page.data_source.open_match_dialog()
        finally:
            timer.stop()
            timer.deleteLater()
        self.window.switchTo(self.window.history_page)
        QTest.qWait(40)
        row = self.window.history_page.table.cellWidget(0, 8)
        buttons = [button for button in row.findChildren(QAbstractButton)
                   if button.toolTip() == '处理待确认提交']
        self.assertEqual(len(buttons), 1)
        self._drive_recovery(lambda: self._mouse(buttons[0]),
                             lambda dialog: self._mouse(dialog.cancelButton))
        self._assert_rejected_without_resolution()

    def test_finished_persistent_batch_entry_keeps_explicit_pending_records(self):
        self.page._finished(0, 0)
        self.assertTrue(self.page.recovery_bar.isVisible())
        self.assertIn('有 3 条提交待确认', self.page.recovery_label.text())
        self.assertTrue(self.page.recovery_button.isEnabled())
        self.assertEqual(self.page.recovery_button.text(), '逐条核对')

        def keep(dialog):
            self.assertIsInstance(dialog, SubmissionRecoveryBatchDialog)
            for entry in dialog._rows:
                self._choose(entry['action'], 1)
            self._mouse(dialog.yesButton)

        self._drive_recovery(lambda: self._mouse(self.page.recovery_button), keep)
        self.ledger.resolve.assert_not_called()
        self.start_tasks.assert_not_called()
        self.assertTrue(self.page.recovery_bar.isVisible(), 'keeping pending must preserve the safety entry')
        self.assertIn('有 3 条提交待确认', self.page.recovery_label.text())

    def test_persistent_entry_removes_resolved_rows_and_hides_when_empty(self):
        for index, record in enumerate(self.records[:3]):
            self.page._apply_resolved_submission(self._resolve_record(
                record['ledger_id'], account_scope(self.config.config), confirmed_not_created=True), record)
            remaining = 2 - index
            self.assertEqual(len(self.page._pending_recovery_records), remaining)
            self.assertEqual(self.page.recovery_bar.isVisible(), bool(remaining))
            if remaining:
                self.assertIn(f'有 {remaining} 条提交待确认', self.page.recovery_label.text())
        self.assertEqual(self.page._pending_records(), [])
        self._assert_no_dialogs_or_masks()

    def test_recovery_temporarily_disables_other_dialog_entries_and_restores_them(self):
        def check(dialog):
            for button in (self.page.task_monitor.details_button, self.page.queue_panel.match_button,
                           self.page.queue_panel.more_button, self.page.recovery_button,
                           self.window.history_page.batch_resolve_button):
                self.assertFalse(button.isEnabled())
                self.assertTrue(button.toolTip())
            self.page.open_controls()
            self.page.data_source.open_match_dialog()
            self.assertEqual(self._visible_dialogs(), [dialog])
            self._mouse(dialog.cancelButton)

        self._drive_recovery(lambda: self.page.resolve_submission(self.records[0]), check)
        for button in (self.page.task_monitor.details_button, self.page.queue_panel.match_button,
                       self.page.queue_panel.more_button, self.page.recovery_button,
                       self.window.history_page.batch_resolve_button):
            self.assertTrue(button.isEnabled())

    def test_coordinate_assertion_detects_a_mask_covering_the_button(self):
        def cover(dialog):
            overlay = QWidget(dialog)
            overlay.setObjectName('windowMask')
            overlay.setGeometry(dialog.rect())
            overlay.show()
            overlay.raise_()
            try:
                with self.assertRaises(AssertionError):
                    self._assert_hit(dialog.cancelButton)
            finally:
                overlay.hide()
                overlay.deleteLater()
            self._mouse(dialog.cancelButton)

        self._drive_recovery(lambda: self.page.resolve_submission(self.records[0]), cover)
        self._assert_rejected_without_resolution()

    def test_main_close_during_recovery_exits_modal_loop_and_requests_stops(self):
        with ExitStack() as spies:
            stop_spies = [spies.enter_context(patch.object(jobs, 'request_stop', wraps=jobs.request_stop))
                          for jobs in (self.page.jobs, self.page.history_jobs, self.page.log_drawer.jobs)]
            cancel = spies.enter_context(patch.object(
                self.page.task_manager, 'cancel_all', wraps=self.page.task_manager.cancel_all))
            self._drive_recovery(lambda: self.page.resolve_submission(self.records[0]),
                                 lambda dialog: self.window.close())
            for stop in stop_spies:
                stop.assert_called()
            cancel.assert_called()
            self.assertTrue(self.page.closing.is_set())
            self._wait(lambda: not self.window.isVisible(), 'main window remained open after close')
            self.assertEqual(self.exec_results, [(SubmissionRecoveryDialog, QDialog.Rejected)])
            self.ledger.resolve.assert_not_called()
            self.start_tasks.assert_not_called()

    def test_main_close_with_worker_requests_interruption_without_blocking(self):
        entered = threading.Event()
        release = threading.Event()
        self.worker_releases.append(release)

        def work():
            entered.set()
            release.wait(5)
            return 'finished'

        worker = self.page.jobs.start(work, lambda result: None, lambda error: None)
        self._wait(entered.is_set, 'background worker did not start')
        self.page.open_controls()
        with patch.object(self.page.jobs, 'request_stop', wraps=self.page.jobs.request_stop) as stop, \
                patch.object(self.page.task_manager, 'cancel_all', wraps=self.page.task_manager.cancel_all) as cancel:
            started = time.monotonic()
            self.window.close()
            self.assertLess(time.monotonic() - started, 1.0, 'close blocked the Qt thread')
            stop.assert_called_once()
            cancel.assert_called_once()
            self.assertTrue(worker.isInterruptionRequested())
            self.assertTrue(self.window._closing)
            self._assert_no_dialogs_or_masks()
            release.set()
            self._wait(lambda: not self.window._background_busy(), 'background worker did not complete')
            self.window._finish_close()
            self.assertFalse(self.window.isVisible())

    def test_close_deadline_forces_all_jobs_with_valid_safe_timer(self):
        real_timer = threading.Timer
        timers = []

        def safe_timer(*args, **kwargs):
            timer = real_timer(*args, **kwargs)
            timer.start = Mock()
            timers.append(timer)
            return timer

        with ExitStack() as mocks:
            mocks.enter_context(patch.object(self.window, '_background_busy', return_value=True))
            stop = mocks.enter_context(patch.object(self.page.task_manager, 'force_stop'))
            force_spies = [mocks.enter_context(patch.object(jobs, 'force_stop')) for jobs in (
                self.page.jobs, self.page.history_jobs, self.page.log_drawer.jobs,
                self.window.settings_page.jobs, self.window.model_catalog.jobs)]
            quit_app = mocks.enter_context(patch('ui.main_window.QApplication.quit'))
            exit_process = mocks.enter_context(patch('ui.main_window.os._exit'))
            mocks.enter_context(patch('ui.main_window.threading.Timer', side_effect=safe_timer))
            self.window._close_deadline = time.monotonic() + 60
            self.window._finish_close()
            stop.assert_not_called()
            quit_app.assert_not_called()
            self.window._close_deadline = time.monotonic() - 1
            started = time.monotonic()
            self.window._finish_close()
            self.assertLess(time.monotonic() - started, 1.0)
            stop.assert_called_once()
            for force_stop in force_spies:
                force_stop.assert_called_once()
            quit_app.assert_called_once()
            self.assertEqual(len(timers), 1)
            self.assertTrue(timers[0].daemon, 'last-resort timer must not hold the process open')
            timers[0].start.assert_called_once()
            exit_process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
