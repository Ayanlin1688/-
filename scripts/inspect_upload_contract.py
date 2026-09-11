"""Read public provider frontend code to identify its documented request flow."""
import re
import requests

BASE = 'https://video.kkone.vip'


def main():
    with requests.Session() as session:
        page = session.get(BASE, timeout=(15, 60)); page.raise_for_status()
        assets = re.findall(r'src="([^"]+\.js)"', page.text)
        for asset in assets:
            response = session.get(BASE + asset, timeout=(15, 60)); response.raise_for_status()
            code = response.text
            print('Public asset:', asset)
            for term in ('upload_token', 'X-Upload-Token', 'x-upload-token', 'Bearer', 'Authorization', 'upload-admin', 'Token 鉴权', 'headers.set'):
                matches = list(re.finditer(re.escape(term), code))
                print('TERM', term, 'COUNT', len(matches))
                for match in matches[:3]:
                    print(code[max(0, match.start()-250):match.end()+400])


if __name__ == '__main__':
    main()
