"""Bounded HTTPS time calibration for the application's clock, not the OS clock."""
from datetime import datetime
from email.utils import parsedate_to_datetime
import time
import requests
from urllib.request import getproxies


class NetworkClock:
    def __init__(self):
        self.offset = 0.0
        self.status = '系统时间（正在网络校时）'

    def now(self):
        return datetime.fromtimestamp(time.time() + self.offset)

    def sample(self):
        errors = []
        with requests.Session() as session:
            # Use the user's proxy for reachability, without implicit netrc credentials.
            session.trust_env = False
            session.proxies.update(getproxies())
            for url in ('https://www.cloudflare.com/cdn-cgi/trace', 'https://www.microsoft.com/'):
                try:
                    started = time.time()
                    with session.head(url, params={'clock_check': str(int(started))}, headers={'Cache-Control': 'no-cache'},
                                      timeout=(5, 8), allow_redirects=True) as response:
                        response.raise_for_status()
                        ended = time.time()
                        if ended - started > 5 or float(response.headers.get('Age', '0')) > 2:
                            raise ValueError('网络时间响应延迟或缓存过大')
                        stamp = parsedate_to_datetime(response.headers['Date'])
                        if stamp.tzinfo is None:
                            raise ValueError('网络时间缺少时区')
                        return dict(offset=stamp.timestamp() + .5 - (started + ended) / 2, source=response.url.split('/')[2])
                except (requests.RequestException, ValueError, KeyError, TypeError) as error:
                    errors.append(str(error))
        raise RuntimeError('网络校时未成功，使用系统时间；' + '; '.join(errors))

    def apply(self, sample):
        self.offset = float(sample['offset'])
        self.status = f'网络已校时 · {sample["source"]} · 校正{self.offset:+.1f}秒'

    def use_system_time(self):
        self.offset = 0.0
        self.status = '网络不可用，使用系统时间'
