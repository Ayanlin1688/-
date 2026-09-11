"""Deterministic Unicode path scanning and storyboard image binding."""
from pathlib import Path
import re
import unicodedata


def normalized_name(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).casefold()


def natural_path_key(path):
    # Tokenize complete numbers: 1(2) precedes 1(10), regardless of spaces.
    parts = re.split(r'(\d+)', normalized_name(Path(path).name))
    tokens = tuple((0, int(part)) if part.isdecimal() else (1, part) for part in parts)
    return tokens, str(path).casefold(), str(path)


def prefix_matches(stem, prefix):
    if not stem.startswith(prefix):
        return False
    # A prompt ending in series 1 must never absorb series 10.
    return not (prefix[-1:].isdecimal() and stem[len(prefix):len(prefix)+1].isdecimal())


def prompt_number(name, fallback):
    match = re.search(r'(\d+)$', name) or re.match(r'(\d+)', name)
    return int(match.group(1)) if match else fallback


def image_series_number(stem):
    # Numbered image series use the leading group, not the view number in (N).
    match = re.match(r'(\d+)', stem)
    return int(match.group(1)) if match else None


class StoryboardMatcher:
    def __init__(self, recursive=True, overrides=None):
        self.recursive = recursive
        self.overrides = overrides or {}

    @classmethod
    def from_config(cls, config):
        return cls(config.get('scan_settings', {}).get('recursive', True), config.get('match_overrides', {}))

    def scan_directories(self, prompt_dir, image_dir):
        def scan(directory, extensions):
            if not directory:
                return []
            root = Path(directory).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f'目录不存在：{root}')
            entries = root.rglob('*') if self.recursive else root.iterdir()
            return sorted((str(p.resolve()) for p in entries if p.is_file() and p.suffix.lower() in extensions),
                          key=natural_path_key)
        return scan(prompt_dir, {'.txt'}), scan(image_dir, {'.jpg', '.jpeg', '.png', '.webp'})

    def match_files(self, prompt_files, image_files):
        prompts = sorted((str(Path(p).resolve()) for p in prompt_files), key=natural_path_key)
        images = sorted((str(Path(p).resolve()) for p in image_files), key=natural_path_key)
        stems = {path: normalized_name(Path(path).stem) for path in images}
        result = []
        for index, prompt in enumerate(prompts):
            name = Path(prompt).stem
            if prompt in self.overrides:
                bound = [str(Path(p).resolve()) for p in self.overrides[prompt]]
                method = '手动绑定'
            else:
                normalized = normalized_name(name)
                bound = [p for p in images if stems[p] == normalized]
                method = '完全匹配'
                if not bound:
                    bound = [p for p in images if prefix_matches(stems[p], normalized)]
                    method = '前缀匹配'
                if not bound:
                    number = prompt_number(normalized, index + 1)
                    bound = [p for p in images if image_series_number(stems[p]) == number]
                    method = f'序号匹配（系列{number}）'
            result.append(dict(prompt_path=prompt, prompt_name=name, images=bound, matched=bool(bound), match_method=method if bound else '未绑定图片'))
        return result

    def scan_and_match(self, paths):
        return self.match_files(*self.scan_directories(paths.get('prompts', ''), paths.get('images', '')))
