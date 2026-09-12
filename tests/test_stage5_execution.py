import copy
import json
from pathlib import Path
import tempfile
import unittest

from PyQt5.QtWidgets import QApplication
from core.config_manager import DEFAULT_CONFIG
from core.task_manager import TaskManager
from core.submission_ledger import SubmissionLedger
from test_task_manager import wait_until
from test_pool_execution import PoolServer
from test_prompt_converter import H3_PROMPT, V2_TAIL


class Stage5ExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prompts = self.root / 'prompts'; self.prompts.mkdir()
        self.path = self.prompts / '1.txt'
        self.path.write_text(H3_PROMPT, encoding='utf-8')
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config['paths'].update(prompts=str(self.prompts), output=str(self.root / 'out'))
        self.config['workspace'].update(duration=10, poll_interval=.01)
        self.config['task_strategy'].update(unmatched_prompt='仍提交文生视频', retry_interval=.01)
        self.config['_submission_ledger_path'] = str(self.root / 'ledger.sqlite3')
        self.manager = TaskManager()

    def tearDown(self):
        self.manager.cancel_all()
        wait_until(lambda: not self.manager.is_running)
        self.temp.cleanup()

    def start(self, server):
        self.config['api'].update(base_url=server.base, api_key='local-fixture')
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running, timeout=12000)
        return [json.loads(body) for path, _, body in server.calls if path == '/videos']

    def test_failover_converts_original_and_stores_exact_wire_prompt(self):
        self.config['model_pool'].update(enabled=True, cooldown=.01, models=[dict(name='video-v2', enabled=True)])
        with PoolServer() as server:
            server.remote_fail_models = {'MiniMax-H3'}
            bodies = self.start(server)
            self.assertEqual([body['model'] for body in bodies], ['MiniMax-H3', 'video-v2'])
            self.assertIn('subject_definitions:', bodies[0]['prompt'])
            self.assertIn('人物站位：', bodies[1]['prompt'])
            self.assertIn('@Image10', bodies[1]['prompt'])
            self.assertTrue(bodies[1]['prompt'].endswith(V2_TAIL))
            task = self.manager.tasks[0]
            self.assertEqual(task['submitted_prompt'], bodies[1]['prompt'])
            self.assertEqual(task['original_prompt'], H3_PROMPT)
            self.assertIn('@图10', task['converted_prompt'])
            self.assertNotEqual(task['attempts'][0]['task_id'], task['task_id'])

    def test_preserve_original_off_never_writes_snapshot_to_history_or_ledger(self):
        self.config['prompt_conversion']['preserve_original'] = False
        self.config['model_overrides'][str(self.path)] = 'video-v2'
        records = []
        self.manager.record_updated.connect(records.append)
        with PoolServer() as server:
            self.start(server)
            self.assertTrue(records)
            self.assertTrue(all('original_prompt' not in task and '_original_prompt' not in task for task in records))
            data = Path(self.config['_submission_ledger_path']).read_bytes()
            self.assertNotIn(b'Metadata that must not become a shot.', data)
            self.assertEqual(self.path.read_text(encoding='utf-8'), H3_PROMPT)

    def test_same_format_failover_precedes_other_format_model(self):
        v2 = '人物站位：人物在左侧。\n镜头1：人物看向窗外。\n' + V2_TAIL
        self.path.write_text(v2, encoding='utf-8')
        self.config['model_pool'].update(enabled=True, cooldown=.01, models=[
            dict(name='video-v2', enabled=True), dict(name='MiniMax-H3', enabled=True), dict(name='video-v3', enabled=True)])
        with PoolServer() as server:
            server.remote_fail_models = {'video-v2'}
            bodies = self.start(server)
            self.assertEqual([body['model'] for body in bodies], ['video-v2', 'video-v3'])

    def test_completed_rerun_requires_explicit_one_batch_signature(self):
        self.config['model_overrides'][str(self.path)] = 'MiniMax-H3'
        with PoolServer() as server:
            self.start(server)
            task = copy.deepcopy(self.manager.tasks[0])
            self.start(server)
            self.assertEqual(len(server.jobs), 1)
            self.assertEqual(self.manager.tasks[0]['status'], 'duplicate')
            self.config['_rerun_signatures'] = [task['signature']]
            self.start(server)
            self.assertEqual(len(server.jobs), 2)

    def test_known_id_restart_queries_without_creating_and_preserves_model(self):
        self.config['workspace']['poll_timeout'] = .02
        self.config['task_strategy']['auto_retry'] = False
        with PoolServer() as server:
            server.processing_seconds = 1
            self.start(server)
            original = self.manager.tasks[0]['task_id']
            self.manager = TaskManager()
            self.config['workspace']['poll_timeout'] = 10
            self.start(server)
            self.assertEqual(len(server.jobs), 1)
            self.assertEqual(self.manager.tasks[0]['task_id'], original)
            self.assertEqual(self.manager.tasks[0]['status'], 'completed')
