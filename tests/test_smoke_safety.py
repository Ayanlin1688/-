"""The opt-in CLI must honor the same persisted intent as the desktop app."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication
from core.config_manager import ConfigManager
from core.model_catalog import builtin_models
from core.submission_ledger import SubmissionLedger, account_scope
from core.task_manager import TaskWorker
from ui.model_catalog_controller import runtime_config
from test_submission_safety import SafetyServer


class SmokeSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cli_blocks_desktop_unknown_intent_and_writes_report_beside_config(self):
        with tempfile.TemporaryDirectory() as folder, SafetyServer() as server:
            root = Path(folder)
            prompts = root / 'materials' / 'prompts'
            prompts.mkdir(parents=True)
            (prompts / '1.txt').write_text('A car under soft daylight.', encoding='utf-8')
            manager = ConfigManager(root / 'config.json')
            manager.config['paths'].update(prompts=str(prompts), output=str(root / 'videos'))
            manager.config['api'].update(base_url=server.base, api_key='local-fixture-only')
            manager.config['workspace']['poll_interval'] = 1
            manager.config['task_strategy'].update(unmatched_prompt='仍提交文生视频', auto_retry=False)
            manager.save_config()
            config = runtime_config(manager)
            config['_model_catalog'] = builtin_models()
            worker = TaskWorker(config, [])
            worker._scan()
            ledger = SubmissionLedger(config['_submission_ledger_path'])
            _, record = ledger.reserve(worker.tasks[0], account_scope(config))
            ledger.save(record, 'submitting')
            script = Path(__file__).resolve().parent.parent / 'scripts' / 'run_live_smoke.py'
            isolated_root = root / 'script-root'
            isolated_root.mkdir()
            launch = ('from pathlib import Path; import scripts.run_live_smoke as smoke; '
                      f'smoke.ROOT = Path({str(isolated_root)!r}); raise SystemExit(smoke.main())')
            # Isolate even a regressed ROOT-relative report path from user files.
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', launch, '--config', str(manager.path), '--submit'],
                                    cwd=script.parent.parent, capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(server.calls, [], result.stdout + result.stderr)
            self.assertEqual(server.gets, [])
            self.assertEqual(result.returncode, 1)
            report = json.loads((root / 'live-smoke-result.json').read_text(encoding='utf-8'))
            self.assertEqual(report['tasks'][0]['status'], 'submission_unknown')
