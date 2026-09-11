"""Shared bounded requests and credential-redacted error reporting."""
import logging
import requests

TIMEOUT = (10, 30)


class RequestError(RuntimeError):
    def __init__(self, message, status_code=None, body=''):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class Cancelled(Exception):
    pass


def default_log(message, level='info'):
    getattr(logging.getLogger('storyboard'), {'success': 'info'}.get(level, level), logging.info)(message)


class HttpClient:
    def __init__(self, api_key='', session=None, log=None, timeout=TIMEOUT):
        self.api_key = api_key.strip()
        self.session = session or requests.Session()
        self.log = log or default_log
        self.timeout = timeout

    def redact(self, text):
        return str(text).replace(self.api_key, '[REDACTED]') if self.api_key else str(text)

    def request(self, method, url, *, check_status=True, authenticated=True, **kwargs):
        headers = {'Accept': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.api_key
        headers.update(kwargs.pop('headers', {}))
        try:
            response = self.session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
            if check_status and not 200 <= response.status_code < 300:
                body = self.redact(response.text)
                error = RequestError(f'HTTP {response.status_code}: {body}', response.status_code, body)
                response.close()
                raise error
            return response
        except (requests.RequestException, RequestError) as error:
            message = self.redact(error)
            self.log(message, 'error')
            if isinstance(error, RequestError):
                raise
            raise RequestError(message) from None

    def json(self, response):
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError('响应必须为 JSON 对象')
            return value
        except ValueError as error:
            message = f'HTTP {response.status_code}: 无效 JSON：{self.redact(response.text)} ({error})'
            self.log(message, 'error')
            raise RequestError(message) from None

    def close(self):
        self.session.close()


def objects(payload):
    """Root first, followed by common provider wrappers."""
    result = [payload]
    for obj in result:
        for key in ('data', 'result', 'task'):
            if isinstance(obj.get(key), dict):
                result.append(obj[key])
    return result


def extract(payload, keys):
    for key in keys:
        for obj in objects(payload):
            if obj.get(key) is not None and obj[key] != '':
                return obj[key]
    return None
