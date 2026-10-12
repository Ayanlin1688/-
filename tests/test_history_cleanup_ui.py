import copy
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QEvent, QRect, Qt, QTimer
from PyQt5.QtGui import QColor, QImage, QPainter
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog, QStyle, QStyleOptionButton, QStyleOptionViewItem
from qfluentwidgets import CheckBox

from core.config_manager import ConfigManager
from core.disk_guard import TEMP_SUFFIXES
from core.submission_ledger import SubmissionLedger, account_scope, ledger_path
from ui.components.history_dialog import DeleteHistoryDialog, PermanentDeleteDialog
from ui.components.studio_dialog import safe_delete_dialog
from ui.components.submission_dialog import SubmissionRecoveryBatchDialog
from ui.history_cleanup import cleanup_local_cache, output_file, output_summary, remove_empty_product_folders
from ui.main_window import MainWindow
from ui.model_catalog_controller import runtime_config


class LocalCacheCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'output'
        self.images = self.root / 'images'
        self.scratch = self.root / 'scratch'
        for directory in (self.output, self.images, self.scratch):
            directory.mkdir()

    def make_file(self, path, content=b'fixture', old=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if old:
            timestamp = time.time() - 172800
            os.utime(path, (timestamp, timestamp))
        return path

    def test_manual_cleanup_purges_all_known_suffixes_in_both_roots(self):
        files = [self.make_file(directory / ('cache' + suffix), old=True)
                 for directory in (self.output, self.images) for suffix in TEMP_SUFFIXES]
        report = cleanup_local_cache(dict(output=str(self.output), images=str(self.images)), lambda *_: None,
                                     self.scratch)
        self.assertEqual(report, dict(removed=10, freed_bytes=70))
        self.assertTrue(all(not path.exists() for path in files))

    def test_recent_cache_and_media_survive_while_stale_uploader_jpg_is_removed(self):
        preserved = [self.make_file(self.output / 'video.mp4', old=True),
                     self.make_file(self.images / 'reference.png', old=True),
                     self.make_file(self.output / 'current.part'),
                     self.make_file(self.scratch / 'storyboard-reference-current.jpg'),
                     self.make_file(self.scratch / 'storyboard-reference-other.mp4', old=True),
                     self.make_file(self.scratch / 'unrelated.jpg', old=True)]
        stale = self.make_file(self.scratch / 'storyboard-reference-stale.jpg', old=True)
        report = cleanup_local_cache(dict(output=str(self.output), images=str(self.images)), lambda *_: None,
                                     self.scratch)
        self.assertEqual(report, dict(removed=1, freed_bytes=7))
        self.assertFalse(stale.exists())
        self.assertTrue(all(path.exists() for path in preserved))

    def test_repeated_roots_are_not_cleaned_twice(self):
        self.make_file(self.output / 'old.tmp', old=True)
        report = cleanup_local_cache(dict(output=str(self.output), images=str(self.output)), lambda *_: None,
                                     self.scratch)
        self.assertEqual(report['removed'], 1)
        self.assertEqual(report['freed_bytes'], 7)

    def test_output_summary_deduplicates_files_and_ignores_missing_files(self):
        video = self.make_file(self.output / 'product' / 'video.mp4', b'12345')
        rows = [dict(result_path=str(video)), dict(result_path=str(video)),
                dict(result_path=str(self.output / 'missing.mp4')), {}]
        self.assertEqual(output_summary(rows, self.output), dict(file_count=1, size_bytes=5))

    def test_output_deletion_rejects_outside_paths_and_directories(self):
        outside = self.make_file(self.root / 'user.mp4')
        for path in (outside, self.images, self.output):
            with self.subTest(path=path), self.assertRaises(ValueError):
                output_file(dict(result_path=str(path)), self.output)
        self.assertTrue(outside.exists())

    def test_empty_product_cleanup_keeps_output_root_and_nonempty_siblings(self):
        video = self.make_file(self.output / 'product' / 'nested' / 'video.mp4')
        sibling = self.make_file(self.output / 'other' / 'video.mp4')
        video.unlink()
        remove_empty_product_folders(video, self.output)
        self.assertFalse((self.output / 'product').exists())
        self.assertTrue(self.output.is_dir())
        self.assertTrue(sibling.exists())

    def test_empty_output_root_is_preserved_after_last_video_is_removed(self):
        video = self.make_file(self.output / 'product' / 'video.mp4')
        video.unlink()
        remove_empty_product_folders(video, self.output)
        self.assertFalse((self.output / 'product').exists())
        self.assertTrue(self.output.is_dir())


class HistoryCleanupUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = ExitStack()
        self.releases = []
        self.errors = []
        self.quit_on_close = self.app.quitOnLastWindowClosed()
        self.app.setQuitOnLastWindowClosed(False)
        self.network = self.patches.enter_context(patch('requests.sessions.Session.request',
            side_effect=AssertionError('history cleanup tests must not access network')))
        self.patches.enter_context(patch('sys.excepthook', side_effect=lambda kind, error, trace: self.errors.append(error)))
        self.root = Path(self.temp.name)
        self.output = self.root / 'output'
        self.images = self.root / 'images'
        self.scratch = self.root / 'scratch'
        prompts = self.root / 'prompts'
        for directory in (self.output, self.images, self.scratch, prompts):
            directory.mkdir()
        self.config = ConfigManager(self.root / 'config.json')
        self.config.config['paths'].update(output=str(self.output), images=str(self.images), prompts=str(prompts))
        self.config.config['api'].update(base_url='https://cleanup.invalid/v1', api_key='fixture-only')
        self.config.config['updates']['check_on_start'] = False
        self.config.config['task_strategy'].update(watch_interval=0, open_folder_after_download=False)
        self.config.save_config()
        self.window = MainWindow(self.config, network_time=False, model_sync=False)
        self.page = self.window.workspace_page
        self.history = self.window.history_page
        self.logs = self.patches.enter_context(patch.object(self.page, 'append_log'))
        self.window.show()
        self.window.activateWindow()
        QTest.qWait(60)
        self.wait_idle()

    def tearDown(self):
        try:
            for release in self.releases:
                release.set()
            for widget in QApplication.topLevelWidgets():
                if isinstance(widget, QDialog) and widget.isVisible():
                    widget.reject()
            self.page.shutdown()
            self.wait_idle()
            self.window.close()
            self.window.deleteLater()
            QTest.qWait(40)
            self.app.sendPostedEvents(None, QEvent.DeferredDelete)
            self.assertEqual([widget for widget in QApplication.topLevelWidgets()
                              if isinstance(widget, QDialog) and widget.isVisible()], [])
            self.assertEqual(self.errors, [])
            self.network.assert_not_called()
        finally:
            self.patches.close()
            self.app.setQuitOnLastWindowClosed(self.quit_on_close)
            self.temp.cleanup()

    def wait(self, condition, timeout=4000):
        deadline = time.monotonic() + timeout / 1000
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(condition(), 'background operation did not finish')

    def wait_idle(self):
        self.wait(lambda: not self.window._background_busy() and not self.page._history_maintenance_busy)
        QTest.qWait(20)

    def record(self, local_id='one', status='completed', size=1048576):
        video = self.output / local_id / 'video.mp4'
        video.parent.mkdir(exist_ok=True)
        video.write_bytes(b'v' * size)
        return dict(local_id=local_id, status=status, result_path=str(video), output_dir=str(self.output),
                    prompt_path=str(self.root / 'prompts' / (local_id + '.txt')), prompt_name=local_id,
                    product=local_id, model='video-v3', size_bytes=size, images=[], task_id='',
                    created_at='2026-10-12T08:31:40')

    def publish(self, records, live=True):
        self.config.history_store().upsert_many(records)
        self.page._publish_history(copy.deepcopy(records))
        if live:
            self.page.task_manager.tasks = copy.deepcopy(records)
            self.page.task_manager.current_index = 0 if records else -1
            self.page._tasks_updated(self.page.task_manager.tasks)
        self.page._update_recovery_bar()

    def mouse(self, widget):
        QTest.qWait(20)
        self.assertTrue(widget.isVisible())
        point = widget.mapToGlobal(widget.rect().center())
        top = widget.window()
        hit = QApplication.widgetAt(point)
        if hit is not widget:
            ancestors = []
            ancestor = widget.parentWidget()
            while ancestor is not None:
                ancestors.append(ancestor)
                ancestor = ancestor.parentWidget()
            self.assertIn(hit, ancestors)
            self.assertIs(top.childAt(top.mapFromGlobal(point)), widget)
        QTest.mouseClick(top.windowHandle(), Qt.LeftButton, pos=top.mapFromGlobal(point))

    def test_record_only_deletion_preserves_video_and_refreshes_history_queue_metrics(self):
        row = self.record()
        self.publish([row])
        self.page.delete_history_records([row], False)
        self.wait_idle()
        self.assertTrue(Path(row['result_path']).exists())
        self.assertEqual(self.config.history_store().count(), 0)
        self.assertEqual(self.page.task_manager.tasks, [])
        self.assertEqual(self.page._history_cache, [])
        self.assertEqual(self.history.table.rowCount(), 0)
        self.assertEqual(self.page.summary._metrics['bytes'], row['size_bytes'])
        self.assertIn('共 0 条记录', self.history.footer_left.text())

    def test_recycle_deletion_removes_file_empty_folder_and_updates_storage(self):
        row = self.record()
        self.publish([row])
        with patch('ui.pages.workspace_page.recycle_file', side_effect=lambda path: path.unlink()) as recycle:
            self.page.delete_history_records([row], True)
            self.wait_idle()
        recycle.assert_called_once_with(Path(row['result_path']))
        self.assertFalse(Path(row['result_path']).exists())
        self.assertFalse((self.output / 'one').exists())
        self.assertTrue(self.output.is_dir())
        self.assertEqual(self.config.history_store().count(), 0)
        self.assertEqual(self.page.summary._metrics['bytes'], 0)
        self.assertTrue(any('1.00 MB' in str(call) for call in self.logs.call_args_list))

    def test_running_and_queued_records_are_disabled_and_rejected_even_with_stale_ui_data(self):
        row = self.record(status='queued')
        self.publish([row])
        checkbox = self.history.table.cellWidget(0, 0).findChild(CheckBox)
        self.assertFalse(checkbox.isEnabled())
        self.assertIn('结束', checkbox.toolTip())
        with patch('ui.pages.workspace_page.InfoBar.warning') as warning:
            self.page.delete_history_records([dict(row, status='completed')], True)
            warning.assert_called_once()
        self.assertEqual(self.config.history_store().count(), 1)
        self.assertTrue(Path(row['result_path']).exists())

    def test_all_active_statuses_disable_selection(self):
        states = ('waiting', 'queued', 'uploading', 'submitting', 'processing', 'downloading', 'retry_wait', 'paused')
        rows = [dict(self.record(str(index), size=0), status=state) for index, state in enumerate(states)]
        self.publish(rows)
        self.assertTrue(all(not self.history.table.cellWidget(index, 0).findChild(CheckBox).isEnabled()
                            for index in range(len(rows))))
        self.assertFalse(self.history.delete_button.isEnabled())

    def test_orphan_uncertain_deletion_reuses_discard_and_removes_pending_count(self):
        row = self.record(status='submission_unknown')
        Path(ledger_path(runtime_config(self.config))).touch()
        self.publish([row])
        self.assertEqual(len(self.page._pending_records()), 1)
        with patch.object(self.page, '_discard_submission', wraps=self.page._discard_submission) as discard:
            self.page.delete_history_records([row], False)
            self.wait_idle()
        discard.assert_called_once()
        self.assertEqual(self.config.history_store().count(), 0)
        self.assertEqual(self.page._pending_records(), [])
        self.assertTrue(self.page.recovery_bar.isHidden())
        self.assertTrue(Path(row['result_path']).exists())

    def test_valid_unknown_ledger_is_discarded_and_reservation_can_be_claimed_again(self):
        row = self.record(status='submission_unknown')
        row.update(prompt_sha256='isolated-prompt', signature='isolated-generation')
        ledger = SubmissionLedger(ledger_path(runtime_config(self.config)))
        scope = account_scope(self.config.config)
        _, reserved = ledger.reserve(row, scope)
        reserved.update(status='submission_unknown', ledger_state='unknown')
        ledger.save(reserved, 'unknown')
        self.publish([reserved])
        self.page.delete_history_records([reserved], False)
        self.wait_idle()
        self.assertEqual(ledger.records(scope)[0]['ledger_state'], 'discarded')
        self.assertEqual(ledger.reserve(row, scope)[0], 'claimed')
        self.assertEqual(self.page._pending_records(), [])

    def test_uncertain_with_known_task_id_is_never_discarded_or_deleted(self):
        row = dict(self.record(status='submission_unknown'), task_id='existing-provider-task')
        self.publish([row])
        self.page.delete_history_records([row], True)
        self.wait_idle()
        self.assertEqual(self.config.history_store().count(), 1)
        self.assertTrue(Path(row['result_path']).exists())
        self.assertEqual(len(self.page._pending_records()), 1)

    def test_active_ledger_rejects_deletion_of_stale_orphan_history_without_touching_video(self):
        row = self.record(status='submission_unknown')
        row.update(prompt_sha256='isolated-prompt', signature='isolated-generation')
        ledger = SubmissionLedger(ledger_path(runtime_config(self.config)))
        scope = account_scope(self.config.config)
        _, reserved = ledger.reserve(row, scope)
        ledger.save(dict(reserved, task_id='already-created', status='processing'), 'active')
        stale = dict(reserved, status='submission_unknown', task_id='', ledger_state='unknown')
        self.publish([stale])
        with patch('ui.pages.workspace_page.recycle_file') as recycle:
            self.page.delete_history_records([stale], True)
            self.wait_idle()
        recycle.assert_not_called()
        self.assertTrue(Path(row['result_path']).exists())
        self.assertEqual(self.config.history_store().count(), 1)
        self.assertEqual(ledger.records(scope)[0]['ledger_state'], 'active')

    def test_orphan_missing_ledger_record_can_be_deleted_without_resolve_or_post(self):
        row = dict(self.record(status='submission_unknown'), ledger_id='missing', ledger_state='unknown')
        self.publish([row])
        with patch.object(SubmissionLedger, 'resolve', side_effect=AssertionError('delete must not resolve')):
            self.page.delete_history_records([row], False)
            self.wait_idle()
        self.assertEqual(self.config.history_store().count(), 0)
        self.assertEqual(self.page._pending_records(), [])

    def test_file_outside_output_roots_is_never_deleted(self):
        outside = self.root / 'personal.mp4'
        outside.write_bytes(b'personal')
        row = dict(self.record(), result_path=str(outside))
        self.publish([row])
        with patch('ui.pages.workspace_page.recycle_file') as recycle:
            self.page.delete_history_records([row], True)
            self.wait_idle()
        recycle.assert_not_called()
        self.assertEqual(outside.read_bytes(), b'personal')
        self.assertEqual(self.config.history_store().count(), 1)

    def test_recycle_failure_cancel_preserves_file_and_record(self):
        row = self.record()
        self.publish([row])
        with patch('ui.pages.workspace_page.recycle_file', side_effect=OSError('fixture unavailable')), \
                patch.object(PermanentDeleteDialog, 'exec', return_value=QDialog.Rejected) as confirm:
            self.page.delete_history_records([row], True)
            self.wait_idle()
        confirm.assert_called_once()
        self.assertTrue(Path(row['result_path']).exists())
        self.assertEqual(self.config.history_store().count(), 1)

    def test_permanent_deletion_only_after_second_explicit_confirmation(self):
        row = self.record()
        self.publish([row])
        confirmed = []

        def confirm(dialog):
            self.assertTrue(Path(row['result_path']).exists())
            self.assertEqual(self.config.history_store().count(), 1)
            confirmed.append(True)
            return QDialog.Accepted

        with patch('ui.pages.workspace_page.recycle_file', side_effect=OSError('fixture unavailable')), \
                patch.object(PermanentDeleteDialog, 'exec', confirm):
            self.page.delete_history_records([row], True)
            self.wait(lambda: bool(confirmed) and self.config.history_store().count() == 0)
            self.wait_idle()
        self.assertEqual(confirmed, [True])
        self.assertFalse(Path(row['result_path']).exists())
        self.assertEqual(self.page.summary._metrics['bytes'], 0)

    def test_shared_output_file_is_kept_until_all_referencing_records_are_selected(self):
        first = self.record()
        second = dict(first, local_id='two')
        self.publish([first, second])
        with patch('ui.pages.workspace_page.recycle_file', side_effect=lambda path: path.unlink()) as recycle:
            self.page.delete_history_records([first], True)
            self.wait_idle()
            recycle.assert_not_called()
            self.assertEqual(self.config.history_store().count(), 2)
            self.page.delete_history_records([first, second], True)
            self.wait_idle()
            recycle.assert_called_once()
        self.assertEqual(self.config.history_store().count(), 0)

    def test_selection_header_is_textless_and_centered_inside_resized_section(self):
        self.publish([self.record(size=0)])
        self.window.switchTo(self.history)
        QTest.qWait(40)
        header = self.history.selection_header
        checkbox = self.history.select_all
        self.assertEqual(self.history.table.horizontalHeaderItem(0).text(), '')
        self.assertEqual(checkbox.text(), '')
        for column_width, window_width in ((36, 1100), (92, 1400), (160, 1200), (48, 1000)):
            with self.subTest(column_width=column_width, window_width=window_width):
                self.history.table.setColumnWidth(0, column_width)
                self.window.resize(window_width, 800)
                QTest.qWait(20)
                section = QRect(header.sectionViewportPosition(0), 0,
                                header.sectionSize(0), header.viewport().height())
                self.assertTrue(section.contains(checkbox.geometry()))
                self.assertLessEqual(abs(section.center().x() - checkbox.geometry().center().x()), 1)
                self.assertLessEqual(abs(section.center().y() - checkbox.geometry().center().y()), 1)
                option = QStyleOptionButton()
                option.initFrom(checkbox)
                indicator = checkbox.style().subElementRect(QStyle.SE_CheckBoxIndicator, option, checkbox)
                self.assertLessEqual(abs(section.center().x() - checkbox.mapTo(header.viewport(), indicator.center()).x()), 1)
                row_checkbox = self.history.table.cellWidget(0, 0).findChild(CheckBox)
                self.assertLessEqual(abs(checkbox.mapToGlobal(checkbox.rect().center()).x()
                                         - row_checkbox.mapToGlobal(row_checkbox.rect().center()).x()), 1)

    def test_selection_hosts_are_transparent_borderless_and_keep_row_selection(self):
        self.publish([self.record(size=0)])
        self.window.switchTo(self.history)
        QTest.qWait(40)
        table = self.history.table
        host = table.cellWidget(0, 0)
        checkbox = host.findChild(CheckBox)
        self.assertIn('border: none', host.styleSheet())
        self.assertIn('background: transparent', host.styleSheet())
        self.assertFalse(host.autoFillBackground())
        self.assertEqual(host.layout().contentsMargins().left(), 0)
        self.assertEqual(host.layout().spacing(), 0)
        self.assertEqual(checkbox.text(), '')
        self.assertLessEqual(abs(host.rect().center().x() - checkbox.geometry().center().x()), 1)
        self.assertLessEqual(abs(host.rect().center().y() - checkbox.geometry().center().y()), 1)
        self.assertEqual(checkbox.size(), self.history.select_all.size())
        self.assertTrue(table.item(0, 0).flags() & Qt.ItemIsSelectable)
        table.setCurrentCell(0, 0)
        self.assertEqual(table.currentRow(), 0)
        self.mouse(checkbox)
        self.assertEqual(self.history._selected_ids, {'one'})

    def test_selected_selection_cell_does_not_paint_left_indicator_or_focus_frame(self):
        self.publish([self.record(size=0)])
        self.window.switchTo(self.history)
        QTest.qWait(40)
        table = self.history.table
        table.selectRow(0)
        self.assertIn(0, table.delegate.selectedRows)
        accent = QColor(255, 0, 255)
        table.setCheckedColor(accent, accent)
        image = QImage(table.columnWidth(0), table.rowHeight(0), QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        option = QStyleOptionViewItem(table.viewOptions())
        option.rect = image.rect()
        option.state |= QStyle.State_HasFocus | QStyle.State_Selected
        painter = QPainter(image)
        try:
            with patch('PyQt5.QtWidgets.QStyledItemDelegate.paint') as base_paint:
                table.itemDelegate().paint(painter, option, table.model().index(0, 0))
                painted_option = base_paint.call_args.args[1]
                self.assertFalse(painted_option.state & QStyle.State_HasFocus)
        finally:
            painter.end()
        self.assertFalse(any(image.pixelColor(column, row) == accent
                             for column in range(4, 8) for row in range(12, image.height() - 12)))
        self.assertTrue(option.state & QStyle.State_HasFocus)
        with patch('PyQt5.QtWidgets.QStyledItemDelegate.paint') as base_paint:
            painter = QPainter(image)
            try:
                table.itemDelegate().paint(painter, QStyleOptionViewItem(option), table.model().index(0, 1))
                self.assertTrue(base_paint.call_args.args[1].state & QStyle.State_HasFocus)
            finally:
                painter.end()

    def test_select_all_coordinates_count_only_eligible_rows_and_filter_clears_hidden_selection(self):
        rows = [self.record('one', 'failed', 0), self.record('two', 'completed', 0),
                self.record('three', 'queued', 0)]
        self.publish(rows)
        self.window.switchTo(self.history)
        QTest.qWait(40)
        self.mouse(self.history.select_all)
        self.assertEqual(self.history._selected_ids, {'one', 'two'})
        self.assertIn('（2）', self.history.delete_button.text())
        selected_row = next(row for row, record in enumerate(self.history.records) if record['local_id'] == 'one')
        self.mouse(self.history.table.cellWidget(selected_row, 0).findChild(CheckBox))
        self.assertEqual(self.history.select_all.checkState(), Qt.PartiallyChecked)
        self.mouse(self.history.select_all)
        self.assertEqual(self.history._selected_ids, {'one', 'two'})
        self.assertEqual(self.history.select_all.checkState(), Qt.Checked)
        self.history.filter_box.setCurrentText('失败')
        self.assertEqual(self.history._selected_ids, {'one'})
        self.assertEqual(self.history.select_all.checkState(), Qt.Checked)
        self.mouse(self.history.select_all)
        self.assertEqual(self.history._selected_ids, set())
        self.assertFalse(self.history.delete_button.isEnabled())

    def test_incremental_status_update_keeps_checkbox_identity_but_clears_ineligible_selection(self):
        row = self.record(status='failed')
        self.publish([row])
        checkbox = self.history.table.cellWidget(0, 0).findChild(CheckBox)
        checkbox.setChecked(True)
        self.history.update_history([dict(row, status='processing')])
        self.assertIs(self.history.table.cellWidget(0, 0).findChild(CheckBox), checkbox)
        self.assertFalse(checkbox.isEnabled())
        self.assertFalse(checkbox.isChecked())
        self.assertEqual(self.history._selected_ids, set())

    def test_delete_dialog_coordinates_require_explicit_unknown_confirmation_and_cancel_never_deletes(self):
        row = self.record(status='submission_unknown')
        dialog = DeleteHistoryDialog([row], dict(file_count=1, size_bytes=1048576), self.window)
        try:
            dialog.show()
            dialog.move(20, 20)
            QTest.qWait(30)
            self.assertFalse(dialog.yesButton.isEnabled())
            self.assertTrue(dialog.reason.text())
            self.mouse(dialog.delete_files)
            self.assertTrue(dialog.delete_files.isChecked())
            self.assertFalse(dialog.keep_files.isChecked())
            self.mouse(dialog.uncertain_confirm)
            self.assertTrue(dialog.yesButton.isEnabled())
            QTimer.singleShot(40, lambda: self.mouse(dialog.cancelButton))
            self.assertEqual(dialog.exec(), QDialog.Rejected)
            self.assertTrue(Path(row['result_path']).exists())
        finally:
            safe_delete_dialog(dialog)

    def test_history_delete_signal_connects_to_actual_backend_after_confirming_dialog(self):
        row = self.record()
        self.publish([row])
        self.history.table.cellWidget(0, 0).findChild(CheckBox).setChecked(True)
        summary_calls = []
        gui_thread = threading.get_ident()

        def summary(records, output):
            summary_calls.append(threading.get_ident())
            return output_summary(records, output)

        def confirm(dialog):
            self.assertTrue(dialog.keep_files.isChecked())
            return QDialog.Accepted

        with patch('ui.pages.history_page.output_summary', side_effect=summary), \
                patch.object(DeleteHistoryDialog, 'exec', confirm):
            self.history._request_delete()
            self.wait(lambda: self.config.history_store().count() == 0)
            self.wait_idle()
        self.assertEqual(len(summary_calls), 1)
        self.assertNotEqual(summary_calls[0], gui_thread)
        self.assertTrue(Path(row['result_path']).exists())

    def test_cache_button_runs_background_cleanup_and_refreshes_with_exact_freed_bytes(self):
        stale_paths = [self.output / 'old.part', self.images / 'old.tmp', self.scratch / 'storyboard-reference-old.jpg']
        for path in stale_paths:
            path.write_bytes(b'cache')
            timestamp = time.time() - 172800
            os.utime(path, (timestamp, timestamp))
        row = self.record()
        self.publish([row])
        calls = []

        def clean(paths, log):
            calls.append(threading.get_ident())
            return cleanup_local_cache(paths, log, self.scratch)

        with patch('ui.pages.workspace_page.cleanup_local_cache', side_effect=clean):
            self.history.clean_cache_requested.emit()
            self.wait_idle()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], threading.get_ident())
        self.assertTrue(all(not path.exists() for path in stale_paths))
        self.assertTrue(Path(row['result_path']).exists())
        self.assertEqual(self.config.history_store().count(), 1)
        self.assertTrue(any('已清理 3' in str(call) for call in self.logs.call_args_list))

    def test_slow_cleanup_is_nonblocking_and_repeated_clicks_do_not_start_another_worker(self):
        release = threading.Event()
        started = threading.Event()
        self.releases.append(release)

        def slow(paths, log):
            started.set()
            release.wait(3)
            return dict(removed=0, freed_bytes=0)

        with patch('ui.pages.workspace_page.cleanup_local_cache', side_effect=slow) as cleanup:
            self.page.clean_cache_now()
            self.wait(started.is_set)
            ticks = []
            QTimer.singleShot(10, lambda: ticks.append(True))
            self.wait(lambda: bool(ticks))
            self.assertTrue(self.page._history_maintenance_busy)
            self.assertFalse(self.page.start_button.isEnabled())
            self.assertFalse(self.history.clean_cache_button.isEnabled())
            self.assertTrue(self.history.clean_cache_button.toolTip())
            self.page.clean_cache_now()
            self.page.open_controls()
            self.assertFalse(self.page.controls_dialog.isVisible())
            self.assertEqual(cleanup.call_count, 1)
            release.set()
            self.wait_idle()
        self.assertTrue(self.history.clean_cache_button.isEnabled())
        self.assertTrue(self.page.start_button.isEnabled())

    def test_delete_waits_for_existing_history_write_and_cannot_be_resurrected_by_it(self):
        row = self.record()
        self.publish([row])
        release = threading.Event()
        started = threading.Event()
        self.releases.append(release)
        store = self.config.history_store()

        def write():
            started.set()
            release.wait(3)
            store.upsert(row)

        self.page.history_jobs.start(write, lambda _: None, lambda message: self.fail(message))
        self.wait(started.is_set)
        self.page._history_pending.append(dict(row, error='pending write'))
        self.page.delete_history_records([row], False)
        self.assertTrue(self.page._history_maintenance_busy)
        self.assertEqual(store.count(), 1)
        release.set()
        self.wait_idle()
        self.assertEqual(store.count(), 0)
        self.assertEqual(self.page._history_pending, [])
        self.assertEqual(self.page._history_cache, [])

    def test_full_pending_pool_matches_batch_rows_beyond_history_display_limit_and_logs_orphans(self):
        rows = [dict(self.record(str(index), 'submission_unknown', 0), ledger_state='unknown')
                for index in range(102)]
        self.publish(rows)
        self.assertEqual(self.history.table.rowCount(), 100)
        self.assertIn('102 条', self.page.recovery_label.text())
        counts = []

        def reject(dialog):
            counts.append(len(dialog._rows))
            return QDialog.Rejected

        with patch.object(SubmissionRecoveryBatchDialog, 'exec', reject):
            self.history.batch_resolve_requested.emit(None)
            self.wait_idle()
        self.assertEqual(counts, [102])
        self.assertTrue(any('孤儿待确认记录' in str(call) for call in self.logs.call_args_list))
        self.page.delete_history_records(rows, False)
        self.wait_idle()
        self.assertEqual(self.config.history_store().count(), 0)
        self.assertEqual(self.page._pending_records(), [])
        self.assertTrue(self.page.recovery_bar.isHidden())

    def test_pending_ledger_state_uses_same_normalized_rows_for_bar_and_batch(self):
        row = dict(self.record(status='failed', size=0), ledger_state='unknown', ledger_id='orphan')
        self.publish([row])
        self.assertIn('1 条', self.page.recovery_label.text())
        shown = []

        def reject(dialog):
            shown.extend(dialog.records)
            return QDialog.Rejected

        with patch.object(SubmissionRecoveryBatchDialog, 'exec', reject):
            self.page._open_pending_recovery()
            self.wait_idle()
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]['status'], 'submission_unknown')
        self.assertEqual(shown[0]['local_id'], row['local_id'])

    def test_cleanup_failure_restores_buttons_and_preserves_pending_writes(self):
        with patch('ui.pages.workspace_page.cleanup_local_cache', side_effect=OSError('fixture failed')):
            self.page.clean_cache_now()
            self.wait_idle()
        self.assertTrue(self.history.clean_cache_button.isEnabled())
        self.assertTrue(self.page.start_button.isEnabled())
        self.assertTrue(any('清理未完成' in str(call) for call in self.logs.call_args_list))
