"""Single-model video API adapters. No implicit retries or model failover."""
from contextlib import ExitStack
import mimetypes
import json
from pathlib import Path
from urllib.parse import quote, urlsplit
from .http_client import HttpClient, RequestError, extract, objects
from .model_parameters import GROK, H3, V3_MODELS, validate_task_parameters, model_prompt
from .log_redaction import redact_text, redact_structure


class ApiClient(HttpClient):
    def __init__(self, base_url='https://api.kkone.vip/v1', api_key='', debug_mode=False, log_secrets=(), **kwargs):
        self.log_secrets = tuple(log_secrets)
        super().__init__(api_key, **kwargs)
        self.base_url = base_url.strip().rstrip('/')
        self.debug_mode = debug_mode

    def redact(self, text):
        return redact_text(text, (self.api_key, *self.log_secrets))

    def _debug_request(self, endpoint, payload, multipart=False):
        enabled = self.debug_mode() if callable(self.debug_mode) else self.debug_mode
        if enabled:
            kind = 'multipart/form-data（文件清单，不输出二进制）' if multipart else 'application/json'
            self.log(self.redact(f'请求: POST {self.base_url + endpoint}；Content-Type={kind}；Authorization: Bearer [REDACTED]'), 'debug')
            # Redact string values before JSON escaping, including credentials
            # accidentally present in prompt text or reference URLs.
            sanitized = redact_structure(payload, self.redact)
            self.log('请求体: ' + json.dumps(sanitized, ensure_ascii=False), 'debug')

    def create_task(self, model, prompt, image_urls, params):
        endpoint = '/video/generations' if model == 'video-v1' else '/videos'
        try:
            references = params.get('image_paths', image_urls) if model == GROK else image_urls
            if not isinstance(references, (list, tuple)):
                raise ValueError('参数校验失败：参考图必须是数组')
            fields = validate_task_parameters(model, prompt, params, len(references))
            if model != GROK:
                for url in references:
                    if not isinstance(url, str) or urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).hostname:
                        raise ValueError('参数校验失败：参考图必须是有效的 HTTP/HTTPS 图片 URL')
            payload = dict(model=model, prompt=model_prompt(model, prompt), **fields)
            if model != H3 or image_urls:
                payload['images'] = image_urls
            with ExitStack() as stack:
                if model == GROK:
                    # Grok requires real files: params.image_paths (or the third argument) holds local paths.
                    # Text-only fields use (None, value) to force multipart even with no images.
                    files = [(key, (None, value)) for key, value in dict(model=model, prompt=prompt, **fields).items()]
                    manifest = dict(model=model, prompt=prompt, **fields, input_reference=[])
                    for value in references:
                        if not isinstance(value, (str, Path)) or not Path(value).is_file():
                            raise ValueError('参数校验失败：Grok 参考图必须是存在的本地文件')
                        path = Path(value)
                        stream = stack.enter_context(path.open('rb'))
                        files.append(('input_reference', (path.name, stream, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')))
                        manifest['input_reference'].append(dict(filename=path.name, path=str(path.resolve()), size_bytes=path.stat().st_size,
                                                                content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'))
                    self._debug_request(endpoint, manifest, multipart=True)
                    response = stack.enter_context(self.request('POST', self.base_url + endpoint, files=files))
                else:
                    self._debug_request(endpoint, payload)
                    response = stack.enter_context(self.request('POST', self.base_url + endpoint, json=payload))
                raw = self.json(response)
                task_id = extract(raw, ('task_id', 'id'))
                if task_id is None:
                    raise RequestError(f'HTTP {response.status_code}: 创建响应缺少 task_id/id：{self.redact(response.text)}')
                return str(task_id)
        except Exception as error:
            self.log(f'提交失败：{self.redact(error)}', 'error')
            raise

    def query_task(self, task_id, model):
        endpoint = '/video/generations/' if model == 'video-v1' else '/videos/'
        with self.request('GET', self.base_url + endpoint + quote(str(task_id), safe='')) as response:
            raw = self.json(response)
        status = str(extract(raw, ('status', 'state')) or 'queued').lower()
        status = {'success': 'completed', 'succeeded': 'completed', 'done': 'completed',
                  'running': 'processing', 'in_progress': 'processing', 'pending': 'queued',
                  'error': 'failed', 'cancelled': 'failed', 'canceled': 'failed'}.get(status, status)
        if status not in {'queued', 'processing', 'completed', 'failed'}:
            raise RequestError(f'未知任务状态：{status}')
        value = extract(raw, ('progress',)) or 0
        try:
            progress = max(0, min(100, float(str(value).rstrip('%'))))
        except (TypeError, ValueError):
            progress = 0
        url = extract(raw, ('result_url', 'video_url', 'url'))
        if not url:
            url = next((obj['metadata']['url'] for obj in objects(raw)
                        if isinstance(obj.get('metadata'), dict) and obj['metadata'].get('url')), None)
        return dict(status=status, progress=progress, result_url=url, raw=raw)

    def test_connection(self, uploader=None):
        """With an uploader, report both independent probes; legacy callers get a bool."""
        try:
            with self.request('GET', self.base_url + '/models', check_status=False) as response:
                ok = 200 <= response.status_code < 300
                video = dict(ok=ok, status_code=response.status_code,
                             message='视频 API 可达且模型列表请求成功' if ok else f'HTTP {response.status_code}: {self.redact(response.text)[:600]}')
        except Exception as error:
            video = dict(ok=False, status_code=getattr(error, 'status_code', None), message=self.redact(error))
        self.log(video['message'], 'success' if video['ok'] else 'error')
        if uploader is None:
            return video['ok']
        upload = uploader.test_connection()
        return dict(ok=video['ok'] and upload['ok'], video=video, upload=upload)
