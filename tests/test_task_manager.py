import copy
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from core.config_manager import DEFAULT_CONFIG
from core.task_manager import TaskManager, TaskControl
from test_http_clients import LocalServer


def wait_until(predicate, timeout=30000):
    # 默认超时面向 CI 共享运行器：慢机日（曾观察 15s 等待仍超时）需要更大余量。
    deadline = time.monotonic() + timeout / 1000
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    if not predicate():
        raise AssertionError('condition did not become true before timeout')


class TaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = LocalServer(); self.server = self.fixture.__enter__()
        root = Path(self.temp.name)
        for name in ['提示词', '图片', '输出']:
            (root / name).mkdir()
        for i in range(1, 4):
            (root / '提示词' / f'{i}汽车.txt').write_text(f'<Picture 1> 环绕汽车，场景{i}', encoding='utf-8')
            (root / '图片' / f'{i}汽车.png').write_bytes(b'good-image')
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        # These legacy fixtures verify the original no-retry branch. Stage2B
        # retry behavior has separate local HTTP integration coverage.
        self.config['task_strategy']['auto_retry'] = False
        self.config['paths'] = dict(prompts=str(root / '提示词'), images=str(root / '图片'), output=str(root / '输出'))
        self.config['api'].update(base_url=self.server.base, api_key='local-test', upload_url=self.server.base + '/upload')
        self.config['workspace']['poll_interval'] = 0.04
        self.manager = TaskManager()
        self.records = []
        self.manager.record_updated.connect(self.records.append)

    def tearDown(self):
        self.manager.cancel_all()
        wait_until(lambda: not self.manager.is_running)
        self.fixture.__exit__()
        self.temp.cleanup()

    def test_end_to_end_pause_after_current_resume_no_duplicate_submission(self):
        # “暂停在当前任务之后”是串行语义；全队列并发由专项测试覆盖。
        self.config['task_strategy']['max_concurrency'] = 1
        ticks = []; timer = QTimer(); timer.setInterval(5); timer.timeout.connect(lambda: ticks.append(1)); timer.start()
        paused = [False]
        def pause(index, task):
            if index == 0 and task['task_id'] and not paused[0]:
                paused[0] = True
                self.manager.pause_tasks()
        self.manager.current_task_changed.connect(pause)
        self.manager.start_tasks(self.config)
        wait_until(lambda: self.manager.tasks and self.manager.tasks[0]['status'] == 'completed')
        QTest.qWait(120)
        self.assertEqual(len([p for p, _, _ in self.server.calls if p == '/videos']), 1)
        self.assertTrue(self.manager.is_paused)
        self.manager.resume_tasks()
        wait_until(lambda: not self.manager.is_running)
        timer.stop()
        self.assertGreater(len(ticks), 10)
        self.assertEqual([t['status'] for t in self.manager.tasks], ['completed'] * 3)
        self.assertEqual(Path(self.manager.tasks[0]['result_path']).name, '001_1汽车.mp4')
        self.assertTrue(all(Path(t['result_path']).exists() for t in self.manager.tasks))
        self.assertTrue(all('Authorization' not in headers for path, headers, _ in self.server.calls if path == '/upload'))
        self.assertEqual(len([r for r in self.records if r['status'] == 'completed']), 3)
        submitted = len(self.server.calls)
        self.config['workspace'].update(poll_interval=.1, max_retries=9, skip_threshold=9)
        self.config['paths']['output'] = str(Path(self.temp.name) / '新输出目录')
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running)
        self.assertEqual(len(self.server.calls), submitted)
        self.assertEqual([t['status'] for t in self.manager.tasks], ['duplicate'] * 3)

    def test_upload_failure_continues_and_retains_task_id_on_download_error(self):
        (Path(self.config['paths']['images']) / '1汽车.png').write_bytes(b'bad-image')
        self.config['download_settings']['naming_rule'] = '{不支持}.mp4'
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running)
        self.assertEqual([t['status'] for t in self.manager.tasks], ['failed'] * 3)
        self.assertFalse(self.manager.tasks[0]['task_id'])
        self.assertTrue(self.manager.tasks[1]['task_id'])
        self.assertTrue(self.manager.tasks[1]['result_url'])

    def test_unreadable_prompt_fails_only_its_task_before_upload(self):
        path = sorted(Path(self.config['paths']['prompts']).glob('*.txt'))[0]
        path.write_bytes(b'\xff\xfeinvalid-utf8')
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running)
        self.assertEqual([task['status'] for task in self.manager.tasks], ['failed', 'completed', 'completed'])
        self.assertFalse(self.manager.tasks[0]['task_id'])
        self.assertIn('读取提示词失败', self.manager.tasks[0]['error'])
        self.assertEqual(len([row for row in self.server.calls if row[0] == '/videos']), 2)

    def test_skip_current_then_cancel_marks_remaining(self):
        # “跳过当前、推进到下一个”是串行语义；全队列并发由专项测试覆盖。
        self.config['task_strategy']['max_concurrency'] = 1
        self.server.always_processing = True
        self.manager.start_tasks(self.config)
        wait_until(lambda: self.manager.tasks and self.manager.tasks[0]['status'] == 'processing')
        self.manager.skip_current()
        wait_until(lambda: self.manager.current_index == 1 and self.manager.tasks[1]['status'] == 'processing')
        self.manager.cancel_all()
        wait_until(lambda: not self.manager.is_running)
        self.assertEqual([t['status'] for t in self.manager.tasks], ['skipped', 'cancelled', 'cancelled'])
        self.assertTrue(self.manager.tasks[0]['task_id'])

    def test_unmatched_pause_and_resume_as_text(self):
        self.config['paths']['images'] = ''
        self.config['task_strategy']['unmatched_prompt'] = '暂停任务'
        self.config['_task_limit'] = 1
        self.manager.start_tasks(self.config)
        wait_until(lambda: self.manager.is_paused)
        self.assertEqual(self.server.calls, [])
        self.manager.resume_tasks()
        wait_until(lambda: not self.manager.is_running)
        self.assertEqual(self.manager.tasks[0]['status'], 'completed', self.manager.tasks[0])

    def test_unmatched_skip_submits_nothing(self):
        self.config['paths']['images'] = ''
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running)
        self.assertEqual(self.server.calls, [])
        self.assertEqual([t['status'] for t in self.manager.tasks], ['skipped'] * 3)

    def test_h3_truncates_reference_images_to_nine_and_logs_warning(self):
        prompt_dir = Path(self.config['paths']['prompts'])
        image_dir = Path(self.config['paths']['images'])
        for path in prompt_dir.glob('*.txt'):
            path.unlink()
        for path in image_dir.glob('*'):
            path.unlink()
        prompt = prompt_dir / '玫瑰毯子1.txt'
        prompt.write_text('玫瑰毯子商品展示', encoding='utf-8')
        for index in range(1, 11):
            (image_dir / f'1({index}).png').write_bytes(b'good-image')
        self.config['prompt_detection']['enabled'] = False
        self.config['workspace']['model'] = 'MiniMax-H3'
        self.config['workspace']['resolution'] = '768p'
        logs = []
        self.manager.log_message.connect(lambda message, level: logs.append((message, level)))

        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running)

        self.assertEqual(self.manager.tasks[0]['status'], 'completed', repr(self.manager.tasks[0]))
        self.assertEqual(self.manager.tasks[0]['submitted_image_count'], 9)
        self.assertEqual(len([p for p, _, _ in self.server.calls if p == '/upload']), 9)
        self.assertTrue(any('最多支持9张，已自动截取前9张' in message for message, _ in logs))
        self.assertEqual(len(self.manager.tasks[0]['images']), 10)
        count = len(self.server.calls)
        self.manager = TaskManager()
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running)
        self.assertEqual(self.manager.tasks[0]['status'], 'duplicate')
        self.assertEqual(len(self.server.calls), count)

    def test_api_failure_continues_and_poll_timeout_preserves_ids(self):
        self.server.reject = True
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running)
        self.assertEqual(len([p for p, _, _ in self.server.calls if p == '/videos']), 3)
        self.assertTrue(all(t['status'] == 'submission_unknown' and not t['task_id'] for t in self.manager.tasks))
        from core.submission_ledger import SubmissionLedger, account_scope, ledger_path
        ledger = SubmissionLedger(ledger_path(self.config))
        for task in self.manager.tasks:
            ledger.resolve(task['ledger_id'], account_scope(self.config), confirmed_not_created=True)
        self.server.reject = False
        self.server.always_processing = True
        self.config['workspace']['poll_timeout'] = .08
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running)
        self.assertTrue(all(t['status'] == 'failed' and t['task_id'] for t in self.manager.tasks))
        self.assertEqual([t['sequence'] for t in self.manager.tasks], [1, 2, 3])

    def test_stale_skip_cannot_skip_the_next_task(self):
        control = TaskControl()
        control.begin(0)
        control.begin(1)
        control.skip_for(0)
        control.check()
        self.assertFalse(control.skipped)
