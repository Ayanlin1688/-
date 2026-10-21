"""Shared bounded requests and credential-redacted error reporting."""
import logging
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

TIMEOUT = (10, 30)


class RequestError(RuntimeError):
    def __init__(self, message, status_code=None, body='', *, retry_safe=False):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.retry_safe = retry_safe


class Cancelled(Exception):
    pass


def default_log(message, level='info'):
    getattr(logging.getLogger('storyboard'), {'success': 'info'}.get(level, level), logging.info)(message)


class HttpClient:
    def __init__(self, api_key='', session=None, log=None, timeout=TIMEOUT, direct_session=None):
        self.api_key = api_key.strip()
        self.session = session or requests.Session()
        # Keep the direct session separate from the normal session so an
        # environment proxy failure can be retried once without changing the
        # caller's configured session.
        self.direct_session = direct_session
        if self.direct_session is not None:
            self._configure_direct_session(self.direct_session)
        self.log = log or default_log
        self.timeout = timeout

    @staticmethod
    def _configure_direct_session(session):
        session.trust_env = False
        session.proxies.update({'http': None, 'https': None})

    @staticmethod
    def _can_retry_direct(error):
        if isinstance(error, requests.exceptions.SSLError) or error.response is not None:
            return False
        if isinstance(error, (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout)):
            return True
        # A missing response alone is not proof that the request was unsent:
        # RemoteDisconnected also becomes ConnectionError after a server has
        # accepted the body. Only a confirmed connection-establishment failure
        # is safe to replay.
        reason = error.args[0] if error.args else None
        if isinstance(reason, MaxRetryError):
            reason = reason.reason
        return isinstance(reason, NewConnectionError)

    def redact(self, text):
        return str(text).replace(self.api_key, '[REDACTED]') if self.api_key else str(text)

    def request(self, method, url, *, check_status=True, authenticated=True, **kwargs):
        headers = {'Accept': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.api_key
        headers.update(kwargs.pop('headers', {}))
        timeout = kwargs.pop('timeout', self.timeout)
        # requests consumes multipart streams while preparing the request,
        # before even attempting the proxy connection. Preserve their position
        # so a direct retry sends the same file bytes instead of an empty file.
        file_positions = []
        files = kwargs.get('files') or ()
        if not isinstance(files, (dict, list, tuple)):
            files = kwargs['files'] = list(files)
        entries = files.values() if isinstance(files, dict) else (value for _, value in files)
        for value in entries:
            stream = value[1] if isinstance(value, (tuple, list)) else value
            if hasattr(stream, 'read'):
                try:
                    file_positions.append((stream, stream.tell()))
                except (AttributeError, OSError, ValueError):
                    file_positions.append((stream, None))

        try:
            # Callers may tighten a specific probe without changing the client's
            # normal generation timeout contract.
            try:
                response = self.session.request(method, url, headers=headers, timeout=timeout, **kwargs)
            except requests.exceptions.ConnectionError as error:
                if not self._can_retry_direct(error):
                    raise
                for stream, position in file_positions:
                    if position is None:
                        raise error
                    try:
                        stream.seek(position)
                    except (AttributeError, OSError, ValueError):
                        raise error
                if self.direct_session is None:
                    self.direct_session = requests.Session()
                    self._configure_direct_session(self.direct_session)
                self.log('系统代理连接失败，已切换直连重试一次', 'info')
                response = self.direct_session.request(method, url, headers=headers, timeout=timeout, **kwargs)
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
            retry_safe = (isinstance(error, requests.exceptions.ConnectionError)
                          and self._can_retry_direct(error))
            raise RequestError(message, retry_safe=retry_safe) from None

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
        if self.direct_session is not None:
            self.direct_session.close()


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
