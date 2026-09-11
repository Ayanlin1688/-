"""One authorized H3 reference diagnostic with isolated inputs and persistent redacted evidence."""
import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import signal
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PyQt5.QtCore import QCoreApplication, QTimer
from core.config_manager import ConfigManager
from core.reference_diagnostics import image_metadata
from core.task_manager import TaskManager
from core.matcher import StoryboardMatcher

OUT = ROOT / 'artifacts' / 'reference-debug'
PROMPT = Path('D:/分镜提示词目录/玫瑰毯子/玫瑰毯子1.txt')
IMAGES = [Path(f'D:/参考图目录/玫瑰毯子/1 ({index}).jpg') for index in (1, 2, 3)]


def save_json(name, data):
    destination = OUT / name
    temporary = destination.with_suffix(destination.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(destination)


def main():
    global OUT
    parser = argparse.ArgumentParser(description='玫瑰毯子三张JPG，MiniMax-H3，一次真实提交；不修改日常配置。')
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--auto-match', action='store_true', help='扫描独立的三张JPG素材目录，无手动绑定；保存新的自动匹配验证记录')
    args = parser.parse_args()
    if args.auto_match:
        OUT = ROOT / 'artifacts' / 'reference-automatch'
    config = ConfigManager().load_config()
    if not PROMPT.is_file() or not all(path.is_file() for path in IMAGES):
        raise SystemExit('真实素材不完整，没有上传或提交。')
    if not config['api']['api_key'].strip():
        raise SystemExit('缺少本地 API Key，没有上传或提交。')
    manifest = dict(prompt_path=str(PROMPT.resolve()), model='MiniMax-H3', images=[image_metadata(path) for path in IMAGES])
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    previous = []
    if (OUT / 'run.json').is_file():
        report = json.loads((OUT / 'run.json').read_text(encoding='utf-8'))
        previous = report.get('tasks', [])
        if args.submit and any(task.get('task_id') for task in previous):
            raise SystemExit('本次三图诊断已有远端 task_id，保留证据，不重新付费提交。')
    # Only the confirmed first rose prompt is scanned; other rose/fan prompts stay out of this run.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='storyboard-reference-run-') as directory:
        prompt_dir = Path(directory) / 'prompts'; prompt_dir.mkdir()
        isolated_prompt = prompt_dir / PROMPT.name
        isolated_prompt.write_bytes(PROMPT.read_bytes())
        config = copy.deepcopy(config)
        config['paths'] = dict(prompts=str(prompt_dir), images=str(IMAGES[0].parent), output='D:/视频保存目录/玫瑰毯子调试')
        if args.auto_match:
            # The user selected these three JPG inputs, excluding the two PNG
            # alternatives. Stage unchanged copies; only the normal scanner and
            # matcher decide their binding and order. No override is supplied.
            image_dir = Path(directory) / 'images'; image_dir.mkdir()
            for source in IMAGES:
                shutil.copy2(source, image_dir / source.name)
            config['paths'].update(images=str(image_dir), output='D:/视频保存目录/玫瑰毯子自动匹配')
            config['match_overrides'] = {}
        else:
            config['match_overrides'] = {str(isolated_prompt.resolve()): [str(path.resolve()) for path in IMAGES]}
        config['workspace'].update(model='MiniMax-H3', aspect_ratio='9:16', resolution='1080p', duration=15)
        config['diagnostics'] = dict(debug_mode=True)
        config['download_settings']['naming_rule'] = ('自动匹配' if args.auto_match else '三图调试') + '_{提示词名}_{task_id}.mp4'
        config['_task_limit'] = 1
        matches = StoryboardMatcher.from_config(config).scan_and_match(config['paths'])
        if len(matches) != 1 or [Path(path).name for path in matches[0]['images']] != [path.name for path in IMAGES]:
            raise SystemExit('实际扫描未得到按1→2→3排序的三张JPG，停止验证，没有提交。')
        if args.auto_match and (config['match_overrides'] or matches[0]['match_method'] == '手动绑定'):
            raise SystemExit('验证必须使用正常自动匹配，没有提交。')
        manifest.update(matching_mode='automatic' if args.auto_match else 'manual', binding_check=matches[0])
        print('扫描验证: ' + json.dumps(matches[0], ensure_ascii=False), flush=True)
        if not args.submit:
            print('素材及匹配检查通过；未上传或提交。真实提交需 --submit。'); return 0
        OUT.mkdir(parents=True, exist_ok=True)
        save_json('inputs.json', manifest)
        app = QCoreApplication([])
        manager = TaskManager()
        code = [1]
        events = []
        with (OUT / 'execution.log').open('w', encoding='utf-8', buffering=1) as stream:
            def log(message, level):
                # Worker/client already redact both configured credentials. This
                # final boundary also protects exported artifacts and console output.
                for key in ('api_key', 'upload_api_key'):
                    secret = config['api'].get(key, '').strip()
                    if secret:
                        message = message.replace(secret, '[REDACTED]')
                timestamp = datetime.now().isoformat(timespec='seconds')
                events.append(dict(time=timestamp, level=level, message=message))
                stream.write(f'[{timestamp}] [{level.upper()}] {message}\n')
                save_json('events.json', events)
                print(f'[{level.upper()}] {message}', flush=True)
                if level == 'debug' and message.startswith('请求体: '):
                    save_json('request.json', json.loads(message.split(': ', 1)[1]))
            def checkpoint(*_):
                save_json('run.json', dict(updated_at=datetime.now().isoformat(timespec='seconds'),
                    running=manager.is_running, source_prompt=str(PROMPT.resolve()),
                    source_images=[str(path.resolve()) for path in IMAGES],
                    matching_mode=manifest['matching_mode'], tasks=manager.tasks))
            def done(success, failed):
                code[0] = 0 if success == 1 and failed == 0 else 1
                checkpoint()
                log(f'三图调试结束：成功{success}，失败{failed}；报告={OUT}', 'info')
                app.quit()
            manager.log_message.connect(log)
            manager.current_task_changed.connect(checkpoint)
            manager.record_updated.connect(checkpoint)
            manager.all_finished.connect(done)
            signal.signal(signal.SIGINT, lambda *_: manager.cancel_all())
            heartbeat = QTimer(); heartbeat.timeout.connect(lambda: None); heartbeat.start(100)
            log('本次验证使用正常扫描自动匹配，无手动绑定' if args.auto_match else '本次验证使用已确认的手动绑定', 'info')
            for index, source in enumerate(IMAGES, 1):
                log(f'验证图{index}原始来源: {source.resolve()}；上传未修改的文件副本', 'debug')
            manager.start_tasks(config)
            app.exec_()
        return code[0]


if __name__ == '__main__':
    sys.exit(main())
