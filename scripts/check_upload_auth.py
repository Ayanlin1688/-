"""Non-file upload auth probe: no generation request, no credential output."""
from pathlib import Path
import argparse
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config_manager import ConfigManager
from core.image_uploader import ImageUploader, upload_credentials


def main():
    parser = argparse.ArgumentParser(description='只检查上传鉴权，不上传文件或创建视频任务')
    parser.add_argument('--token-header', action='store_true', help='验证上传 Token 专用请求头')
    args = parser.parse_args()
    config = ConfigManager().load_config()
    client = ImageUploader(*upload_credentials(config['api']), log=lambda *args: None)
    try:
        print('Upload endpoint:', client.upload_url)
        print('Bearer credential configured:', bool(client.api_key))
        print('Separate upload credential configured:', bool(config['api']['upload_api_key'].strip()))
        headers = {'X-Upload-Token': client.api_key} if args.token_header else {}
        print('Dedicated upload token header:', args.token_header)
        with client.request('POST', client.upload_url, headers=headers, check_status=False, files={'probe': (None, 'connection-check')}) as response:
            print('HTTP', response.status_code, client.redact(response.text)[:1000])
    finally:
        client.close()


if __name__ == '__main__':
    main()
