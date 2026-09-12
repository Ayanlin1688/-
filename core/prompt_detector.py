"""Conservative format detection; ambiguous prompts return an empty model ID."""
from pathlib import Path
import os
import re

H3 = 'MiniMax-H3'
SEEDANCE = 'video-v2'
GROK = 'grok-imagine-1.5-video'
H3_SIGNATURES = (
    r'\bsubject_definitions\s*:', r'\bretention_analysis\s*:',
    r'\bdetailed_description\s*:', r'\[\s*Shot\s+[12]\s*\]',
    r'\(S[12]\)\s+says\b', r'\boverall_soundscape\s*:',
    r'\bnon_diegetic_music\s*:', r'<\s*Subject\s+[12]\s*>',
)
SOURCE_TEXT = {'auto': '自动识别', 'manual': '手动强制', 'fallback': '默认回退',
               'workspace': '工作台选择', 'pool': '模型池', 'history': '历史任务'}


def detect_model(prompt_text) -> str:
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        return ''
    text = prompt_text.strip()
    score = sum(bool(re.search(pattern, text, re.I)) for pattern in H3_SIGNATURES)
    if score >= 2:
        return H3
    if score:
        # Incomplete H3 must not be mistaken for English Grok or simple prose.
        return ''
    if re.search(r'【(?:分镜|禁止项|强制声明)】|人物站位\s*：|镜头\s*[一二三四五六七八九十百\d]+|第[一二三四五六七八九十百\d]+镜|@(?:图片|图)\s*\d+', text):
        return SEEDANCE
    structured = bool(re.search(r'^\s*[\w\u4e00-\u9fff-]{2,40}\s*[:：]|[{}]|\[\s*Shot\s*\d+|\bShot\s+\d+', text, re.I | re.M))
    if structured:
        return ''
    han = len(re.findall(r'[\u4e00-\u9fff]', text))
    latin = len(re.findall(r'[A-Za-z]', text))
    words = re.findall(r'[A-Za-z]+', text)
    # A filename fragment or a single generic word has insufficient evidence.
    if len(text) < 200 and (han >= 2 or (len(words) >= 3 and latin >= 12)):
        return 'video-v3'
    letters = sum(character.isalpha() for character in text)
    if letters and latin / letters > .8 and latin >= 12:
        return GROK
    if han >= 2 and han / max(1, letters) >= .5:
        return SEEDANCE
    return ''


def annotate_tasks(tasks, config):
    """Use the same per-file resolution at scan and batch startup; keep images intact."""
    settings = config.get('prompt_detection', {})
    enabled = settings.get('enabled', False)
    workspace = config['workspace']['model']
    overrides = {os.path.normcase(str(Path(path).resolve())): model
                 for path, model in config.get('model_overrides', {}).items()}
    for task in tasks:
        try:
            detected = detect_model(Path(task['prompt_path']).read_text(encoding='utf-8-sig'))
            task.pop('model_detection_error', None)
        except (OSError, UnicodeError) as error:
            detected = ''
            task['model_detection_error'] = f'提示词模型识别失败：{error}'
        manual = overrides.get(os.path.normcase(str(Path(task['prompt_path']).resolve())), '')
        if enabled and manual:
            model, source = manual, 'manual'
        elif enabled and detected:
            model, source = detected, 'auto'
        elif enabled:
            model, source = settings.get('fallback_model') or workspace, 'fallback'
        else:
            model, source = workspace, 'workspace'
        task.update(detected_model=detected, requested_model=model, model=model,
                    model_source=source, model_locked=source == 'manual')
    return tasks


def short_model_name(model):
    if model == H3:
        return 'H3'
    if model == SEEDANCE:
        return 'Seedance'
    return model
