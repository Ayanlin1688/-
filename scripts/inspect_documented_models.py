"""Read public API documentation without application credentials or API calls."""
from html.parser import HTMLParser
import re
import requests


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, value):
        self.parts.append(value)


def main():
    with requests.Session() as session:
        session.trust_env = False
        response = session.get('https://image.kkone.vip/1/docs.html', timeout=(15, 60))
        response.raise_for_status()
        response.encoding = 'utf-8'
        parser = TextParser(); parser.feed(response.text)
    lines = [line.strip() for line in '\n'.join(parser.parts).splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if re.search(r'video-v[123]|seedance|sd-?2\.5|MiniMax-H3|grok-imagine', line, re.I):
            print(' | '.join(lines[max(0, index-1):index+2]))


if __name__ == '__main__':
    main()
