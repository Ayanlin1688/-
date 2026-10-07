"""Watch callbacks must honor disabling and keep per-scan trigger ownership."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from ui.pages.workspace_page import WorkspacePage
try:
    from .test_task_manager import wait_until
except ImportError:
    from test_task_manager import wait_until


class WatchCallbackRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = ConfigManager(root / 'config.json')
        for name in ('prompts', 'images', 'output'):
            (root / name).mkdir()
            self.config.config['paths'][name] = str(root / name)
        self.config.config['task_strategy']['watch_interval'] = 5
        self.config.config['prompt_detection']['enabled'] = False
        self.page = WorkspacePage(self.config)
        QTest.qWait(50)
        wait_until(lambda: not self.page.jobs.busy)
        self.page._watch_seen = set()
        self.page._watch_timer.stop()
        self.requests = []
        self.page.task_manager.start_tasks = lambda config: self.requests.append(config)
        self.page.refresh_metrics = lambda: None
        self.callbacks = []
        self.capture = patch.object(self.page.jobs, 'start',
                                   side_effect=lambda work, done, fail: self.callbacks.append((done, fail)))
        self.capture.start()
        self.tasks = [dict(prompt_path=str(root / 'prompts' / 'new.txt'), prompt_name='new',
                           images=[], matched=False, model='video-v3')]
        self.result = (self.tasks, self.tasks, [])

    def tearDown(self):
        self.capture.stop()
        self.page.shutdown()
        wait_until(lambda: not self.page.jobs.busy)
        self.page.deleteLater()
        QTest.qWait(20)
        self.temp.cleanup()

    def test_disabled_watch_does_not_run_queued_timeout(self):
        self.config.config['task_strategy']['watch_interval'] = 0
        self.page._watch_fired()
        for done, _ in self.callbacks:
            done(self.result)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.page._watch_timer.isActive())

    def test_disabling_watch_during_scan_prevents_automatic_submission(self):
        self.page._watch_fired()
        self.config.config['task_strategy']['watch_interval'] = 0
        self.callbacks[0][0](self.result)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.page._watch_timer.isActive())

    def test_stale_manual_scan_cannot_consume_watch_trigger(self):
        self.page.scan_sources()
        self.page._watch_fired()
        self.assertEqual(len(self.callbacks), 1)
        self.callbacks[0][0](self.result)
        self.assertEqual(len(self.callbacks), 1)
        self.assertEqual(self.requests, [])

    def test_stale_failure_cannot_consume_watch_trigger(self):
        self.page.scan_sources()
        self.page._watch_fired()
        self.assertEqual(len(self.callbacks), 1)
        self.callbacks[0][1]('obsolete scan failed')
        self.assertEqual(len(self.callbacks), 1)
        self.assertEqual(self.requests, [])

    def test_reentrant_scan_is_reported_and_releases_busy_state(self):
        with patch('ui.pages.workspace_page.InfoBar.info') as info:
            self.page.scan_sources()
            self.assertTrue(self.page._interactive_scan_busy)
            self.assertFalse(self.page.start_button.isEnabled())
            self.page.scan_sources()
            info.assert_called_once()
            self.assertEqual(len(self.callbacks), 1)
        self.callbacks[0][0](self.result)
        self.assertFalse(self.page._interactive_scan_busy)
        self.assertTrue(self.page.start_button.isEnabled())

    def test_manual_scan_superseding_watch_rearms_listener(self):
        self.page._watch_fired()
        self.page.scan_sources()
        self.assertEqual(len(self.callbacks), 1)
        self.callbacks[0][0](self.result)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.page._watch_timer.isActive())


if __name__ == '__main__':
    unittest.main()
