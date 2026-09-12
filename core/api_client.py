"""Single-model video API adapters. No implicit retries or model failover."""
from contextlib import ExitStack
import mimetypes
import json
from pathlib import Path
from urllib.parse import quote, urlsplit
from .http_client import HttpClient, RequestError, extract, objects
from .model_catalog import family_for
from .model_parameters import GROK, H3, V3_MODELS, validate_task_parameters, model_prompt
from .log_redaction import redact_text, redact_structure


_MODEL_FIELDS = {
    'id', 'object', 'created', 'owned_by', 'name', 'display_name',
    'description', 'kind', 'type', 'supported_endpoint_types',
    'capabilities', 'aliases', 'pricing_text', 'price', 'pricing', 'cost',
    'resolutions', 'ratios', 'durations', 'audio', 'seed', 'max_images',
}


def _has_error_object(payload):
    return isinstance(payload, dict) and isinstance(payload.get('error'), (dict, str))


def _detail_object(payload):
    if not isinstance(payload, dict) or _has_error_object(payload):
        return None
    data = payload.get('data')
    return data if isinstance(data, dict) else payload if 'id' in payload else None


def _clean_model_record(row, wire_id=None):
    """Copy only documented model metadata and never provider credentials."""
    result = {key: row[key] for key in _MODEL_FIELDS if key in row}
    result['id'] = wire_id or str(row.get('id', '')).strip()
    if 'capabilities' in result:
        if not isinstance(result['capabilities'], dict):
            result.pop('capabilities', None)
        else:
            # Capabilities are structured provider metadata; preserve only the
            # fields consumed by the catalog and drop arbitrary nested values.
            result['capabilities'] = {
                key: result['capabilities'][key]
                for key in ('resolutions', 'ratios', 'durations', 'audio', 'seed', 'max_images')
                if key in result['capabilities']
            }
    return result


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

    def create_task(self, model, prompt, image_urls, params, catalog=None):
        family = family_for(model, catalog)
        endpoint = '/video/generations' if family == 'video-v1' else '/videos'
        try:
            references = params.get('image_paths', image_urls) if family == GROK else image_urls
            if not isinstance(references, (list, tuple)):
                raise ValueError('参数校验失败：参考图必须是数组')
            fields = validate_task_parameters(model, prompt, params, len(references), catalog)
            if family != GROK:
                for url in references:
                    if not isinstance(url, str) or urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).hostname:
                        raise ValueError('参数校验失败：参考图必须是有效的 HTTP/HTTPS 图片 URL')
            payload = dict(model=model, prompt=model_prompt(model, prompt, catalog), **fields)
            if family != H3 or image_urls:
                payload['images'] = image_urls
            with ExitStack() as stack:
                if family == GROK:
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

    def query_task(self, task_id, model, catalog=None):
        endpoint = '/video/generations/' if family_for(model, catalog) == 'video-v1' else '/videos/'
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

    def fetch_models(self):
        """Fetch and sanitize the provider model list.

        Model detail endpoints are optional on the upstream service.  A detail
        error means the endpoint is unsupported, so the current refresh stops
        probing details while retaining the already fetched list.
        """
        endpoint = self.base_url + '/models'
        with self.request('GET', endpoint, timeout=10) as response:
            payload = self.json(response)
        if _has_error_object(payload):
            raise RequestError(f'HTTP {response.status_code}: 模型列表返回错误对象')
        rows = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RequestError(f'HTTP {response.status_code}: 模型列表响应必须包含 data 数组')
        if any(not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'].strip()
               for row in rows):
            raise RequestError(f'HTTP {response.status_code}: 模型列表包含无效条目')
        records = [_clean_model_record(row) for row in rows]

        # A provider may advertise /models while not implementing model detail
        # routes. Keep discovery bounded even when a large list is returned.
        probes = 0
        for record in records:
            if probes >= 8:
                break
            if record.get('capabilities'):
                continue
            probes += 1
            try:
                detail_response = self.request(
                    'GET', endpoint + '/' + quote(record['id'], safe=''),
                    check_status=False, timeout=10)
                try:
                    detail_payload = self.json(detail_response)
                finally:
                    detail_response.close()
            except RequestError:
                break
            if detail_response.status_code == 404 or _has_error_object(detail_payload):
                break
            detail = _detail_object(detail_payload)
            if not isinstance(detail, dict):
                break
            record.update(_clean_model_record(detail, record['id']))
        return records
