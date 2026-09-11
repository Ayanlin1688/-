"""Read-only verification of the one authorized live run; no network or submission."""
import json
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.config_manager import ConfigManager
from core.model_parameters import model_prompt
from core.prompt_processor import process_prompt
from core.reference_diagnostics import image_metadata


def main():
    parser = argparse.ArgumentParser(description='只读取真实日志和请求体，不联网、不提交。')
    parser.add_argument('--auto-match', action='store_true')
    args = parser.parse_args()
    folder = ROOT / 'artifacts' / ('reference-automatch' if args.auto_match else 'reference-debug')
    read = lambda name: json.loads((folder / name).read_text(encoding='utf-8'))
    inputs, request, report, events = (read(name) for name in ('inputs.json', 'request.json', 'run.json', 'events.json'))
    task = report['tasks'][0]
    debug = [event['message'] for event in events if event['level'] == 'debug']
    original = Path(inputs['prompt_path']).read_text(encoding='utf-8-sig')
    expected = [str(Path(f'D:/参考图目录/玫瑰毯子/1 ({i}).jpg').resolve()) for i in (1, 2, 3)]
    assert [info['path'] for info in inputs['images']] == expected, '源图片顺序不一致'
    if args.auto_match:
        assert inputs['matching_mode'] == report['matching_mode'] == 'automatic', '未使用自动匹配'
        assert '序号匹配' in task['match_method'], '绑定来源不是序号匹配'
        assert [Path(path).name for path in task['images']] == ['1 (1).jpg', '1 (2).jpg', '1 (3).jpg'], '自动绑定顺序不一致'
        assert task['images'] == inputs['binding_check']['images'], '提交未沿用自动扫描结果'
        assert any('上传图片3张' in event['message'] for event in events), '缺少三图上传日志'
    else:
        assert task['images'] == expected, '绑定顺序不一致'
    assert task['submitted_image_count'] == len(request['images']) == 3, '提交数量不为3'
    assert request['model'] == 'MiniMax-H3' and request['size'] == '1088x1920', 'H3参数不一致'
    assert request['workflow_id'] == 'multi-reference' and request['seconds'] == 15, '生成模式/时长不一致'
    assert request['prompt'] == model_prompt('MiniMax-H3', process_prompt(original)), '实际提示词不一致'
    assert '提示词替换前: ' + original in debug, '缺少完整原始提示词'
    assert '提示词替换后: ' + process_prompt(original) in debug, '缺少完整替换后提示词'
    assert json.loads(next(message.split(': ', 1)[1] for message in debug if message.startswith('实际提交images: '))) == request['images'], '日志images不一致'
    assert json.loads(next(message.split(': ', 1)[1] for message in debug if message.startswith('请求体: '))) == request, '请求体证据不一致'
    for i, (info, url) in enumerate(zip(inputs['images'], request['images']), 1):
        assert image_metadata(info['path'])['sha256'] == info['sha256'], '源图片已变化'
        assert any(message.startswith(f'  图{i}: {task["images"][i-1]} (') and message.endswith(' -> ' + url) for message in debug), '图片URL次序不一致'
        assert any(f'图{i}原图/上传后 SHA256一致：{info["sha256"]}' in message for message in debug), '缺少原图一致性证据'
    config = ConfigManager().load_config()
    secrets = [config['api'].get(key, '').strip() for key in ('api_key', 'upload_api_key')]
    for path in folder.iterdir():
        if path.suffix not in {'.json', '.log', '.md', '.txt'}:
            continue
        content = path.read_text(encoding='utf-8')
        for secret in filter(None, secrets):
            forms = {secret, json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret, ensure_ascii=True)[1:-1]}
            if any(form in content for form in forms):
                raise RuntimeError('FAIL: 诊断产物检测到未脱敏凭据，停止交付')
    print(json.dumps(dict(result='PASS', references=3, byte_identical_images=3, prompt_and_payload='match',
                         credential_scan='PASS', task_id=task['task_id'], status=task['status'],
                         running=report['running'], updated_at=report['updated_at'],
                         matching_method=task['match_method'],
                         current_workspace_model=config['workspace']['model'],
                         current_workspace_images=config['paths']['images']), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
