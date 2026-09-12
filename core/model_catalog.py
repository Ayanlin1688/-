"""Dynamic model metadata, conservative protocol defaults, and offline cache."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import time


_FIELDS = (
    'id', 'name', 'display_name', 'description', 'kind', 'family',
    'resolutions', 'ratios', 'durations', 'audio', 'seed', 'max_images',
    'pricing_text', 'aliases', 'available', 'protocol_known',
    'capability_source',
)
_CACHE_FIELDS = set(_FIELDS)
_VIDEO_ENDPOINTS = {'openai-video', 'video', 'videos', 'video-generation'}
_IMAGE_ENDPOINTS = {'openai', 'openai-image', 'gemini', 'image', 'images'}
_RESOLUTION_RE = re.compile(r'(?<![A-Za-z0-9])(?:480p|720p|768p|1080p|2K|4K)(?![A-Za-z0-9])', re.I)
_RATIO_RE = re.compile(r'(?<!\d)(\d{1,2}\s*:\s*\d{1,2})(?!\d)')
_DURATION_RANGE_RE = re.compile(r'(?<!\d)(\d{1,3})\s*(?:-|~|至|到)\s*(\d{1,3})\s*(?:秒|seconds?\b|sec\b|s\b)', re.I)
_DURATION_SINGLE_RE = re.compile(r'(?<!\d)(\d{1,3})\s*(?:秒|seconds?\b|sec\b|s\b)', re.I)
_PRICE_RE = re.compile(r'(?:(?:[$¥￥]\s*\d+(?:\.\d+)?)|(?:\d+(?:\.\d+)?\s*(?:USD|RMB|元|美元)))(?:\s*/?\s*[\w一-龥]+)?', re.I)


def _unique(values):
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _base_family(model):
    value = str(model or '').strip().replace('（', '(')
    if value == 'MiniMax-H3' or value.startswith('MiniMax-H3(') or value.startswith('MiniMax-H3-'):
        return 'MiniMax-H3'
    if value == 'grok-imagine-1.5-video':
        return value
    if value in {'seedance-2.5', 'seedance2.5', 'sd-2.5', 'sd2.5'}:
        return 'video-v3'
    if re.match(r'^video-v1(?:$|[-(])', value):
        return 'video-v1'
    if re.match(r'^video-v2(?:$|[-(])', value):
        return 'video-v2'
    if re.match(r'^video-v3(?:$|[-(])', value):
        return 'video-v3'
    return ''


def family_for(model, catalog=None):
    record = _catalog_record(model, catalog)
    if record is not None and record.get('family'):
        return str(record['family'])
    return _base_family(model)


def resolve_model_id(model, catalog):
    if not model or not isinstance(catalog, dict) or model in catalog:
        return model
    replacements = [name for name, record in catalog.items()
                    if model in record.get('aliases', []) and record.get('available', True)]
    return replacements[0] if len(replacements) == 1 else model


def _catalog_record(model, catalog):
    if catalog is None:
        return None
    if hasattr(catalog, 'snapshot'):
        catalog = catalog.snapshot()
    if not isinstance(catalog, dict):
        return None
    value = catalog.get(model)
    return value if isinstance(value, dict) else None


def _defaults(family, model=''):
    if family == 'MiniMax-H3':
        return dict(resolutions=('1080p', '2K', '4K'), ratios=('16:9', '9:16', '1:1', '2:3', '3:2', '3:4', '4:3', '21:9'),
                    durations=tuple(range(4, 16)), audio=False, seed=False, max_images=9, protocol_known=True)
    if family == 'video-v1':
        return dict(resolutions=(), ratios=('16:9', '9:16', '1:1', '4:3', '3:4'), durations=(5, 10, 15),
                    audio=False, seed=False, max_images=9, protocol_known=True)
    if family == 'video-v2':
        resolutions = ('720p',)
        return dict(resolutions=resolutions, ratios=('16:9', '9:16', '1:1', '4:3', '3:4', '21:9'), durations=(5, 10, 15),
                    audio=True, seed=False, max_images=9, protocol_known=True)
    if family == 'video-v3':
        resolutions = ('480p',) if re.search(r'v3[-_( ]?480p', model, re.I) else ('720p',)
        return dict(resolutions=resolutions, ratios=('16:9', '9:16', '1:1'), durations=tuple(range(4, 31)),
                    audio=True, seed=True, max_images=30, protocol_known=True)
    if family == 'grok-imagine-1.5-video':
        return dict(resolutions=('480p', '720p', '1080p'), ratios=('16:9', '9:16', '1:1', '2:3', '3:2'), durations=tuple(range(1, 16)),
                    audio=False, seed=False, max_images=9, protocol_known=True)
    # Wan is visible at its supported UI resolution, but its request protocol
    # is intentionally unknown until provider documentation is confirmed.
    if str(model).strip() == 'wan-3.0':
        return dict(resolutions=('720p',), ratios=(), durations=(), audio=False, seed=False, max_images=0, protocol_known=False)
    return dict(resolutions=(), ratios=(), durations=(), audio=False, seed=False, max_images=0, protocol_known=False)


def _builtin_record(model, description='', display_name=None):
    family = _base_family(model)
    defaults = _defaults(family, model)
    kind = 'video' if family or model == 'wan-3.0' else 'unknown'
    return dict(id=model, name=model, display_name=display_name or model, description=description,
                kind=kind, family=family, pricing_text='计费未提供', aliases=[], available=True,
                capability_source='builtin', **{key: defaults[key] for key in ('resolutions', 'ratios', 'durations', 'audio', 'seed', 'max_images', 'protocol_known')})


def builtin_models():
    names = ['video-v1', 'video-v2', 'video-v2-fast', 'video-v3', 'seedance-2.5', 'MiniMax-H3', 'grok-imagine-1.5-video',
             'seedance2.5', 'sd-2.5', 'sd2.5', 'wan-3.0']
    records = {_name: _builtin_record(_name) for _name in names}
    # Alias models retain their wire IDs while sharing the protocol family.
    return records


def _as_list(value, cast=None):
    if not isinstance(value, (list, tuple)):
        return None
    result = []
    for item in value:
        if cast:
            try:
                item = cast(item)
            except (TypeError, ValueError):
                return None
        if item not in result:
            result.append(item)
    return result


def _description_capabilities(description):
    text = str(description or '')
    resolutions = _unique([match.lower() if match.lower() in {'2k', '4k'} else match.lower() for match in _RESOLUTION_RE.findall(text)])
    # Preserve the conventional capitalisation used by the UI.
    resolutions = [('2K' if item == '2k' else '4K' if item == '4k' else item) for item in resolutions]
    ratios = [re.sub(r'\s+', '', item) for item in _RATIO_RE.findall(text)]
    durations = []
    for start, end in _DURATION_RANGE_RE.findall(text):
        start, end = int(start), int(end)
        if start <= end <= start + 60:
            durations.extend(range(start, end + 1))
    if not durations:
        durations = [int(item) for item in _DURATION_SINGLE_RE.findall(text)]
    prices = _PRICE_RE.findall(text)
    return dict(resolutions=_unique(resolutions), ratios=_unique(ratios), durations=_unique(durations),
                pricing_text=' '.join(_unique(prices)))


def _record_from_upstream(row):
    if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'].strip():
        raise ValueError('上游模型条目缺少有效 id')
    model = row['id'].strip()
    family = _base_family(model)
    defaults = _defaults(family, model)
    description = str(row.get('description') or '')
    detail = {name: row[name] for name in ('resolutions', 'ratios', 'durations', 'audio', 'seed', 'max_images') if name in row}
    if isinstance(row.get('capabilities'), dict):
        detail.update(row['capabilities'])
    described = _description_capabilities(description)
    endpoint_types = row.get('supported_endpoint_types')
    if isinstance(endpoint_types, str):
        endpoint_types = [endpoint_types]
    endpoints = {str(item).lower() for item in endpoint_types or []}
    explicit_kind = str(row.get('kind') or row.get('type') or '').lower()
    if explicit_kind in {'video', 'image', 'unknown'}:
        kind = explicit_kind
    elif endpoints & _VIDEO_ENDPOINTS or family:
        kind = 'video'
    elif endpoints & _IMAGE_ENDPOINTS:
        kind = 'image'
    else:
        kind = 'unknown'

    def choose(name, cast=None):
        explicit = _as_list(detail.get(name), cast)
        if explicit is not None:
            return explicit, 'upstream'
        described_value = described.get(name)
        if described_value:
            return described_value, 'description'
        return list(defaults[name]), 'builtin'

    resolutions, source = choose('resolutions')
    ratios, source_ratio = choose('ratios')
    durations, source_duration = choose('durations', int)
    audio = detail.get('audio') if isinstance(detail.get('audio'), bool) else defaults['audio']
    seed = detail.get('seed') if isinstance(detail.get('seed'), bool) else defaults['seed']
    max_images = detail.get('max_images') if isinstance(detail.get('max_images'), int) and not isinstance(detail.get('max_images'), bool) else defaults['max_images']
    source_name = 'upstream' if detail else ('description' if any((described['resolutions'], described['ratios'], described['durations'], described['pricing_text'])) else 'builtin')
    pricing = row.get('pricing_text') or row.get('price') or row.get('pricing') or row.get('cost')
    if not isinstance(pricing, str) or not pricing.strip():
        pricing = described['pricing_text'] or '计费未提供'
    aliases = row.get('aliases') if isinstance(row.get('aliases'), list) else []
    aliases = [str(item) for item in aliases if isinstance(item, (str, int, float))]
    return dict(id=model, name=model, display_name=str(row.get('display_name') or row.get('name') or model),
                description=description, kind=kind, family=family, resolutions=resolutions, ratios=ratios,
                durations=durations, audio=audio, seed=seed, max_images=max_images, pricing_text=pricing,
                aliases=aliases, available=True, protocol_known=bool(defaults['protocol_known']), capability_source=source_name)


def options_for(model, catalog=None):
    record = _catalog_record(model, catalog)
    if record is not None:
        return {key: deepcopy(record.get(key)) for key in ('ratios', 'resolutions', 'durations', 'audio', 'seed', 'max_images', 'protocol_known')}
    builtin = builtin_models().get(model)
    if builtin is None:
        family = _base_family(model)
        builtin = _builtin_record(model)
        if not family and model != 'wan-3.0':
            builtin['kind'] = 'unknown'
    return {key: deepcopy(builtin[key]) for key in ('ratios', 'resolutions', 'durations', 'audio', 'seed', 'max_images', 'protocol_known')}


class ModelCatalog:
    CACHE_NAME = 'models_cache.json'
    CACHE_TTL = 24 * 60 * 60

    def __init__(self, config_path, base_url, api_key, log=None):
        self.config_path = Path(config_path)
        self.base_url = str(base_url or '').strip().rstrip('/')
        self.api_key = str(api_key or '').strip()
        self.log = log or (lambda message, level='info': None)
        self._models = {}
        self.fetched_at = 0
        self.source = 'builtin'
        self.expired = False
        self.last_error = None
        self._load()

    @property
    def cache_path(self):
        return self.config_path.parent / self.CACHE_NAME

    @property
    def key_fingerprint(self):
        return hashlib.sha256(self.api_key.encode('utf-8')).hexdigest() if self.api_key else ''

    def _load(self):
        if not self.api_key:
            self._models = builtin_models()
            self.source = 'builtin'
            return
        try:
            document = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if set(document) != {'source', 'base_url', 'key_fingerprint', 'fetched_at', 'models'}:
                raise ValueError('缓存结构包含未知字段')
            if document.get('source') != 'upstream':
                raise ValueError('缓存来源无效')
            if document.get('base_url') != self.base_url or document.get('key_fingerprint') != self.key_fingerprint:
                raise ValueError('缓存身份不匹配')
            models = document.get('models')
            if not isinstance(models, dict) or not all(key == item.get('id') and _valid_record(item) for key, item in models.items()):
                raise ValueError('缓存模型内容无效')
            self._models = deepcopy(models)
            self.fetched_at = float(document.get('fetched_at', 0))
            self.expired = bool(self.fetched_at and time.time() - self.fetched_at > self.CACHE_TTL)
            self.source = 'cache'
        except Exception:
            self._models = builtin_models()
            self.source = 'builtin'
            self.fetched_at = 0
            self.expired = False

    def snapshot(self):
        return deepcopy(self._models)

    def refresh(self, client, persist=True):
        if not self.api_key:
            self.last_error = None
            self.source = 'builtin'
            self._models = builtin_models()
            self.fetched_at = 0
            self.expired = False
            return self.snapshot()
        try:
            raw = client.fetch_models()
            if not isinstance(raw, list):
                raise ValueError('上游模型列表必须是数组')
            models = {}
            for row in raw:
                record = _record_from_upstream(_scrub(row, self.api_key))
                models[record['id']] = record
            if not all(_valid_record(item) for item in models.values()):
                raise ValueError('上游模型记录无效')
            fetched_at = time.time()
            document = {'source': 'upstream', 'base_url': self.base_url, 'key_fingerprint': self.key_fingerprint,
                        'fetched_at': fetched_at, 'models': models}
            if persist:
                self._atomic_write(document)
            self._models = models
            self.fetched_at = fetched_at
            self.source = 'upstream'
            self.expired = False
            self.last_error = None
            return self.snapshot()
        except Exception as error:
            self.last_error = _redact_error(error, self.api_key)
            self.log('使用离线模型缓存', 'warning')
            return self.snapshot()

    def redact(self, value):
        return _redact_error(value, self.api_key)

    def save(self):
        if self.source == 'upstream' and self.api_key:
            self._atomic_write({'source': 'upstream', 'base_url': self.base_url,
                                'key_fingerprint': self.key_fingerprint,
                                'fetched_at': self.fetched_at, 'models': self._models})

    def _atomic_write(self, document):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.cache_path.with_name(self.cache_path.name + '.tmp')
        try:
            temp_path.write_text(json.dumps(document, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            os.replace(temp_path, self.cache_path)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _redact_error(error, key):
    return str(error).replace(key, '[REDACTED]') if key else str(error)


def _scrub(value, key):
    """Remove a credential from provider text before it can reach disk."""
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, '[REDACTED]')
    if isinstance(value, dict):
        return {name: _scrub(item, key) for name, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, key) for item in value]
    return value


def _valid_record(record):
    if not isinstance(record, dict) or set(record) - _CACHE_FIELDS or any(field not in record for field in _FIELDS):
        return False
    if not isinstance(record['id'], str) or not record['id'] or record['name'] != record['id']:
        return False
    if record['kind'] not in {'video', 'image', 'unknown'} or not isinstance(record['family'], str):
        return False
    if not isinstance(record['resolutions'], (list, tuple)) or not all(isinstance(item, str) for item in record['resolutions']):
        return False
    if not isinstance(record['ratios'], (list, tuple)) or not all(isinstance(item, str) for item in record['ratios']):
        return False
    if not isinstance(record['durations'], (list, tuple)) or not all(isinstance(item, int) and not isinstance(item, bool) for item in record['durations']):
        return False
    return isinstance(record['audio'], bool) and isinstance(record['seed'], bool) and isinstance(record['max_images'], int) and not isinstance(record['max_images'], bool)


__all__ = ['ModelCatalog', 'builtin_models', 'options_for', 'family_for']
