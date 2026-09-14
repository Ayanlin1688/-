"""Workspace interactions using isolated files; never submits paid requests."""
import copy
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import Qt, QPoint, QPointF, QMimeData
from PyQt5.QtGui import QImage, QColor, QDragEnterEvent, QDropEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from core.config_manager import ConfigManager
from core.matcher import StoryboardMatcher
from ui.main_window import MainWindow
from test_task_manager import wait_until


class WorkspaceDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.config = ConfigManager(root / 'config.json')
        self.window = MainWindow(self.config, network_time=False)
        self.window.show()
        QTest.qWait(80)
        self.page = self.window.workspace_page
        self.prompt = root / '玫瑰毯子1.txt'
        self.prompt.write_text('<Picture 1> <Picture 2> <Picture 3>', encoding='utf-8')
        self.paths = []
        for n, color in enumerate(('red', 'green', 'blue'), 1):
            path = root / f'1({n}).png'
            image = QImage(180, 120, QImage.Format_RGB32)
            image.fill(QColor(color)); image.save(str(path))
            self.paths.append(str(path))
        self.tasks = StoryboardMatcher().match_files([self.prompt], self.paths)
        self.tasks[0].update(model='MiniMax-H3', status='waiting', progress=0)
        self.page._tasks_updated(self.tasks)

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater(); QTest.qWait(30)
        self.temp.cleanup()

    def test_reordered_references_survive_rescan_and_restart(self):
        self.assertTrue(hasattr(self.page, 'reorder_references'), 'workspace needs persisted drag ordering')
        reordered = [self.paths[2], self.paths[0], self.paths[1]]
        self.page.reorder_references(0, reordered)
        config = ConfigManager(self.config.path).load_config()
        result = StoryboardMatcher.from_config(config).match_files([self.prompt], self.paths)
        self.assertEqual(result[0]['images'], reordered)
        self.assertEqual(self.page.queue_panel._tasks[0]['images'], reordered)

    def test_reorder_rejects_lost_or_added_images(self):
        self.assertTrue(hasattr(self.page, 'reorder_references'))
        self.page.reorder_references(0, self.paths[:2])
        self.assertEqual(self.page.queue_panel._tasks[0]['images'], self.paths)
        self.assertFalse(self.config.config['match_overrides'])

    def test_running_task_references_cannot_be_reordered(self):
        self.assertTrue(hasattr(self.page, 'reorder_references'))
        self.page.task_manager.is_running = True
        try:
            self.page.reorder_references(0, list(reversed(self.paths)))
            self.assertEqual(self.page.queue_panel._tasks[0]['images'], self.paths)
        finally:
            self.page.task_manager.is_running = False

    def test_dropping_third_picture_on_first_persists_the_displayed_order(self):
        row = self.page.queue_panel.rows[0]
        source, target = row.references.thumbnails[2], row.references.thumbnails[0]
        mime = QMimeData(); mime.setData(source.MIME, f'{source.owner}:2'.encode())
        enter = QDragEnterEvent(QPoint(40, 40), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(target, enter)
        self.assertTrue(enter.isAccepted())
        drop = QDropEvent(QPointF(40, 40), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(target, drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(row.references.paths, [self.paths[2], self.paths[0], self.paths[1]])
        restored = ConfigManager(self.config.path).load_config()
        self.assertEqual(restored['match_overrides'][str(self.prompt)], [self.paths[2], self.paths[0], self.paths[1]])

    def test_summary_counts_follow_task_and_output_changes(self):
        from ui.widgets.workspace_summary import directory_metrics
        self.page._update_summary()
        self.assertEqual(self.page.summary.cards[0].number.text(), '1')
        self.assertIn('1 已匹配', self.page.summary.cards[0].detail.text())
        result = directory_metrics(dict(images=str(self.prompt.parent), output=''))
        self.assertEqual(result['images'], 3)
        output = self.prompt.parent / 'output'; output.mkdir()
        (output / 'example.mp4').write_bytes(b'fixture-video')
        result = directory_metrics(dict(images='', output=str(output)))
        self.assertEqual((result['videos'], result['bytes']), (1, 13))

    def test_disabling_view_autosave_does_not_disable_reference_persistence(self):
        self.page.auto_save.setChecked(False)
        self.page.queue_panel.filter_box.setCurrentText('失败')
        self.page.reorder_references(0, list(reversed(self.paths)))
        restored = ConfigManager(self.config.path).load_config()
        self.assertFalse(restored['workspace_view']['auto_save'])
        self.assertNotEqual(restored['workspace_view'].get('task_filter'), '失败')
        self.assertEqual(restored['match_overrides'][str(self.prompt)], list(reversed(self.paths)))

    def test_log_drag_height_and_collapsed_state_restore_on_expansion(self):
        page = self.page
        page.splitter.setSizes([400, 190]); QTest.qWait(60)
        page._save_view()
        saved = ConfigManager(self.config.path).load_config()['workspace_view']['log_height']
        page.log_drawer.toggle(); QTest.qWait(240)
        self.assertFalse(page.log_drawer.browser.isVisible())
        page.log_drawer.toggle(); QTest.qWait(240)
        self.assertTrue(page.log_drawer.browser.isVisible())
        self.assertLessEqual(abs(page.log_drawer.height()-saved), 3)

    def test_preview_opens_actual_clicked_reference(self):
        self.assertTrue(hasattr(self.page.queue_panel, 'rows'), 'expanded rows expose image controls')
        row = self.page.queue_panel.rows[0]
        QTest.mouseClick(row.references.thumbnails[1], Qt.LeftButton)
        QTest.qWait(60)
        preview = self.page.image_preview
        self.assertTrue(preview.isVisible())
        self.assertEqual(preview.path, self.paths[1])
        self.assertEqual(preview.original.toImage().pixelColor(40, 40), QColor('green'))
        preview.close()

    def test_status_updates_keep_row_and_reference_widgets(self):
        self.assertTrue(hasattr(self.page.queue_panel, 'rows'))
        row = self.page.queue_panel.rows[0]
        changed = copy.deepcopy(self.tasks)
        changed[0].update(status='processing', progress=42)
        self.page._tasks_updated(changed)
        self.assertIs(self.page.queue_panel.rows[0], row)
        self.assertEqual(row.progress.value(), 42)
        self.assertIn('生成中', row.status_label.text())
        self.assertEqual(len(row.references.thumbnails), 3)

    def test_worker_indexed_progress_reaches_every_visible_row_without_a_snapshot(self):
        from core.task_manager import TaskWorker
        from ui.model_catalog_controller import runtime_config
        tasks = [dict(self.tasks[0], prompt_name=f'任务{i}', local_id=str(i), status='processing') for i in range(2)]
        self.page._tasks_updated(tasks)
        worker = TaskWorker(runtime_config(self.config), [], self.page)
        self.page.task_manager.worker = worker
        self.page._running_changed(True)
        try:
            worker.indexed_progress.emit(0, 32, 4, 7)
            worker.indexed_progress.emit(1, 71, 5, 2)
            QApplication.processEvents()
            self.assertEqual([row.progress.value() for row in self.page.queue_panel.rows], [32, 71])
        finally:
            self.page.task_manager.worker = None
            self.page._running_changed(False)
            worker.deleteLater()

    def test_log_filter_info_excludes_other_levels_and_escapes_html(self):
        logs = self.page.log_drawer
        logs.clear()
        logs.append_log('information only', 'info')
        logs.append_log('<unsafe> failure', 'error')
        logs.filter_box.setCurrentText('INFO')
        self.assertIn('information only', logs.browser.toPlainText())
        self.assertNotIn('failure', logs.browser.toPlainText())
        logs.filter_box.setCurrentText('ERROR')
        self.assertIn('<unsafe> failure', logs.browser.toPlainText())
        self.assertNotIn('information only', logs.browser.toPlainText())

    def test_toolbar_navigation_and_unknown_api_status(self):
        self.assertTrue(hasattr(self.page, 'navigation_tabs'))
        self.page.navigation_tabs['history'].click(); QTest.qWait(350)
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.history_page)
        self.window.switchTo(self.page); QTest.qWait(350)
        self.assertNotEqual(self.page.api_status.text(), '已连接')

    def test_row_retry_uses_ledger_and_redownload_never_creates_another_video(self):
        from test_http_clients import LocalServer
        with LocalServer() as server:
            self.config.config['api'].update(base_url=server.base, api_key='test-only', upload_url=server.base + '/upload')
            self.config.config['paths'].update(prompts=str(self.prompt.parent), images=str(self.prompt.parent), output=str(self.prompt.parent / 'output'))
            self.config.config['prompt_detection']['enabled'] = False
            self.config.config['workspace'].update(model='video-v3', poll_interval=.01)
            self.page.scan_sources(); wait_until(lambda: not self.page.jobs.busy)
            self.page.task_action(0, 'retry')
            wait_until(lambda: not self.page.task_manager.is_running)
            record = copy.deepcopy(self.page.task_manager.tasks[0])
            self.assertEqual(record['status'], 'completed')
            result = Path(record['result_path']); self.assertTrue(result.exists()); result.unlink()
            self.page.queue_panel.rows[0].download_button.click()
            wait_until(lambda: not self.page.jobs.busy)
            self.assertTrue(result.exists())
            # Even a stale failure row cannot bypass the completed ledger record.
            self.page._tasks_updated([dict(record, status='failed')])
            self.page.task_action(0, 'retry')
            wait_until(lambda: not self.page.task_manager.is_running)
            self.assertEqual(self.page.task_manager.tasks[0]['status'], 'duplicate')
            self.assertEqual(sum(path == '/videos' for path, _, _ in server.calls), 1)
