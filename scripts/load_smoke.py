"""负载冒烟：批量任务全链路（本地桩服务，零费用），记录耗时与内存概要。

用法：python -X utf8 scripts/load_smoke.py [任务数，默认 300]
"""
import copy
import json
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.config_manager import DEFAULT_CONFIG  # noqa: E402
from core.task_manager import TaskManager  # noqa: E402
from test_http_clients import LocalServer  # noqa: E402


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    app = QApplication([])
    with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        prompts = root / '提示词'
        images = root / '图片'
        output = root / '输出'
        for folder in (prompts, images, output):
            folder.mkdir()
        for index in range(1, count + 1):
            (prompts / f'{index}样品.txt').write_text(f'<Picture 1> 场景{index}', encoding='utf-8')
            (images / f'{index}样品.png').write_bytes(b'img')
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['paths'] = dict(prompts=str(prompts), images=str(images), output=str(output))
        config['api'].update(base_url=server.base, api_key='***', upload_url=server.base + '/upload')
        config['workspace']['poll_interval'] = 0.02
        config['task_strategy']['max_concurrency'] = 5
        manager = TaskManager()
        tracemalloc.start()
        started = time.perf_counter()
        manager.start_tasks(config)
        deadline = time.time() + 300
        while manager.is_running and time.time() < deadline:
            QTest.qWait(10)
        elapsed = time.perf_counter() - started
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        statuses = {}
        for task in manager.tasks:
            statuses[task.get('status')] = statuses.get(task.get('status'), 0) + 1
        done = statuses.get('completed', 0)
        files = list(output.glob('*.mp4'))
        posts = sum(1 for call in server.calls if call[0] == '/videos')
        print(f'任务数={len(manager.tasks)} 完成={done} 用时={elapsed:.2f}s '
              f'吞吐={done / max(elapsed, .01):.1f} 任务/秒 峰值内存≈{peak / 1024 / 1024:.1f}MB')
        print('状态分布:', statuses)
        print(f'POST /videos 次数={posts}；输出 mp4 文件数={len(files)}')
        out_dir = ROOT.parent / '.cluster' / 'r16'
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / 'load-smoke-result.json'
        report_path.write_text(json.dumps({
            'count': len(manager.tasks), 'completed': done, 'elapsed_seconds': round(elapsed, 3),
            'throughput_per_second': round(done / max(elapsed, .01), 2),
            'peak_traced_mb': round(peak / 1024 / 1024, 1),
            'statuses': statuses, 'posts': posts, 'output_files': len(files),
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print('报告：', report_path)
        return 0 if done == count else 1


if __name__ == '__main__':
    raise SystemExit(main())
