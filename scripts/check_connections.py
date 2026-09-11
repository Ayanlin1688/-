"""Check configured video and upload endpoints independently, without video submission."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config_manager import ConfigManager
from core.api_client import ApiClient
from core.image_uploader import ImageUploader


def main():
    config = ConfigManager().load_config()
    def log(message, level):
        print(f'[{level.upper()}] {message}', flush=True)
    client = ApiClient(config['api']['base_url'], config['api']['api_key'], log=log)
    uploader = ImageUploader.from_config(config, log=log)
    try:
        result = client.test_connection(uploader)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 1
    finally:
        client.close(); uploader.close()


if __name__ == '__main__':
    sys.exit(main())
