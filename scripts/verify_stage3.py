"""Real Qt timer + local HTTP product acceptance, without provider charges or user config."""
import argparse
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from PyQt5.QtCore import QTime
from PyQt5.QtGui import QImage, QColor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from test_http_clients import LocalServer
from test_reference_debug import ReferenceHandler
from test_task_manager import wait_until


def verify(wait_minute=False):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    evidence = ROOT / 'artifacts' / 'stage3'
    evidence.mkdir(parents=True, exist_ok=True)
    result = {'provider_requests': 0, 'kind': 'local HTTP fixture / real Qt timer'}
    logs = []
    with tempfile.TemporaryDirectory(prefix='storyboard-stage3-') as temp, LocalServer() as server:
        server.RequestHandlerClass = ReferenceHandler
        server.uploads = []; server.reference_headers = []
        server.changed_image = None; server.reference_failure = False
        root = Path(temp)
        products = [('玫瑰毯子', ['01_展示', '02_细节']), ('车载风扇', ['01_展示'])]
        expected_images = []
        for product, names in products:
            prompts, images = root / '提示词' / product, root / '参考图' / product
            prompts.mkdir(parents=True); images.mkdir(parents=True)
            for n, name in enumerate(names, 1):
                (prompts / (name + '.txt')).write_text(product + ' <Picture1> <Picture 2 > @Image3', encoding='utf-8')
                for number in (3, 1, 2):
                    image = QImage(80 + number, 120 + number, QImage.Format_RGB32)
                    image.fill(QColor(number * 50, n * 50, len(product) * 30))
                    assert image.save(str(images / f'{n}({number}).jpg'), 'JPG')
                expected_images.extend((images / f'{n}({number}).jpg').read_bytes() for number in (1, 2, 3))
        config = ConfigManager(root / 'config.json'); config.load_config()
        config.config['paths'] = dict(prompts=str(root / '提示词'), images=str(root / '参考图'), output=str(root / '视频'))
        config.config['api'].update(base_url=server.base, api_key='local-fixture-only', upload_url=server.base + '/upload', upload_api_key='')
        config.config['workspace'].update(model='MiniMax-H3', aspect_ratio='9:16', resolution='1080p', duration=8, poll_interval=3)
        config.config['diagnostics']['debug_mode'] = True
        config.save_config()
        window = MainWindow(config, network_time=False)
        window.show()
        workspace = window.workspace_page
        workspace.task_manager.log_message.connect(lambda text, level: logs.append((level, text)))
        window.schedule_engine.log = lambda text, level: (workspace.append_log(text, level), logs.append((level, text)))
        try:
            wait_until(lambda: not workspace.jobs.busy and len(workspace.data_source.matches) == 3)
            assert not config.config['match_overrides']
            assert [len(t['images']) for t in workspace.data_source.matches] == [3, 3, 3]
            assert '已识别 2 个产品，共 3 个提示词' in workspace.data_source.status_label.text()
            started = time.monotonic()
            if wait_minute:
                # Pick a real minute boundary at least 60 seconds away, using the UI.
                due = (window.clock.now() + timedelta(minutes=2)).replace(second=0, microsecond=0)
                selected = QTime(due.hour, due.minute)
                window.settings_page.schedule_time.setTime(selected)
                window.settings_page.schedule_time.timeChanged.emit(selected)
                window.settings_page.schedule_enabled.switchButton.setChecked(True)
                assert window.schedule_engine.next_run == due
                window.showMinimized()
                print(f'真实定时验收：最小化窗口，等待 {(due-window.clock.now()).total_seconds():.1f} 秒，到 {due:%H:%M} 自动开始', flush=True)
                deadline = started + 140
                while not workspace.task_manager.tasks:
                    if time.monotonic() > deadline:
                        raise AssertionError('定时未在预期时间触发')
                    QTest.qWait(100)
                result['wait_seconds'] = round(time.monotonic() - started, 2)
                result['minimized_at_trigger'] = window.isMinimized()
                assert result['wait_seconds'] >= 59
                assert window.isMinimized()
                assert not window.settings_page.schedule_enabled.isChecked()
            else:
                workspace.start_button.click()
            wait_until(lambda: not workspace.task_manager.is_running, timeout=30000)
            tasks = workspace.task_manager.tasks
            assert [(t['product'], t['prompt_name']) for t in tasks] == [(p, n) for p, names in products for n in names]
            assert [t['status'] for t in tasks] == ['completed'] * 3
            assert [t['submitted_image_count'] for t in tasks] == [3] * 3
            assert server.uploads == expected_images
            requests = [json.loads(body) for path, _, body in server.calls if path == '/videos']
            assert len(requests) == 3
            assert [len(payload['images']) for payload in requests] == [3] * 3
            for index, payload in enumerate(requests):
                assert payload['images'] == [server.base + f'/reference/{index*3+i}' for i in (1, 2, 3)]
            assert sum('上传图片3张' in text for _, text in logs) == 3
            outputs = []
            for task in tasks:
                path = Path(task['result_path'])
                assert path.parent.name == task['product']
                assert path.read_bytes() == b'local-video-fixture' * 10000
                outputs.append(dict(product=task['product'], file=path.name, bytes=path.stat().st_size,
                                    sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            assert workspace.queue_panel.product_progress_label.text() == '已完成产品 2/2'
            assert window.history_page.table.columnCount() >= 8
            assert {window.history_page.table.item(i, 1).text() for i in range(3)} == {'玫瑰毯子', '车载风扇'}
            result.update(ok=True, products=2, tasks=3, upload_count=len(server.uploads), images_lengths=[len(p['images']) for p in requests], outputs=outputs)
            window.showNormal(); app.processEvents()
            window.resize(1600, 1100); QTest.qWait(200)
            window.grab().save(str(evidence / 'verified-products.png'))
            (evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            (evidence / 'execution.log').write_text('\n'.join(f'[{level.upper()}] {text}' for level, text in logs), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        finally:
            window.close()
            wait_until(lambda: not window._background_busy())
            window.deleteLater(); QTest.qWait(50)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--wait-minute', action='store_true', help='实际等待60–120秒，在最小化状态到分钟时刻启动')
    verify(parser.parse_args().wait_minute)
