"""Opt-in one-task provider smoke test. Reads keys locally and never prints them."""
import argparse
import json
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PyQt5.QtCore import QCoreApplication, QTimer
from core.config_manager import ConfigManager
from core.model_catalog import ModelCatalog
from core.task_manager import TaskManager
from ui.model_catalog_controller import runtime_config


def main():
    parser = argparse.ArgumentParser(description='使用本地配置提交最多一个真实视频任务（可能计费）。')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--submit', action='store_true', help='执行最多一个真实任务；默认仅检查配置')
    args = parser.parse_args()
    config_manager = ConfigManager(args.config)
    config = config_manager.load_config()
    missing = []
    if not config['api']['api_key'].strip():
        missing.append('设置 → API Key')
    if not config['paths']['prompts'] or not Path(config['paths']['prompts']).is_dir():
        missing.append('存在的提示词目录')
    if not config['paths']['output']:
        missing.append('视频保存目录')
    if config['paths']['images'] and not Path(config['paths']['images']).is_dir():
        missing.append('存在的图片目录')
    if missing:
        print('BLOCKED: 缺少 ' + '、'.join(missing) + '。请在本地应用设置中填写；没有提交请求。')
        return 2
    if not args.submit:
        print('配置前置检查通过。执行真实测试：python scripts\\run_live_smoke.py --submit（最多一个任务，可能计费）。')
        return 0
    # Share the GUI's pre-POST ledger even when the material directory lives on
    # a different drive. Cached model capabilities also match desktop behavior.
    config = runtime_config(config_manager)
    config['_model_catalog'] = ModelCatalog(config_manager.path, config['api']['base_url'],
                                             config['api']['api_key']).snapshot()
    config['_task_limit'] = 1
    # A noninteractive smoke test cannot wait for a UI resume decision.
    if config['task_strategy']['unmatched_prompt'] == '暂停任务':
        config['task_strategy']['unmatched_prompt'] = '跳过并警告'
    app = QCoreApplication([])
    manager = TaskManager()
    code = [1]
    manager.log_message.connect(lambda message, level: print(f'[{level.upper()}] {message}', flush=True))
    def persist(record):
        records = config_manager.config['history']
        index = next((i for i, item in enumerate(records) if item.get('local_id') == record['local_id']), None)
        if index is None:
            records.append(record)
        else:
            records[index] = record
        try:
            config_manager.update(('history',), records)
        except Exception as error:
            print(f'ERROR: 无法保存历史：{error}', flush=True)
    def done(success, failed):
        code[0] = 0 if success == 1 and failed == 0 else 1
        report = dict(success=success, failed=failed, tasks=manager.tasks)
        # No keys or raw configuration are included in the report.
        path = config_manager.path.with_name('live-smoke-result.json')
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'验证结果：{path}', flush=True)
        app.quit()
    manager.record_updated.connect(persist)
    manager.all_finished.connect(done)
    signal.signal(signal.SIGINT, lambda *_: manager.cancel_all())
    heartbeat = QTimer(); heartbeat.timeout.connect(lambda: None); heartbeat.start(100)
    manager.start_tasks(config)
    app.exec_()
    return code[0]


if __name__ == '__main__':
    sys.exit(main())
