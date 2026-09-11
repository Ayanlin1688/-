"""Compare explicitly authorized credential transports without printing any key.

Uses the first matched reference image. Stops at the first successful upload and
never creates a video task. Does not access upload-admin endpoints or forge sessions.
"""
import html
import json
import mimetypes
from pathlib import Path
import re
import sys
import requests
import argparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.config_manager import ConfigManager
from core.matcher import StoryboardMatcher
from core.image_uploader import upload_credentials
from core.http_client import extract, objects


def main():
    parser = argparse.ArgumentParser(description='只使用现有配置密钥对比上传鉴权，不创建视频任务')
    parser.add_argument('--refresh-docs', action='store_true', help='仅刷新现有报告的文档与公开 OPTIONS 信息')
    args = parser.parse_args()
    if args.refresh_docs:
        path = ROOT / 'upload-auth-diagnostics.json'
        report = json.loads(path.read_text(encoding='utf-8'))
        with requests.Session() as session:
            docs = session.get(report['documentation'], timeout=(15, 60)); docs.raise_for_status()
            section = re.search(r'<section id="media".*?</section>', docs.content.decode('utf-8'), re.S)
            report['documentation_media_text'] = ' '.join(html.unescape(re.sub('<[^>]+>', ' ', section.group())).split())
            option = session.options(report['endpoint'], timeout=(15, 60))
            report['options'] = dict(status_code=option.status_code, allowed_headers=option.headers.get('Access-Control-Allow-Headers'))
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Refreshed documentation and public OPTIONS evidence:', path)
        return 0
    config = ConfigManager().load_config()
    endpoint, key = upload_credentials(config['api'])
    if not key:
        print('No configured credential; nothing sent.')
        return 2
    matches = StoryboardMatcher.from_config(config).scan_and_match(config['paths'])
    image = next((Path(t['images'][0]) for t in matches if t['images']), None)
    if image is None or not image.is_file():
        print('No matched reference image; nothing sent.')
        return 2
    def redact(value):
        value = str(value)
        for secret in (key, config['api']['api_key'], config['api']['upload_api_key']):
            if secret:
                value = value.replace(secret, '[REDACTED]')
        return value
    variants = [
        ('documented-no-auth', {}, {}),
        ('bearer-header', {'Authorization': 'Bearer ' + key}, {}),
        ('form-token', {}, {'token': key}),
        ('bearer-and-form-token', {'Authorization': 'Bearer ' + key}, {'token': key}),
        ('x-upload-token', {'X-Upload-Token': key}, {}),
        ('x-api-key', {'X-API-Key': key}, {}),
        ('upload-token-header', {'Upload-Token': key}, {}),
        ('form-upload-token', {}, {'upload_token': key}),
        ('authorization-token', {'Authorization': 'Token ' + key}, {}),
    ]
    report = dict(documentation='https://image.kkone.vip/1/docs.html', endpoint=endpoint,
                  image_name=image.name, video_probe=None, attempts=[])
    with requests.Session() as session:
        docs = session.get(report['documentation'], timeout=(15, 60)); docs.raise_for_status()
        section = re.search(r'<section id="media".*?</section>', docs.content.decode('utf-8'), re.S)
        if section:
            report['documentation_media_text'] = html.unescape(re.sub('<[^>]+>', ' ', section.group()))
            print('DOCUMENTED UPLOAD SECTION:', ' '.join(report['documentation_media_text'].split()), flush=True)
        with session.get(config['api']['base_url'].strip().rstrip('/') + '/models',
                         headers={'Authorization': 'Bearer ' + config['api']['api_key'].strip(), 'Accept': 'application/json'}, timeout=(15, 60)) as response:
            report['video_probe'] = dict(status_code=response.status_code, ok=response.ok)
            print('VIDEO API HTTP', response.status_code, flush=True)
        for name, headers, data in variants:
            headers = {'Accept': 'application/json', **headers}
            entry = dict(method=name, header_names=list(headers), form_fields=['files', *data])
            try:
                with image.open('rb') as stream:
                    with session.post(endpoint, headers=headers, data=data,
                                      files={'files': (image.name, stream, mimetypes.guess_type(image.name)[0] or 'image/jpeg')},
                                      timeout=(15, 60)) as response:
                        entry.update(status_code=response.status_code, body=redact(response.text)[:1500],
                                     redirects=[dict(status=r.status_code, url=r.url) for r in response.history])
                        url = None
                        if response.ok:
                            payload = response.json()
                            if isinstance(payload, dict):
                                for obj in objects(payload):
                                    files = obj.get('files')
                                    if isinstance(files, list) and files and isinstance(files[0], dict):
                                        url = files[0].get('url')
                                        if url:
                                            break
                                url = url or extract(payload, ('url',))
                        entry['upload_succeeded'] = bool(url)
                        if url:
                            entry['image_url'] = redact(url)
                        print(name, 'HTTP', response.status_code, entry['body'], flush=True)
            except Exception as error:
                entry.update(error=redact(error), upload_succeeded=False)
                print(name, entry['error'], flush=True)
            report['attempts'].append(entry)
            if entry.get('upload_succeeded'):
                break
    destination = ROOT / 'upload-auth-diagnostics.json'
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Report:', destination, flush=True)
    return 0 if any(item.get('upload_succeeded') for item in report['attempts']) else 1


if __name__ == '__main__':
    sys.exit(main())
