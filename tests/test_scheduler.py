from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from core.config_manager import ConfigManager
from core.scheduler import ScheduleEngine


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.config['schedule'] = dict(enabled=True, time='23:00', mode='once', after_finish='keep', next_run='', last_run='')
        self.now = datetime(2026, 9, 11, 22, 54)
        self.logs = []
        self.engine = ScheduleEngine(self.config, now=lambda: self.now, log=lambda message, level: self.logs.append((message, level)))

    def test_once_reminder_and_consume_before_start_even_after_restart(self):
        self.now = datetime(2026, 9, 11, 22, 55)
        self.assertFalse(self.engine.tick())
        self.engine.tick()
        self.assertEqual(sum('距离定时开始还有5分钟' in message for message, _ in self.logs), 1)
        self.now = datetime(2026, 9, 11, 23)
        self.assertTrue(self.engine.tick())
        reopened = ConfigManager(self.config.path); reopened.load_config()
        self.assertFalse(reopened.config['schedule']['enabled'])
        self.assertFalse(ScheduleEngine(reopened, now=lambda: self.now).tick())

    def test_daily_busy_skips_occurrence_and_schedules_next_day(self):
        self.config.config['schedule']['mode'] = 'daily'; self.engine.configure(reset=True)
        self.now = datetime(2026, 9, 11, 23)
        self.assertFalse(self.engine.tick(busy=True))
        self.assertEqual(self.engine.next_run, datetime(2026, 9, 12, 23))
        self.assertTrue(any('正在运行' in message and '跳过' in message for message, _ in self.logs))
        self.now = datetime(2026, 9, 12, 23)
        self.assertTrue(self.engine.tick())
        self.assertEqual(self.engine.next_run, datetime(2026, 9, 13, 23))

    def test_disable_cancels_and_time_change_rearms(self):
        self.config.config['schedule']['enabled'] = False; self.engine.configure(reset=True)
        self.now += timedelta(days=1)
        self.assertFalse(self.engine.tick()); self.assertIsNone(self.engine.next_run)
        self.config.config['schedule'].update(enabled=True, time='22:56')
        self.engine.configure(reset=True)
        self.now = self.now.replace(hour=22, minute=56)
        self.assertTrue(self.engine.tick())

    def test_configuration_persistence_failure_cannot_start_paid_task(self):
        self.now = datetime(2026, 9, 11, 23)
        self.config.update = lambda *args, **kwargs: False
        self.assertFalse(self.engine.tick())
        self.assertTrue(any(level == 'error' for _, level in self.logs))

    def test_restart_does_not_replay_missed_occurrence(self):
        self.config.config['schedule'].update(mode='daily', next_run='2026-09-10T23:00:00')
        self.engine.configure()
        self.assertEqual(self.engine.next_run, datetime(2026, 9, 11, 23))
        self.assertFalse(self.engine.tick())

    def test_backward_clock_calibration_never_replays_consumed_daily_occurrence(self):
        self.config.config['schedule']['mode'] = 'daily'; self.engine.configure(reset=True)
        self.now = datetime(2026, 9, 11, 23)
        self.assertTrue(self.engine.tick())
        self.now = datetime(2026, 9, 11, 22, 50)
        self.engine.configure(reset=True)
        self.assertEqual(self.engine.next_run, datetime(2026, 9, 12, 23))
        self.now = datetime(2026, 9, 11, 23)
        self.assertFalse(self.engine.tick())
        self.config.config['schedule']['next_run'] = '2026-09-11T23:00:00'
        self.now = datetime(2026, 9, 11, 22, 50)
        self.engine.configure()
        self.assertEqual(self.engine.next_run, datetime(2026, 9, 12, 23))
