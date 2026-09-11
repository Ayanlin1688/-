"""Read only: inspect configured bindings and image metadata without printing credentials."""
import hashlib
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PyQt5.QtGui import QImageReader
from core.config_manager import ConfigManager
from core.matcher import StoryboardMatcher
from core.prompt_processor import reference_warnings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prompts')
    parser.add_argument('--images')
    args = parser.parse_args()
    config = ConfigManager().load_config()
    if args.prompts:
        config['paths']['prompts'] = args.prompts
    if args.images:
        config['paths']['images'] = args.images
    matcher = StoryboardMatcher.from_config(config)
    prompts, images = matcher.scan_directories(config['paths']['prompts'], config['paths']['images'])
    print('Current model:', config['workspace']['model'])
    for path in images:
        reader = QImageReader(path); size = reader.size()
        data = Path(path).read_bytes()
        print(json.dumps(dict(path=path, width=size.width(), height=size.height(), format=bytes(reader.format()).decode(),
                              bytes=len(data), sha256=hashlib.sha256(data).hexdigest()), ensure_ascii=False))
    for task in matcher.match_files(prompts, images):
        prompt = Path(task['prompt_path']).read_text(encoding='utf-8-sig')
        print(json.dumps(dict(prompt_file=task['prompt_path'], binding='manual' if task['prompt_path'] in matcher.overrides else 'automatic',
                              images=task['images'], markers=re.findall(r'<[^>]*(?:Picture|图片)[^>]*>|@(?:Image|参考图|图片)\s*\d+', prompt, re.I),
                              warnings=reference_warnings(prompt, len(task['images']))), ensure_ascii=False))


if __name__ == '__main__':
    main()
