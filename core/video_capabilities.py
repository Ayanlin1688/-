"""Verified relay video limits, shared by the form and submission validation.

These limits describe provider capabilities; they never add models to a catalog.
"""
import logging
import re

from .model_catalog import family_for
from .model_parameters import BASE_RATIOS, H3_SIZES

_LOG = logging.getLogger(__name__)


def output_size(ratio, resolution):
    """Use an even integer long side, keeping the requested short side."""
    short = {'480p': 480, '768p': 768, '720p': 720, '1080p': 1080,
             '2K': 2560, '4K': 3840}[resolution]
    width, height = (int(value) for value in ratio.split(':'))
    # Round the long side up to an even integer (480p portrait = 480x854).
    import math
    long = math.ceil(short * max(width, height) / min(width, height) / 2) * 2
    return f'{long}x{short}' if width >= height else f'{short}x{long}'


def capabilities(model, catalog=None):
    model = str(model or '')
    family = family_for(model, catalog)
    snapshot = catalog.snapshot() if hasattr(catalog, 'snapshot') else catalog
    record = snapshot.get(model, {}) if isinstance(snapshot, dict) else {}
    if not isinstance(record, dict):
        record = {}

    def cap_value(name, fallback):
        value = record.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else fallback
    lower = model.lower()
    source = 'upstream'
    durations, default, presets = tuple(range(4, 16)), 5, (5, 10, 15)
    ratios, resolutions = BASE_RATIOS, ('720p',)
    limits, audio, seed = (9, 3, 3), False, False
    if family == 'MiniMax-H3' or (not family and lower.startswith('minimax-h3')):
        ratios, resolutions = tuple(H3_SIZES), ('768p', '1080p', '2K', '4K')
    elif family == 'video-v2' or (not family and re.match(r'^video-v2(?:$|[^0-9])', lower)):
        durations = (5, 10, 15)
        ratios, audio = BASE_RATIOS + ('21:9',), True
        resolutions = (('480p',) if '480p' in lower else ('720p',)
                       if any(value in lower for value in ('720p', 'fast', '特价'))
                       else ('480p', '720p', '1080p'))
    elif family == 'video-v3' or (not family and re.match(r'^video-v3(?:$|[^0-9])', lower)):
        # The documented video-v3 endpoint accepts 4-29 seconds.  The
        # separate video-v3-480p SKU keeps its own resolution below.
        durations, default, presets = tuple(range(4, 30)), 29, (5, 10, 15, 29)
        ratios = ('16:9', '9:16', '1:1')
        resolutions = ('480p',) if '480p' in lower else ('720p',)
        limits, audio, seed = (30, 10, 10), True, True
    elif family in ('video-v1', 'grok-imagine-1.5-video'):
        # Preserve the existing, separately verified legacy protocols.
        from .model_parameters import legacy_model_options
        legacy = legacy_model_options(family)
        durations, ratios = legacy['durations'], legacy['ratios']
        resolutions = legacy['resolutions'] or ('720p',)
        limits = (legacy['max_images'], 0, 0)
    elif re.search(r'veo|gemini', lower):
        # Keep the separate Gemini route's catalog options; OpenAI fallback
        # must not replace dimensions supplied for that protocol.
        ratios = tuple(record.get('ratios') or BASE_RATIOS)
        resolutions = tuple(record.get('resolutions') or ('720p',))
        durations = tuple(record.get('durations') or range(4, 16))
        limits = (cap_value('max_images', 3), 0, 0)
    elif record and record.get('kind') == 'video':
        # Unrecognized SKUs, including Wan when present in a live catalog, are
        # driven by that route's directory snapshot instead of a built-in name.
        durations = tuple(record.get('durations') or range(4, 16))
        ratios = tuple(record.get('ratios') or BASE_RATIOS)
        resolutions = tuple(record.get('resolutions') or ('720p',))
        limits = (cap_value('max_images', 9), cap_value('max_videos', 3), cap_value('max_audios', 3))
        audio = bool(record.get('audio', False))
        seed = bool(record.get('seed', False))
    else:
        source = 'fallback'
        _LOG.warning('未识别的视频模型，使用保守能力：%s', model)
    audio = audio and record.get('audio', True) is not False
    seed = seed and record.get('seed', True) is not False
    sizes = {ratio: tuple(res if 'x' in res else output_size(ratio, res) for res in resolutions) for ratio in ratios}
    if family == 'MiniMax-H3' or (not family and lower.startswith('minimax-h3')):
        sizes = {ratio: (values[1], values[2], '2K', '4K') for ratio, values in H3_SIZES.items()}
    return dict(durations=durations, default_duration=default, quick_presets=presets,
                ratios=ratios, resolutions=resolutions, output_sizes=sizes,
                max_images=limits[0], max_videos=limits[1], max_audios=limits[2],
                supports_audio=audio, supports_seed=seed, source=source)


def validate_media_file(path, kind):
    """Validate local media before opening a connection (URLs are handled elsewhere)."""
    from pathlib import Path
    if kind not in ('videos', 'audios'):
        raise ValueError('参考素材类型不支持')
    path = Path(path)
    extensions, limit = ({'.mp4', '.webm', '.mov'}, 200) if kind == 'videos' else (
        {'.mp3', '.wav', '.m4a', '.aac', '.ogg'}, 50)
    name = '视频' if kind == 'videos' else '音频'
    if not path.is_file():
        raise ValueError(f'参考{name}文件不存在，请重新选择')
    if path.suffix.lower() not in extensions:
        raise ValueError(f'参考{name}格式不支持，请重新选择')
    if path.stat().st_size == 0:
        raise ValueError(f'参考{name}文件为空，请重新选择')
    if path.stat().st_size > limit * 1024 * 1024:
        raise ValueError(f'参考{name}不能超过 {limit}MB')
    return str(path)
