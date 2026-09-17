"""Deterministic conversion between the supported H3 and V2 prompt schemas."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re

from .model_catalog import family_for


_H3_FIELDS = (
    'subject_definitions',
    'summary',
    'retention_analysis',
    'detailed_description',
    'overall_soundscape',
    'non_diegetic_music',
)
_H3_SIGNATURES = (
    r'(?im)^\s*subject_definitions\s*:',
    r'(?im)^\s*retention_analysis\s*:',
    r'(?im)^\s*detailed_description\s*:',
    r'(?i)\[\s*Shot\s+1\s*\]',
    r'(?i)\(S1\)\s+says\b',
    r'(?im)^\s*overall_soundscape\s*:',
)
_V2_SIGNATURES = (
    r'【分镜】',
    r'(?m)^\s*人物站位\s*：',
    r'(?m)^\s*镜头\s*1\s*：',
    r'【禁止项】',
    r'【强制声明】',
)
_V2_DECLARATIONS = (
    '该SKILL 由Work-Fisher制作，免费公开，禁止任何盗卖行为',
    '该SKILL由Work-Fisher制作，免费公开，禁止任何盗卖行为',
)
_V2_FORBIDDEN = '文字/UI/水印/Logo/角标/可读文字/真实UI'
_V2_REQUIRED = '无背景音乐,仅保留环境音与人声和音效;画面禁字幕/文字/水印/Logo;禁止可读文字(指画面字幕文字,不含人声台词)'
_SECTION_RE = re.compile(
    r'(?im)^\s*(subject_definitions|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music)\s*:\s*'
)
_H3_SHOT_RE = re.compile(r'(?im)^\s*\[\s*Shot\s+(\d+)\s*\]\s*')
_V2_SHOT_RE = re.compile(r'^\s*镜头\s*(\d+)\s*：\s*(.*)$')
_VOICE_RE = re.compile(r'^\s*【[^】\r\n]*声线】(?:\s*.*)?$')
_SOUND_RE = re.compile(r'^\s*环境音\s*：\s*(.*)$')


@dataclass(frozen=True)
class ConversionResult:
    text: str
    source_format: str
    target_format: str
    converted: bool
    warnings: tuple[str, ...] = ()
    shot_count: int = 0


def _text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError('提示词必须是字符串')
    return value.replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff')


def detect_format(prompt_text: str) -> str:
    """Return ``H3``, ``V2`` or ``text`` using the documented signatures."""
    text = _text(prompt_text)
    h3_score = sum(bool(re.search(pattern, text)) for pattern in _H3_SIGNATURES)
    if h3_score >= 2:
        return 'H3'
    if any(re.search(pattern, text) for pattern in _V2_SIGNATURES):
        return 'V2'
    return 'text'


def strip_v2_declaration(prompt_text: str) -> str:
    """Remove only the known V2 template declaration when it is the first content line."""
    text = _text(prompt_text)
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        content = line.rstrip('\n')
        if not content.strip():
            continue
        if content.strip() not in _V2_DECLARATIONS:
            return text
        remainder = ''.join(lines[index + 1:])
        return re.sub(r'\A[ \t]*\n', '', remainder, count=1)
    return text


def _parse_h3(prompt_text: str) -> dict[str, str]:
    text = _text(prompt_text).strip()
    matches = list(_SECTION_RE.finditer(text))
    names = [match.group(1).lower() for match in matches]
    if names != list(_H3_FIELDS):
        raise ValueError('H3 结构无效：必须按顺序且各一次包含六个标准字段')
    if text[:matches[0].start()].strip():
        raise ValueError('H3 结构无效：subject_definitions 前存在无法保留的内容')

    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        if not value:
            raise ValueError(f'H3 结构无效：字段 {names[index]} 不能为空')
        sections[names[index]] = value
    return sections


def _picture_to_v2(text: str) -> str:
    text = re.sub(r'<\s*Picture\s*(\d+)\s*>', lambda match: f'@图{match.group(1)}', text, flags=re.I)
    return re.sub(r'@Image\s*(\d+)', lambda match: f'@图{match.group(1)}', text, flags=re.I)


def _picture_to_h3(text: str) -> str:
    return re.sub(r'@(?:图片|图)\s*(\d+)', lambda match: f'<Picture {match.group(1)}>', text)


def _subject_position(value: str) -> str:
    lines = [line.strip() for line in _picture_to_v2(value).splitlines() if line.strip()]
    if not lines:
        raise ValueError('H3 结构无效：subject_definitions 没有主体描述')
    descriptions = []
    for line in lines:
        match = re.match(r'<\s*Subject\s+\d+\s*>\s+is\s+(.+)$', line, re.I)
        description = match.group(1).strip() if match else line
        if description:
            description = description[0].upper() + description[1:]
            descriptions.append(description)
    if not descriptions:
        raise ValueError('H3 结构无效：无法提取主体描述')
    return ' '.join(descriptions)


def _h3_shots(value: str) -> list[str]:
    matches = list(_H3_SHOT_RE.finditer(value))
    if not matches or value[:matches[0].start()].strip():
        raise ValueError('H3 镜头结构无效：detailed_description 必须从 [Shot 1] 开始')
    numbers = [int(match.group(1)) for match in matches]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError('H3 镜头结构无效：镜头编号必须从 1 连续递增且不能重复')

    shots = []
    previous_time = -1.0
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        body = value[match.end():end].strip()
        if not body:
            raise ValueError(f'H3 镜头结构无效：Shot {numbers[index]} 不能为空')
        timestamp = re.match(r'At\s+(\d{2}):(\d{2}(?:\.\d{3})?)\s*,\s*', body, re.I)
        if index == 0:
            if timestamp:
                raise ValueError('H3 镜头结构无效：Shot 1 不应包含时间码')
        else:
            if not timestamp:
                raise ValueError(f'H3 镜头结构无效：Shot {numbers[index]} 缺少递增时间码')
            current_time = int(timestamp.group(1)) * 60 + float(timestamp.group(2))
            if current_time <= previous_time:
                raise ValueError('H3 镜头结构无效：时间码必须严格递增')
            previous_time = current_time
            body = body[timestamp.end():]
            body = re.sub(r'^(?:(?:the\s+camera\s+)?(?:cuts?|cut)\s+to\s+)', '', body, flags=re.I)
            if body:
                body = body[0].upper() + body[1:]
        shots.append(_clean_h3_shot(body))
    return shots


def _clean_h3_shot(value: str) -> str:
    value = _picture_to_v2(value)
    value = re.sub(
        r'\(S\d+\)\s+says\s+in\s+an\s+off-screen\s+voiceover\s*:?\s*',
        '',
        value,
        flags=re.I,
    )
    value = re.sub(r'<d>\s*(?:\[[^\]\r\n]+\]\s*)?(.*?)\s*</d>', lambda match: match.group(1), value,
                   flags=re.I | re.S)
    lines = [line.rstrip() for line in value.strip().splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines)


def h3_to_v2(prompt_text: str) -> str:
    sections = _parse_h3(prompt_text)
    shots = _h3_shots(sections['detailed_description'])
    blocks = ['【分镜】', '', f"人物站位：{_subject_position(sections['subject_definitions'])}"]
    for index, shot in enumerate(shots, 1):
        blocks.extend(('', f'镜头{index}：{shot}'))
    blocks.extend((
        '', f"环境音：{_picture_to_v2(sections['overall_soundscape'])}",
        '', '【禁止项】', _V2_FORBIDDEN,
        '', '【强制声明】',
        '原始概要：' + _picture_to_v2(sections['summary']),
        '主体保留：' + _picture_to_v2(sections['retention_analysis']),
        '配乐要求：' + _picture_to_v2(sections['non_diegetic_music']),
    ))
    return '\n'.join(blocks)


def _strip_v2_envelope(prompt_text: str) -> list[str]:
    lines = strip_v2_declaration(prompt_text).strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise ValueError('V2 结构无效：缺少人物站位和镜头')
    if lines[0].strip() == '【分镜】':
        lines.pop(0)
    elif not re.match(r'^\s*人物站位\s*：', lines[0]):
        raise ValueError('V2 结构无效：必须从【分镜】或人物站位开始，可选首行声明除外')

    forbidden = [index for index, line in enumerate(lines) if line.strip() == '【禁止项】']
    required = [index for index, line in enumerate(lines) if line.strip() == '【强制声明】']
    if len(forbidden) != 1 or len(required) != 1 or forbidden[0] >= required[0]:
        raise ValueError('V2 结构无效：缺少唯一且有序的【禁止项】与【强制声明】')
    if any(line.strip() for line in lines[required[0] + 1:]) is False:
        raise ValueError('V2 结构无效：【强制声明】内容不能为空')
    return lines[:forbidden[0]]


def _parse_v2(prompt_text: str) -> tuple[str, list[str], str]:
    body = _strip_v2_envelope(prompt_text)
    index = 0
    while index < len(body) and not body[index].strip():
        index += 1
    if index >= len(body):
        raise ValueError('V2 结构无效：缺少人物站位和镜头')
    station_match = re.match(r'^\s*人物站位\s*：\s*(.*)$', body[index])
    if not station_match or not station_match.group(1).strip():
        raise ValueError('V2 结构无效：缺少非空的人物站位')
    station = station_match.group(1).strip()
    index += 1

    shots: list[list[str]] = []
    numbers = []
    sound_lines: list[str] = []
    voice_lines: list[str] = []
    mode = 'shots'
    while index < len(body):
        line = body[index]
        index += 1
        if not line.strip():
            continue
        sound_match = _SOUND_RE.match(line)
        if sound_match:
            if not shots or sound_lines:
                raise ValueError('V2 结构无效：环境音字段重复或位置无效')
            mode = 'sound'
            if sound_match.group(1).strip():
                sound_lines.append(sound_match.group(1).rstrip())
            continue
        if mode == 'voices':
            voice_lines.append(line.rstrip())
            continue
        shot_match = _V2_SHOT_RE.match(line)
        if shot_match:
            if mode != 'shots':
                raise ValueError('V2 镜头结构无效：镜头不能出现在声线或环境音之后')
            number = int(shot_match.group(1))
            content = shot_match.group(2).rstrip()
            if not content:
                raise ValueError(f'V2 镜头结构无效：镜头{number}不能为空')
            numbers.append(number)
            shots.append([content])
            continue
        if _VOICE_RE.match(line):
            if not shots or mode == 'sound':
                raise ValueError('V2 结构无效：声线块位置无效')
            mode = 'voices'
            voice_lines.append(line.rstrip())
            continue
        if mode == 'shots' and shots:
            shots[-1].append(line.rstrip())
        elif mode == 'sound':
            sound_lines.append(line.rstrip())
        else:
            raise ValueError(f'V2 结构无效：存在无法归属且可能丢失的内容：{line.strip()}')

    if not shots:
        raise ValueError('V2 镜头结构无效：至少需要镜头1')
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError('V2 镜头结构无效：镜头编号必须从 1 连续递增且不能重复')
    return station, ['\n'.join(lines).strip() for lines in shots], '\n'.join(sound_lines + voice_lines).strip() or 'N/A'


def _timestamp_milliseconds(index: int, shot_count: int, duration: float | None) -> int:
    if duration is None:
        return index * 3000
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('时长必须是大于 0 的有限数字')
    total = int(round(float(duration) * 1000))
    step = total // shot_count
    if step < 1:
        raise ValueError('时长过短，无法为所有镜头生成严格递增的毫秒时间码')
    return index * step


def _format_timestamp(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f'{minutes:02d}:{seconds:02d}.{millis:03d}'


def _sentence(value: str) -> str:
    value = value.strip()
    if value and not re.search(r'[.?!。！？][\"\'”’》】）)]*$', value):
        return value + '.'
    return value


def v2_to_h3(prompt_text: str, duration: float | None = None) -> str:
    station, raw_shots, sound = _parse_v2(prompt_text)
    station = _picture_to_h3(station)
    shots = [_picture_to_h3(shot) for shot in raw_shots]
    sound = _picture_to_h3(sound)
    summary = ' '.join(re.sub(r'\s+', ' ', shot).strip() for shot in shots)
    detailed = []
    # Keep custom negative prompts and mandatory declarations verbatim within
    # a standard H3 section. Previously these were silently discarded.
    source = strip_v2_declaration(prompt_text)
    constraints = source[re.search(r'(?m)^\s*【禁止项】\s*$', source).start():].strip()
    for index, shot in enumerate(shots):
        if index == 0:
            detailed.append(f'[Shot 1] {shot}')
        else:
            timestamp = _format_timestamp(_timestamp_milliseconds(index, len(shots), duration))
            detailed.append(f'[Shot {index + 1}] At {timestamp}, {shot}')
    return '\n\n'.join((
        f'subject_definitions:\n<Subject 1> is {_sentence(station)}',
        f'summary:\n[reference generation] {_sentence(summary)}',
        'retention_analysis:\n<Subject 1>: fully_preserved\n' + _picture_to_h3(constraints),
        'detailed_description:\n' + '\n'.join(detailed),
        f'overall_soundscape:\n{sound}',
        'non_diegetic_music:\nN/A',
    ))


def model_format(model: str, catalog=None) -> str:
    family = family_for(model, catalog)
    if family == 'MiniMax-H3':
        return 'H3'
    if family in {'video-v2', 'video-v3'}:
        return 'V2'
    return 'text'


def convert_for_model(
    prompt_text: str,
    model: str,
    duration: float | None = None,
    enabled: bool = True,
    catalog=None,
) -> ConversionResult:
    text = strip_v2_declaration(prompt_text)
    source = detect_format(text)
    target = model_format(model, catalog)
    if not enabled or source == 'text' or target == 'text' or source == target:
        return ConversionResult(text, source, target, False, ())
    if source == 'H3' and target == 'V2':
        converted = h3_to_v2(text)
    elif source == 'V2' and target == 'H3':
        converted = v2_to_h3(text, duration)
    else:
        return ConversionResult(text, source, target, False, ())
    shot_count = len(_h3_shots(_parse_h3(text)['detailed_description'])) if source == 'H3' else len(_parse_v2(text)[1])
    return ConversionResult(converted, source, target, True, (), shot_count)


__all__ = [
    'ConversionResult',
    'convert_for_model',
    'detect_format',
    'h3_to_v2',
    'model_format',
    'strip_v2_declaration',
    'v2_to_h3',
]
