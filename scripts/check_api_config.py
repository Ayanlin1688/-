"""Report only presence of credentials and input paths, never credential contents."""
import json
import argparse
from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from core.config_manager import ConfigManager

parser = argparse.ArgumentParser(description='仅显示配置是否齐全，不输出密钥。')
parser.add_argument('--migrate', action='store_true', help='保留现有配置，补齐阶段2A字段并原子保存')
args = parser.parse_args()
manager = ConfigManager()
# Keep the default presence check read-only; load_config can run migrations.
cfg = json.loads(manager.path.read_text(encoding='utf-8'))
if args.migrate:
    if not isinstance(cfg, dict):
        raise ValueError('配置必须为 JSON 对象，已停止迁移')
    cfg = manager.load_config()
    manager.save_config()
    print('Stage 2A configuration schema saved; existing preferences preserved.')
print("api_key_configured:", bool(cfg.get("api", {}).get("api_key", "").strip()))
print("upload_key_configured:", bool(cfg.get("api", {}).get("upload_api_key", "").strip()))
for name, path in cfg.get("paths", {}).items():
    print(name + "_directory_configured:", bool(path), "exists:", bool(path) and Path(path).is_dir())
