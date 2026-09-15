"""Unattended-operation guarantees: global concurrency, bounded retries, asset-miss
alarms, disk guard and persistent run logs — all against a local HTTP fixture."""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication
from core.config_manager import ConfigManager, DEFAULT_CONFIG
from core.task_manager import TaskManager
from core.disk_guard import cleanup_disk
from core.run_log import RunLog
from test_pool_execution import PoolServer
from test_task_manager import wait_until


class UnattendedReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config['paths'] = {key: str(self.root / key) for key in ('prompts', 'images', 'output')}
        for path in self.config['paths'].values():
            Path(path).mkdir()
        self.config['prompt_detection']['enabled'] = False
        self.config['workspace'].update(model='video-v3', duration=8, resolution='720p', poll_interval=.02)
        self.config['task_strategy'].update(retry_interval=.01)
        self.manager = TaskManager()
        self.logs = []
        self.manager.log_message.connect(lambda message, level: self.logs.append((level, message)))

    def tearDown(self):
        self.manager.cancel_all()
        wait_until(lambda: not self.manager.is_running, timeout=10000)
        self.temp.cleanup()

    def product(self, name, count, images=1):
        prompts = Path(self.config['paths']['prompts']) / name
        prompts.mkdir()
        if images:
            pictures = Path(self.config['paths']['images']) / name
            pictures.mkdir()
        for number in range(1, count + 1):
            (prompts / f'{name}{number}.txt').write_text(f'{name} 场景{number}', encoding='utf-8')
            if images:
                (pictures / f'{number}.png').write_bytes(f'image-{name}-{number}'.encode())

    def run_batch(self, server):
        self.config['api'].update(base_url=server.base, api_key='***', upload_url=server.base + '/upload')
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running, timeout=45000)

    def test_global_concurrency_across_products_default_five(self):
        for product, count in (('玫瑰毯子', 3), ('车载灯', 3)):
            self.product(product, count)
        self.assertEqual(self.config['task_strategy']['max_concurrency'], 5)
        with PoolServer() as server:
            server.processing_seconds = .5
            self.run_batch(server)
            self.assertEqual([t['status'] for t in self.manager.tasks], ['completed'] * 6)
            # 超过单产品规模（3）→ 确实跨产品并行，且峰值接近全队列上限。
            self.assertGreaterEqual(server.maximum, 4)
            self.assertGreaterEqual(max(e[3] for e in server.events if e[0] == 'submit'), 4)
            self.assertTrue(any('最大并发5' in message for _, message in self.logs))
            self.assertTrue(all(Path(t['result_path']).is_file() for t in self.manager.tasks))

    def test_retry_limit_three_marks_failure_and_queue_continues(self):
        self.product('故障组', 2, images=0)
        self.config['task_strategy'].update(auto_retry=True, max_retries=3, unmatched_prompt='仍提交文生视频')
        with PoolServer() as server:
            server.remote_fail_models = {'video-v3'}
            self.run_batch(server)
            tasks = self.manager.tasks
            self.assertEqual([t['status'] for t in tasks], ['failed', 'failed'])
            # 1 次初始尝试 + 3 次重试 = 4 次，封顶不无限重试。
            self.assertEqual([len(t['attempts']) for t in tasks], [4, 4])
            self.assertTrue(any('标记失败并保存现场' in message for _, message in self.logs))
            self.assertTrue(any('队列处理完毕' in message for _, message in self.logs))

    def test_asset_miss_alarm_logged_and_flow_continues(self):
        self.product('漏检组', 2, images=0)
        self.config['task_strategy']['unmatched_prompt'] = '仍提交文生视频'
        with PoolServer() as server:
            self.run_batch(server)
            self.assertTrue(any('资产漏检告警' in message for _, message in self.logs))
            self.assertTrue(any('未匹配到参考图' in message for _, message in self.logs))
            self.assertEqual([t['status'] for t in self.manager.tasks], ['completed'] * 2)

    def test_disk_guard_removes_only_stale_temp_files(self):
        output = Path(self.config['paths']['output'])
        old_part = output / 'a.part'; old_part.write_bytes(b'x' * 128)
        old_tmp = output / 'b.tmp'; old_tmp.write_bytes(b'y' * 64)
        fresh_part = output / 'c.part'; fresh_part.write_bytes(b'z')
        keep_video = output / 'done.mp4'; keep_video.write_bytes(b'v' * 32)
        stale = time.time() - 10 * 86400
        os.utime(old_part, (stale, stale)); os.utime(old_tmp, (stale, stale))
        records = []
        result = cleanup_disk(self.config['paths'], lambda message, level='info': records.append((level, message)), retention_days=7)
        self.assertEqual(result['removed'], 2)
        self.assertFalse(old_part.exists()); self.assertFalse(old_tmp.exists())
        self.assertTrue(fresh_part.exists()); self.assertTrue(keep_video.exists())
        self.assertTrue(any('磁盘清理' in message for _, message in records))

    def test_run_log_writes_lines_and_trims_old_files(self):
        logs_dir = self.root / 'logs'
        run_log = RunLog(logs_dir)
        run_log.write('冒烟测试：任务开始', 'warning')
        today = time.strftime('%Y%m%d')
        target = logs_dir / f'run-{today}.log'
        self.assertTrue(target.is_file())
        content = target.read_text(encoding='utf-8')
        self.assertIn('[WARNING] 冒烟测试：任务开始', content)
        stale = logs_dir / 'run-20200101.log'
        stale.write_text('old', encoding='utf-8')
        old_stamp = time.time() - 60 * 86400
        os.utime(stale, (old_stamp, old_stamp))
        RunLog(logs_dir)  # 新实例启动时自动清理过期日志
        self.assertFalse(stale.exists())

    def test_config_migration_upgrades_old_defaults_once(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps({'task_strategy': {'max_concurrency': 1, 'max_retries': 5}}), encoding='utf-8')
        loaded = ConfigManager(path).load_config()
        self.assertEqual(loaded['task_strategy']['max_concurrency'], 5)
        self.assertEqual(loaded['task_strategy']['max_retries'], 3)
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(saved.get('migrations', {}).get('r6_unattended_defaults'))
        self.assertEqual(saved['task_strategy']['max_concurrency'], 5)
        # 用户显式调回 1 后不再被迁移覆盖。
        saved['task_strategy']['max_concurrency'] = 1
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        reloaded = ConfigManager(path).load_config()
        self.assertEqual(reloaded['task_strategy']['max_concurrency'], 1)


if __name__ == '__main__':
    unittest.main()
