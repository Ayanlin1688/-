"""Submission contract from https://image.kkone.vip/1/docs.html, checked 2026-09-11.

Only the existing text/image generation flows are exposed. No upload behavior lives here.
"""
import re

V3_MODELS = {'video-v3', 'seedance-2.5', 'seedance2.5', 'sd-2.5', 'sd2.5'}
GROK = 'grok-imagine-1.5-video'
H3 = 'MiniMax-H3'
MODELS = ['video-v1', 'video-v2', 'video-v2-fast', 'video-v3', 'seedance-2.5', H3, GROK,
          'seedance2.5', 'sd-2.5', 'sd2.5']
H3_SIZES = {
    '16:9': ('864x480', '1376x768', '1920x1088'),
    '9:16': ('480x864', '768x1376', '1088x1920'),
    '1:1': ('640x640', '1024x1024', '1440x1440'),
    '2:3': ('544x800', '832x1248', '1184x1760'),
    '3:2': ('800x544', '1248x832', '1760x1184'),
    '3:4': ('576x736', '896x1184', '1248x1664'),
    '4:3': ('736x576', '1184x896', '1664x1248'),
    '21:9': ('992x416', '1568x672', '2208x960'),
}
H3_RESOLUTIONS = ('480p', '768p', '1080p', '2K', '4K')
BASE_RATIOS = ('16:9', '9:16', '1:1', '4:3', '3:4')


def model_options(model):
    if model == 'video-v1':
        return dict(ratios=BASE_RATIOS, resolutions=(), durations=(5, 10, 15), audio=False, seed=False, max_images=9)
    if model in {'video-v2', 'video-v2-fast'}:
        return dict(ratios=BASE_RATIOS + ('21:9',), resolutions=('480p', '720p', '1080p'), durations=(5, 10, 15), audio=True, seed=False, max_images=9)
    if model in V3_MODELS:
        return dict(ratios=('16:9', '9:16', '1:1'), resolutions=('720p',), durations=tuple(range(4, 31)), audio=True, seed=True, max_images=30)
    if model == H3:
        return dict(ratios=tuple(H3_SIZES), resolutions=H3_RESOLUTIONS, durations=tuple(range(4, 16)), audio=False, seed=False, max_images=9)
    if model == GROK:
        return dict(ratios=BASE_RATIOS + ('2:3', '3:2'), resolutions=('480p', '720p', '1080p'), durations=tuple(range(1, 16)), audio=False, seed=False, max_images=9)
    raise ValueError(f'不支持的模型：{model}')


def h3_size(ratio, resolution):
    if ratio not in H3_SIZES or resolution not in H3_RESOLUTIONS:
        raise ValueError(f'H3 不支持比例/分辨率组合：{ratio} + {resolution}')
    return resolution if resolution in ('2K', '4K') else H3_SIZES[ratio][H3_RESOLUTIONS.index(resolution)]


def generation_fields(model, params, image_count):
    """Validate internal UI values and return only fields accepted by this model."""
    options = model_options(model)
    duration = params.get('duration')
    ratio = params.get('aspect_ratio')
    resolution = params.get('resolution')
    if type(duration) is not int or duration not in options['durations']:
        allowed = '5、10、15' if model in {'video-v1', 'video-v2', 'video-v2-fast'} else f"{options['durations'][0]}–{options['durations'][-1]}"
        raise ValueError(f'{model} duration/seconds 必须是 {allowed} 秒的整数，当前为 {duration!r}')
    if ratio not in options['ratios']:
        raise ValueError(f"{model} 比例必须为 {' / '.join(options['ratios'])}，当前为 {ratio!r}")
    if options['resolutions'] and resolution not in options['resolutions']:
        raise ValueError(f"{model} 分辨率必须为 {' / '.join(options['resolutions'])}，当前为 {resolution!r}")
    if image_count > options['max_images']:
        raise ValueError(f"{model} 最多支持 {options['max_images']} 张参考图，当前为 {image_count} 张")
    if model == GROK and image_count > 1 and resolution == '1080p':
        raise ValueError('Grok 多图仅支持 480p / 720p，请修改分辨率')
    if options['audio'] and type(params.get('generate_audio')) is not bool:
        raise ValueError(f'{model} generate_audio 必须是布尔值 true/false')
    if model == 'video-v1':
        return dict(duration=duration, aspect_ratio=ratio)
    if model in {'video-v2', 'video-v2-fast'}:
        return dict(duration=duration, aspect_ratio=ratio, resolution=resolution, generate_audio=params['generate_audio'], videos=[], audios=[])
    if model in V3_MODELS:
        # The table marks bypass_face_check as required although the example omits it.
        # Send false explicitly; the application does not enable bypass behavior.
        fields = dict(duration=duration, ratio=ratio, resolution=resolution, generate_audio=params['generate_audio'],
                      bypass_face_check=False, videos=[], audios=[])
        seed = params.get('seed', '')
        if seed is not None and seed != '':
            if type(seed) is int:
                value = seed
            elif isinstance(seed, str) and re.fullmatch(r'[0-9]+', seed.strip()):
                value = int(seed.strip())
            else:
                raise ValueError(f'{model} seed 必须是 0–4294967295 的整数或留空')
            if not 0 <= value <= 4294967295:
                raise ValueError(f'{model} seed 必须是 0–4294967295 的整数或留空')
            fields['seed'] = value
        return fields
    if model == H3:
        fields = dict(workflow_id='multi-reference' if image_count else 'text-to-video', seconds=duration, size=h3_size(ratio, resolution))
        if resolution in ('2K', '4K'):
            fields['aspect_ratio'] = ratio
        return fields
    return dict(aspect_ratio=ratio, seconds=str(duration), resolution=resolution)


def validate_task_parameters(model, prompt, params, image_count):
    try:
        fields = generation_fields(model, params, image_count)
        if not isinstance(prompt, str) or (not prompt.strip() and not (model == 'video-v1' and image_count)):
            raise ValueError('提示词必须是非空文本')
        return fields
    except ValueError as error:
        raise ValueError(f'参数校验失败：{error}') from error


def model_prompt(model, prompt):
    if model in {'video-v2', 'video-v2-fast'}:
        labels = {'图': 'Image', '视频': 'Video', '音频': 'Audio'}
        return re.sub(r'@参考(图|视频|音频)(\d+)', lambda match: '@' + labels[match[1]] + match[2], prompt)
    return prompt
