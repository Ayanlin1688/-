"""幂等键：创建请求携带 Idempotency-Key；管线生成的任务键稳定可用。"""
import copy
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent))
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from core.api_client import ApiClient
from core.config_manager import DEFAULT_CONFIG
from core.task_manager import TaskManager
from test_http_clients import LocalServer


def header_value(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


class IdempotencyKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_create_request_carries_idempotency_header(self):
        with LocalServer() as server:
            client = ApiClient(server.base, 'key-for-test', log=lambda *args: None)
            params = dict(duration=8, aspect_ratio='16:9', resolution='720p', generate_audio=True)
            task_id = client.create_task('video-v3', '测试提示词', [], params, None, idempotency_key='yl-demo-123')
            self.assertTrue(task_id)
            headers = server.calls[-1][1]
            self.assertEqual(header_value(headers, 'Idempotency-Key'), 'yl-demo-123')
            client.close()

    def test_pipeline_sends_generated_idempotency_key(self):
        with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('提示词', '图片', '输出'):
                (root / name).mkdir()
            (root / '提示词' / '1汽车.txt').write_text('<Picture 1> 环绕汽车，场景1', encoding='utf-8')
            (root / '图片' / '1汽车.png').write_bytes(b'good-image')
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['paths'] = dict(prompts=str(root / '提示词'), images=str(root / '图片'), output=str(root / '输出'))
            config['api'].update(base_url=server.base, api_key='***', upload_url=server.base + '/upload')
            config['workspace']['poll_interval'] = 0.05
            manager = TaskManager()
            manager.start_tasks(config)
            deadline = time.monotonic() + 15
            while manager.is_running and time.monotonic() < deadline:
                QTest.qWait(20)
            posts = [call for call in server.calls if call[0] == '/videos']
            self.assertEqual(len(posts), 1)
            key = header_value(posts[0][1], 'Idempotency-Key') or ''
            self.assertTrue(key.startswith('yl-'), key)
            self.assertEqual(len(key), 43)  # 'yl-' + 40 位十六进制


if __name__ == '__main__':
    unittest.main()
