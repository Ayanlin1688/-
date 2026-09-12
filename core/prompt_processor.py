"""Normalize reference markers without changing the rest of the prompt."""
import re

_REFERENCE = re.compile(r'<\s*(?:Picture|图片)\s*(\d+)\s*>|@(?:Image|图片|参考图|图)\s*(\d+)', re.IGNORECASE)


def process_prompt(prompt_text):
    # Replace each complete numeric token atomically: Picture10 can never be
    # partially consumed by a replacement for Picture1, regardless of order.
    return _REFERENCE.sub(lambda m: '@参考图' + (m.group(1) or m.group(2)), prompt_text).strip()


def reference_warnings(prompt_text, image_count):
    numbers = {int(match.group(1) or match.group(2)) for match in _REFERENCE.finditer(prompt_text)}
    warnings = [f'提示词引用了Picture {number}，但只绑定了{image_count}张图，可能影响生成质量'
                for number in sorted(numbers) if number > image_count]
    if 0 in numbers:
        warnings.append('图片引用编号从Picture 1开始，Picture 0无效')
    remaining = re.findall(r'<\s*(?:Picture|图片)[^>\n]*(?:>|$)', process_prompt(prompt_text), re.IGNORECASE)
    if remaining:
        warnings.append('替换后仍有未识别的图片标记：' + '、'.join(remaining))
    return warnings
