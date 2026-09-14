"""Stage4 local HTTP/Qt acceptance; optional read-only upstream model discovery."""
import argparse
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
    parser.add_argument('--upstream', action='store_true', help='GET configured provider models only; never create a video')
    args = parser.parse_args()
    os.environ['QT_QPA_PLATFORM'] = 'windows' if args.native and sys.platform == 'win32' else 'offscreen'
    from PyQt5.QtWidgets import QApplication
    from core.api_client import ApiClient
    from core.config_manager import ConfigManager
    from core.image_uploader import connection_probe_png
    from core.model_catalog import ModelCatalog
    from ui.main_window import MainWindow
    from test_pool_execution import PoolServer
    from test_task_manager import wait_until

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    evidence = ROOT / 'artifacts' / 'stage4'
    evidence.mkdir(parents=True, exist_ok=True)
    logs = []
    report = dict(platform=app.platformName(), paid_provider_submissions=0)
    with tempfile.TemporaryDirectory(prefix='storyboard-stage4-') as directory, PoolServer() as server:
        root = Path(directory).resolve()
        prompts, images = root/'prompts', root/'images'
        prompts.mkdir(); images.mkdir()
        texts = ['subject_definitions: blanket\n[Shot 1] <Picture 1> <Picture 2> <Picture 3>',
                 '镜头1：展示@图1。镜头2：参考@图2和@图3。', '自然光照亮桌上的玫瑰毯子。']
        for number, text in enumerate(texts, 1):
            (prompts/f'{number}.txt').write_text(text, encoding='utf-8')
            for picture in (3, 1, 2):
                (images/f'{number}({picture}).png').write_bytes(connection_probe_png())
        config = ConfigManager(root/'config.json')
        config.config['paths'].update(prompts=str(prompts), images=str(images), output=str(root/'videos'))
        config.config['api'].update(base_url=server.base, api_key='fixture-only', upload_url=server.base+'/upload')
        config.config['workspace'].update(model='video-v3', resolution='720p', duration=8, aspect_ratio='9:16')
        config.config['diagnostics']['debug_mode'] = True
        config.save_config()
        window = MainWindow(config, network_time=False)
        window.setWindowTitle('Yanlin Smart-Creation Matrix · 本地 HTTP 验证（非付费生成）')
        window.show()
        page = window.workspace_page
        page.task_manager.log_message.connect(lambda text, level: logs.append((text, level)))
        try:
            wait_until(lambda: len(page.queue_panel._tasks) == 3)
            assert [task['requested_model'] for task in page.queue_panel._tasks] == ['MiniMax-H3', 'video-v2', 'video-v3']
            assert all(len(task['images']) == 3 for task in page.queue_panel._tasks)
            config.config['workspace']['poll_interval'] = .02
            page.start_button.click()
            wait_until(lambda: not page.task_manager.is_running, timeout=20000)
            tasks = page.task_manager.tasks
            bodies = [json.loads(body) for path, _, body in server.calls if path == '/videos']
            assert [body['model'] for body in bodies] == ['MiniMax-H3', 'video-v2', 'video-v3']
            assert [task['status'] for task in tasks] == ['completed']*3
            assert all(len(body['images']) == 3 for body in bodies)
            assert bodies[0]['size'] == '1088x1920'
            assert bodies[1]['prompt'] == '镜头1：展示@Image1。镜头2：参考@Image2和@Image3。'
            assert all(Path(task['result_path']).read_bytes() == b'local-video-fixture'*10000 for task in tasks)
            for number, model in enumerate(('MiniMax-H3', 'video-v2', 'video-v3'), 1):
                assert any(f'任务{number}使用模型 {model}（自动识别）' in text for text, _ in logs)
            report['local'] = dict(ok=True, models=[task['model'] for task in tasks],
                                   reference_counts=[len(body['images']) for body in bodies], downloads=3)
        finally:
            page.task_manager.cancel_all()
            window.close()
            wait_until(lambda: not window._background_busy(), timeout=10000)
            window.deleteLater()
            app.processEvents()
    if args.upstream:
        production = ConfigManager()
        production.load_config()
        api = production.config['api']
        if not api['api_key'].strip():
            raise SystemExit('No configured API Key; upstream verification not run')
        catalog = ModelCatalog(production.path, api['base_url'], api['api_key'])
        client = ApiClient(api['base_url'], api['api_key'], log=lambda *_: None)
        try:
            snapshot = catalog.refresh(client)
        finally:
            client.close()
        report['upstream'] = dict(ok=not bool(catalog.last_error), total=len(snapshot),
            video=sum(row['kind'] == 'video' for row in snapshot.values()),
            image=sum(row['kind'] == 'image' for row in snapshot.values()),
            video_ids=[name for name, row in snapshot.items() if row['kind'] == 'video'],
            error=catalog.last_error, fetched_at=catalog.fetched_at)
    (evidence/'execution.log').write_text('\n'.join(f'[{level.upper()}] {text}' for text, level in logs), encoding='utf-8')
    (evidence/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report.get('upstream', {}).get('ok') is False else 0


if __name__ == '__main__':
    raise SystemExit(main())
