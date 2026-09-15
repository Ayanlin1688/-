"""Rose-blanket batch acceptance using isolated files and local HTTP only."""
import copy
import gc
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication, QFileDialog
from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtTest import QTest
from core.config_manager import ConfigManager, DEFAULT_CONFIG
from core.task_manager import TaskManager
from test_pool_execution import PoolServer
from test_task_manager import wait_until
from ui.main_window import MainWindow


class BatchRegressionTests(unittest.TestCase):
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
        self.config['workspace'].update(model='MiniMax-H3', resolution='768p', poll_interval=.02)
        self.config['prompt_detection']['enabled'] = False
        self.config['task_strategy'].update(auto_retry=False)
        # 本文件验证串行（逐产品推进）语义与跨产品日志边界；全队列并发由
        # test_unattended_reliability 的专项测试覆盖。
        self.config['task_strategy']['max_concurrency'] = 1
        self.manager = TaskManager()
        self.logs = []
        self.manager.log_message.connect(lambda text, level: self.logs.append(text))

    def tearDown(self):
        self.manager.cancel_all()
        wait_until(lambda: not self.manager.is_running)
        # Release closed Fluent windows on the GUI thread, before a later
        # HTTP worker can trigger cyclic GC during a theme registry refresh.
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        gc.collect()
        self.temp.cleanup()

    def product(self, name, count, images=True):
        prompts = Path(self.config['paths']['prompts']) / name
        prompts.mkdir()
        pictures = Path(self.config['paths']['images']) / name
        if images:
            pictures.mkdir()
        for number in range(1, count + 1):
            (prompts / f'{name}{number}.txt').write_text(f'商品展示 场景{number}', encoding='utf-8')
            if images:
                for view in (3, 1, 2):
                    (pictures / f'{number}({view}).png').write_bytes(f'image-{name}-{number}-{view}'.encode())

    def run_batch(self, server):
        self.config['api'].update(base_url=server.base, api_key='local-fixture-only', upload_url=server.base + '/upload')
        self.manager.start_tasks(self.config)
        wait_until(lambda: not self.manager.is_running, timeout=15000)

    def test_five_rose_prompts_all_submit_with_logs_before_next_product(self):
        self.product('玫瑰毯子', 5)
        self.product('车载灯', 1)
        with PoolServer() as server:
            self.run_batch(server)
            self.assertEqual([t['status'] for t in self.manager.tasks], ['completed'] * 6)
            self.assertEqual([e[0] for e in server.events], ['submit', 'done'] * 6)
            payloads = [json.loads(body) for path, _, body in server.calls if path == '/videos']
            self.assertEqual(len(payloads), 6)
            self.assertEqual([len(p['images']) for p in payloads], [3] * 6)
            for index, task in enumerate(self.manager.tasks, 1):
                self.assertEqual(Path(task['result_path']).parent.name, task['product'])
                self.assertTrue(Path(task['result_path']).is_file())
                self.assertEqual(task['submitted_image_count'], 3)
                prefix = f'任务{index}/6：'
                self.assertIn(prefix + f'提示词={task["prompt_name"]}.txt，产品={task["product"]}，模型=MiniMax-H3', self.logs)
                self.assertTrue(any(line.startswith(prefix + '绑定参考图3张：') for line in self.logs))
                self.assertIn(prefix + '参数=比例16:9，分辨率768p，时长8秒', self.logs)
                self.assertIn(prefix + '上传图片3张...', self.logs)
            self.assertTrue(any('共6个提示词，6个已匹配，0个未匹配' in line for line in self.logs))
            self.assertTrue(any('开始处理产品：玫瑰毯子，共5个任务' in line for line in self.logs))

    def test_missing_product_images_obey_text_submission_policy(self):
        self.product('玫瑰毯子', 4, images=False)
        self.config['task_strategy']['unmatched_prompt'] = '仍提交文生视频'
        with PoolServer() as server:
            self.run_batch(server)
            self.assertEqual([t['status'] for t in self.manager.tasks], ['completed'] * 4)
            payloads = [json.loads(body) for path, _, body in server.calls if path == '/videos']
            self.assertEqual([p['workflow_id'] for p in payloads], ['text-to-video'] * 4)
            self.assertTrue(all(Path(t['result_path']).parent.name == '玫瑰毯子' for t in self.manager.tasks))

    def test_default_policy_skips_all_missing_product_images(self):
        self.product('玫瑰毯子', 4, images=False)
        with PoolServer() as server:
            self.run_batch(server)
            self.assertEqual([t['status'] for t in self.manager.tasks], ['skipped'] * 4)
            self.assertEqual(server.calls, [])

    def test_all_directory_pickers_persist_and_reopen_scans_without_selection(self):
        self.product('玫瑰毯子', 5)
        path = self.root / 'config.json'
        window = MainWindow(ConfigManager(path), network_time=False)
        try:
            source = window.workspace_page.data_source
            for key, directory in self.config['paths'].items():
                with patch.object(QFileDialog, 'getExistingDirectory', return_value=directory):
                    source._choose(key, source.fields[key])
            wait_until(lambda: len(source.matches) == 5 and not window.workspace_page.jobs.busy)
            self.assertEqual(ConfigManager(path).load_config()['paths'], self.config['paths'])
            window.close(); window.deleteLater(); QTest.qWait(40)
            window = MainWindow(ConfigManager(path), network_time=False)
            source = window.workspace_page.data_source
            wait_until(lambda: len(source.matches) == 5 and not window.workspace_page.jobs.busy)
            self.assertEqual([len(t['images']) for t in source.matches], [3] * 5)
            for key, directory in self.config['paths'].items():
                self.assertEqual(source.fields[key].text(), directory)
                with patch.object(QFileDialog, 'getExistingDirectory', return_value='') as chooser:
                    source._choose(key, source.fields[key])
                    self.assertEqual(chooser.call_args.args[2], directory)
        finally:
            window.close(); window.deleteLater(); QTest.qWait(50)
