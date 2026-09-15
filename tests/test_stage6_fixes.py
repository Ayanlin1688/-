"""Stage 6 fixes: reference caps, directory memory, and batch execution logs."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from core.config_manager import DEFAULT_CONFIG, ConfigManager
from core.task_manager import TaskManager
from test_http_clients import LocalServer
from test_task_manager import wait_until


class DirectoryMemoryTests(unittest.TestCase):
    def test_directory_fields_mirror_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            manager = ConfigManager(path)
            manager.load_config()
            manager.update(('paths', 'prompts'), 'D:/prompts')
            manager.update(('paths', 'images'), 'D:/images')
            manager.update(('paths', 'output'), 'D:/output')
            document = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(document['prompts_dir'], 'D:/prompts')
            self.assertEqual(document['images_dir'], 'D:/images')
            self.assertEqual(document['output_dir'], 'D:/output')
            restored = ConfigManager(path).load_config()
            self.assertEqual(restored['paths']['prompts'], 'D:/prompts')
            self.assertEqual(restored['paths']['images'], 'D:/images')
            self.assertEqual(restored['paths']['output'], 'D:/output')
            self.assertEqual(restored['prompts_dir'], 'D:/prompts')

    def test_legacy_alias_fields_migrate_on_load(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps({
                'prompts_dir': 'E:/p', 'images_dir': 'E:/i', 'output_dir': 'E:/o',
            }), encoding='utf-8')
            config = ConfigManager(path).load_config()
            self.assertEqual(config['paths']['prompts'], 'E:/p')
            self.assertEqual(config['paths']['images'], 'E:/i')
            self.assertEqual(config['paths']['output'], 'E:/o')


class BatchExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _make_config(self, root, server, model=None):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['paths'] = {'prompts': str(root / '提示词'), 'images': str(root / '图片'),
                           'output': str(root / '输出')}
        config['api'].update(base_url=server.base, api_key='***', upload_url=server.base + '/upload')
        config['workspace']['poll_interval'] = 0.02
        config['prompt_detection']['enabled'] = False
        if model:
            config['workspace']['model'] = model
            config['workspace']['resolution'] = '768p'
        return config

    def test_all_matched_tasks_execute_with_binding_logs(self):
        with tempfile.TemporaryDirectory() as temp, LocalServer() as server:
            root = Path(temp)
            prompts = root / '提示词'; images = root / '图片'
            prompts.mkdir(); images.mkdir()
            for i in range(1, 6):
                (prompts / f'玫瑰毯子{i}.txt').write_text(f'<Picture 1> 展示产品，场景{i}', encoding='utf-8')
                (images / f'玫瑰毯子{i}(1).jpg').write_bytes(b'good-image')
            config = self._make_config(root, server)
            manager = TaskManager()
            logs = []
            manager.log_message.connect(lambda message, level: logs.append((message, level)))
            try:
                manager.start_tasks(config)
                wait_until(lambda: not manager.is_running)
                self.assertEqual([t['status'] for t in manager.tasks], ['completed'] * 5)
                self.assertEqual(len([p for p, _, _ in server.calls if p == '/videos']), 5)
                summary = [m for m, _ in logs if '共5个提示词，5个已匹配，0个未匹配' in m]
                self.assertTrue(summary)
                binding = [m for m, _ in logs if '：绑定参考图' in m and '张：' in m]
                self.assertEqual(len(binding), 5)
                self.assertIn('任务1/5：绑定参考图1张：玫瑰毯子1(1).jpg', binding[0])
                params = [m for m, _ in logs if m.startswith('任务1/5：参数=比例')]
                self.assertTrue(params and '分辨率720p，时长8秒' in params[0])
            finally:
                manager.cancel_all()
                wait_until(lambda: not manager.is_running)

    def test_h3_binding_above_cap_truncates_and_submits_nine(self):
        with tempfile.TemporaryDirectory() as temp, LocalServer() as server:
            root = Path(temp)
            prompts = root / '提示词'; images = root / '图片'
            prompts.mkdir(); images.mkdir()
            (prompts / '玫瑰毯子1.txt').write_text('展示产品', encoding='utf-8')
            for i in range(1, 13):
                (images / f'玫瑰毯子1({i}).jpg').write_bytes(b'good-image')
            config = self._make_config(root, server, model='MiniMax-H3')
            manager = TaskManager()
            logs = []
            manager.log_message.connect(lambda message, level: logs.append((message, level)))
            try:
                manager.start_tasks(config)
                wait_until(lambda: not manager.is_running)
                self.assertEqual(manager.tasks[0]['status'], 'completed')
                self.assertTrue(any('已自动截取前9张' in m for m, _ in logs))
                self.assertTrue(any(m.startswith('任务1/1：绑定参考图9张') for m, _ in logs))
                body = [c for c in server.calls if c[0] == '/videos'][-1][2]
                payload = json.loads(body)
                self.assertEqual(len(payload['images']), 9)
                self.assertEqual(payload['workflow_id'], 'multi-reference')
            finally:
                manager.cancel_all()
                wait_until(lambda: not manager.is_running)


class DirectoryMemoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_saved_directories_reload_and_auto_scan_on_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = root / '提示词'; images = root / '图片'
            prompts.mkdir(); images.mkdir()
            (prompts / '玫瑰毯子1.txt').write_text('<Picture 1> 展示产品', encoding='utf-8')
            (images / '玫瑰毯子1(1).jpg').write_bytes(b'image')
            manager = ConfigManager(root / 'config.json')
            manager.load_config()
            manager.config['paths'] = {'prompts': str(prompts), 'images': str(images), 'output': str(root / '输出')}
            manager.save_config()
            from ui.main_window import MainWindow
            for _ in range(2):
                window = MainWindow(ConfigManager(root / 'config.json'), network_time=False)
                window.show()
                try:
                    wait_until(lambda: not window.workspace_page.jobs.busy
                               and len(window.workspace_page.data_source.matches) == 1)
                    self.assertEqual(window.workspace_page.queue_panel.list.count(), 1)
                    self.assertEqual(window.workspace_page.data_source.fields['prompts'].text(), str(prompts))
                finally:
                    window.close()
                    wait_until(lambda: not window._background_busy())
                    window.deleteLater()


if __name__ == '__main__':
    unittest.main()
