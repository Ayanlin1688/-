"""Native/local Stage 5 acceptance: count POSTs, preview and persisted recovery."""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    os.environ['QT_QPA_PLATFORM'] = 'windows' if args.native and sys.platform == 'win32' else 'offscreen'
    from PyQt5.QtWidgets import QApplication
    from core.config_manager import ConfigManager
    from core.task_manager import TaskManager
    from core.submission_safety import SubmissionGate
    from core.prompt_converter import h3_to_v2, v2_to_h3
    from test_prompt_converter import H3_PROMPT, V2_TAIL
    from test_submission_safety import SafetyServer
    from test_task_manager import wait_until
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    evidence = ROOT / 'artifacts' / 'stage5'
    evidence.mkdir(parents=True, exist_ok=True)
    logs, scenarios = [], []
    for scenario in ('重复提交', '响应丢失', '429限流', '503故障', '错误响应已有ID'):
        with tempfile.TemporaryDirectory(prefix='stage5-') as folder, SafetyServer() as server:
            root = Path(folder)
            prompts = root / 'prompts'; prompts.mkdir()
            (prompts / '1.txt').write_text('A car under soft daylight.', encoding='utf-8')
            config = ConfigManager(root / 'config.json')
            config.config['paths'].update(prompts=str(prompts), output=str(root / 'videos'))
            config.config['api'].update(base_url=server.base, api_key='local-fixture-only')
            config.config['workspace'].update(model='video-v3', poll_interval=3)
            config.config['task_strategy'].update(unmatched_prompt='仍提交文生视频', retry_interval=1)
            config.save_config()
            if scenario == '响应丢失':
                server.drop_response = True
            elif scenario == '429限流':
                server.create_status = 429
            elif scenario in {'503故障', '错误响应已有ID'}:
                server.create_status = 503
                if scenario == '错误响应已有ID':
                    server.error_id = 'recovered-local-id'
            window = MainWindow(config, network_time=False)
            window.setWindowTitle('阶段5本地验收 · 不调用付费生成')
            window.show()
            page = window.workspace_page
            page.task_manager.log_message.connect(lambda text, level: logs.append((scenario, level, text)))
            try:
                wait_until(lambda: not page.jobs.busy and page.queue_panel._tasks)
                config.config['workspace']['poll_interval'] = .01
                page.start_button.click()
                page.start_generation()  # A repeated click cannot start a second worker.
                wait_until(lambda: not page.task_manager.is_running, timeout=15000)
                tasks = page.task_manager.tasks
                expected = 'completed' if scenario in {'重复提交', '错误响应已有ID'} else 'submission_unknown'
                assert tasks[0]['status'] == expected, tasks[0].get('error')
                assert len(server.calls) == 1
                if scenario == '重复提交':
                    page.start_button.click()
                    wait_until(lambda: not page.task_manager.is_running)
                    assert page.task_manager.tasks[0]['status'] == 'duplicate'
                if expected == 'submission_unknown':
                    assert page.current_task.resolve_button.isVisible()
                    restarted = TaskManager()
                    from ui.model_catalog_controller import runtime_config
                    restarted.start_tasks(runtime_config(config))
                    wait_until(lambda: not restarted.is_running)
                    assert restarted.tasks[0]['status'] == 'submission_unknown'
                assert len(server.calls) == 1
                scenarios.append(dict(name=scenario, passed=True, creation_requests=1, state=page.task_manager.tasks[0]['status']))
            finally:
                window.close()
                wait_until(lambda: not window._background_busy(), timeout=10000)
                window.deleteLater()
                app.processEvents()
    converted = h3_to_v2(H3_PROMPT)
    assert converted.endswith(V2_TAIL) and '@图10' in converted
    restored = v2_to_h3(converted, duration=10)
    assert '[Shot 2] At 00:05.000,' in restored and '<Picture 10>' in restored
    (evidence / 'h3-to-v2.txt').write_text(converted, encoding='utf-8')
    (evidence / 'v2-to-h3.txt').write_text(restored, encoding='utf-8')
    now = [0.0]
    gate = SubmissionGate(5, now=lambda: now[0])
    delays = []
    for _ in range(5):
        delays.append(gate.failed(429)); now[0] += delays[-1]
    assert delays == [5, 10, 20, 40, 60] and gate.limit == 1 and gate.paused
    report = dict(ok=True, platform=app.platformName(), paid_provider_submissions=0,
                  scenarios=scenarios, backoff_seconds=delays, concurrency_after_429=gate.limit,
                  new_submissions_paused=gate.paused, conversion_roundtrip=True)
    (evidence / 'execution.log').write_text('\n'.join(f'[{case}][{level.upper()}] {text}' for case, level, text in logs), encoding='utf-8')
    (evidence / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
