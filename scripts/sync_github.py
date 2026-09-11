"""Commit verified changes and push to the project's authorized private repository."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.config_manager import ConfigManager
from core.repository_sync import RepositorySync, GitSyncError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--message', default='refactor: 同步软件代码修改')
    parser.add_argument('--message-file', type=Path)
    args = parser.parse_args()
    message = args.message_file.read_text(encoding='utf-8').strip() if args.message_file else args.message
    config = ConfigManager().load_config()
    secrets = [config['api'].get(key, '') for key in ('api_key', 'upload_api_key')]
    try:
        RepositorySync(ROOT, secrets, log=lambda text, level: print(f'[{level.upper()}] {text}', flush=True)).sync(message)
    except GitSyncError as error:
        print('ERROR: ' + str(error)); return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
