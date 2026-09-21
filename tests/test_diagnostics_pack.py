"""诊断包：密钥脱敏 + 内容齐全 + 可解压读取。"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from core.config_manager import ConfigManager
from core.diagnostics_pack import build_diagnostics_pack


class DiagnosticsPackTests(unittest.TestCase):
    def test_pack_redacts_secrets_and_includes_sections(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = ConfigManager(root / 'config.json')
            config = manager.load_config()
            config['api']['api_key'] = 'super-secret-value'
            config['api']['upload_api_key'] = 'upload-secret-value'
            manager.save_config(config)
            manager.history_upsert({'local_id': 'test-1', 'status': 'completed', 'product': '样例'})
            (root / 'logs').mkdir(exist_ok=True)
            (root / 'logs' / 'run-test.log').write_text('hello log', encoding='utf-8')
            target = build_diagnostics_pack(manager, root / 'diag.zip')
            self.assertTrue(target.is_file())
            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
                self.assertIn('environment.txt', names)
                self.assertIn('config.redacted.json', names)
                self.assertIn('history-recent.json', names)
                self.assertIn('logs/run-test.log', names)
                blob = archive.read('config.redacted.json').decode('utf-8')
                self.assertNotIn('super-secret-value', blob)
                self.assertNotIn('upload-secret-value', blob)
                payload = json.loads(blob)
                self.assertEqual(payload['api']['api_key'], '***')
                self.assertEqual(payload['api']['upload_api_key'], '***')
                history = json.loads(archive.read('history-recent.json').decode('utf-8'))
                self.assertEqual(history[0]['local_id'], 'test-1')


if __name__ == '__main__':
    unittest.main()
