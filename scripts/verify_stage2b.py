"""Stage2B native/offscreen UI + local HTTP acceptance; no production config."""
import argparse
import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'windows' if '--native' in sys.argv and sys.platform == 'win32' else 'offscreen')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from core.config_manager import ConfigManager
from core.image_uploader import connection_probe_png
from ui.main_window import MainWindow
from test_pool_execution import PoolServer
from test_task_manager import wait_until


def verify():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    evidence = ROOT / 'artifacts' / 'stage2b'; evidence.mkdir(parents=True, exist_ok=True)
    result = dict(kind='local HTTP + Qt UI', provider_requests=0, platform=os.environ['QT_QPA_PLATFORM'], scenarios=[])
    logs = []
    cases = [('轮询三模型', 3), ('提交错误故障转移', 1), ('断网重试五次', 1), ('并发2跑5任务', 5), ('冷却恢复', 1)]
    for name, count in cases:
        with tempfile.TemporaryDirectory(prefix='storyboard-stage2b-') as temp, PoolServer() as server:
            root = Path(temp)
            prompts = root / '提示词'; prompts.mkdir()
            images = root / '参考图'; images.mkdir()
            for n in range(1, count+1):
                (prompts / f'{n:02d}_产品.txt').write_text('<Picture1> <Picture2> <Picture3> product', encoding='utf-8')
                for k in (3, 1, 2):
                    (images / f'{n}({k}).png').write_bytes(connection_probe_png())
            config = ConfigManager(root / 'config.json'); config.load_config()
            config.config['prompt_detection']['enabled'] = False
            config.config['paths'] = dict(prompts=str(prompts), images=str(images), output=str(root / '视频'))
            config.config['api'].update(base_url=server.base, api_key='local-fixture-only', upload_url=server.base+'/upload')
            config.config['workspace'].update(model='video-v3', duration=10, resolution='720p', poll_interval=3)
            config.config['model_pool'].update(enabled=True, cooldown=1,
                models=[dict(name=m, enabled=True, status='健康') for m in ('video-v2', 'video-v2-fast', 'video-v3')])
            config.config['task_strategy'].update(retry_interval=1, max_retries=5, failure_skip_threshold=100)
            if name == '提交错误故障转移':
                server.fail_models = {'video-v2'}
            elif name == '断网重试五次':
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
                config.config['api']['base_url'] = f'http://127.0.0.1:{port}'
                config.config['model_pool']['enabled'] = False
            elif name == '并发2跑5任务':
                config.config['task_strategy']['max_concurrency'] = 2
                server.processing_seconds = 1
            elif name == '冷却恢复':
                config.config['model_pool']['models'] = [dict(name='video-v3', enabled=True, status='冷却中', cooldown_until=time.time()+2)]
            config.save_config()
            window = MainWindow(config, network_time=False)
            window.setWindowTitle('StoryboardVideoStudio · 本机HTTP验收（无收费请求）')
            window.resize(1600, 1100); window.show()
            page = window.workspace_page; manager = page.task_manager
            manager.log_message.connect(lambda text, level, case=name: logs.append((case, level, text)))
            pool_states = []
            manager.pool_updated.connect(lambda snapshot: pool_states.append(copy.deepcopy(snapshot)))
            try:
                wait_until(lambda: not page.jobs.busy and len(page.data_source.matches) == count)
                assert all(len(task['images']) == 3 for task in page.data_source.matches)
                config.config['workspace']['poll_interval'] = .05
                page.start_button.click()
                if name == '并发2跑5任务':
                    wait_until(lambda: server.maximum == 2 and '正在生成2个，排队3个' == page.current_task.concurrency_label.text())
                    window.grab().save(str(evidence / 'verified-concurrency.png'))
                wait_until(lambda: not manager.is_running, timeout=45000)
                tasks = manager.tasks
                posts = [json.loads(body) for path, _, body in server.calls if path == '/videos']
                expected = 'failed' if name == '断网重试五次' else 'completed'
                assert [t['status'] for t in tasks] == [expected]*count
                assert all(len(body['images']) == 3 for body in posts)
                if name == '轮询三模型':
                    assert [t['model'] for t in tasks] == ['video-v2', 'video-v2-fast', 'video-v3']
                if name == '提交错误故障转移':
                    assert tasks[0]['model'] == 'video-v2-fast' and tasks[0]['retry_count'] == 1
                    assert any(row['status'] == '冷却中' for snap in pool_states for row in snap)
                if name == '断网重试五次':
                    assert len(tasks[0]['attempts']) == 6 and tasks[0]['retry_count'] == 5
                if name == '并发2跑5任务':
                    assert server.maximum == 2
                if name == '冷却恢复':
                    assert any('恢复可用' in text for case, _, text in logs if case == name)
                    assert window.settings_page.model_rows[0][3].text() == '● 健康'
                stored = ConfigManager(config.path).load_config()['history']
                assert {t['local_id']: (t['model'], t['status']) for t in stored} == {t['local_id']: (t['model'], t['status']) for t in tasks}
                assert window.history_page.table.rowCount() == count
                assert sorted(window.history_page.table.item(i, 3).text() for i in range(count)) == sorted(t['model'] for t in tasks)
                completed = [t for t in tasks if t['status'] == 'completed']
                assert all(Path(t['result_path']).read_bytes() == b'local-video-fixture'*10000 for t in completed)
                scenario = dict(name=name, passed=True, models=[t['model'] for t in tasks],
                    attempts=[len(t['attempts']) for t in tasks], retry_counts=[t['retry_count'] for t in tasks],
                    images_lengths=[len(body['images']) for body in posts], maximum_remote_active=server.maximum,
                    downloads_verified=len(completed))
                result['scenarios'].append(scenario)
                print(json.dumps(scenario, ensure_ascii=False), flush=True)
            finally:
                window.close(); wait_until(lambda: not window._background_busy(), timeout=10000)
                window.deleteLater(); QTest.qWait(30)
    result['ok'] = True
    (evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (evidence / 'execution.log').write_text('\n'.join(f'[{case}][{level.upper()}] {text}' for case, level, text in logs), encoding='utf-8')
    print('PASS: ' + str(evidence), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', action='store_true', help='使用Windows原生窗口完成相同本地接口验收')
    parser.parse_args()
    verify()
