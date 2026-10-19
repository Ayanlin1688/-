"""Resolve configured API stations by capability without performing I/O."""
from __future__ import annotations

from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit


CAPABILITIES = ('video', 'chat', 'image')
_LINE_FIELDS = ('base_url', 'api_key', 'upload_url', 'upload_api_key')


def normalize_base_url(url):
    value = str(url or '').strip().rstrip('/')
    if not value:
        return ''
    parts = urlsplit(value)
    if parts.scheme and parts.netloc and parts.path in ('', '/'):
        return urlunsplit((parts.scheme, parts.netloc, '/v1', parts.query, parts.fragment))
    return value


def _active_line(config):
    api = config.get('api', {})
    line = deepcopy(api) if isinstance(api, dict) else {}
    for field in _LINE_FIELDS:
        line.setdefault(field, '')
    line['base_url'] = normalize_base_url(line.get('base_url'))
    line['source'] = 'active'
    return line


def resolve_line(config, capability):
    """Return the active API or an enabled station selected for a capability."""
    config = config if isinstance(config, dict) else {}
    routing = config.get('line_routing', {})
    station_id = routing.get(capability, 'active') if isinstance(routing, dict) else 'active'
    if not station_id or station_id == 'active':
        return _active_line(config)

    stations = config.get('stations', [])
    if isinstance(stations, dict):
        candidates = stations.values()
    elif isinstance(stations, (list, tuple)):
        candidates = stations
    else:
        candidates = ()
    for station in candidates:
        if not isinstance(station, dict):
            continue
        if station.get('id') != station_id or not station.get('enabled'):
            continue
        line = {field: station.get(field) or '' for field in _LINE_FIELDS}
        line['base_url'] = normalize_base_url(line['base_url'])
        line['source'] = str(station_id)
        return line
    return _active_line(config)


def distinct_lines(config):
    """Return the current API and selected capability routes, deduplicated."""
    config = config if isinstance(config, dict) else {}
    current = _active_line(config)
    lines, seen = [], set()
    for line in (current, *(resolve_line(config, capability) for capability in CAPABILITIES)):
        identity = (line.get('base_url', ''), line.get('api_key', ''))
        if identity in seen:
            continue
        seen.add(identity)
        lines.append(line)
    return lines
