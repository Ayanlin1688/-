"""Deterministic Unicode path scanning and storyboard image binding."""
from pathlib import Path
import re
import unicodedata


def normalized_name(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).casefold()


def natural_path_key(path):
    # Tokenize every path component so nested folder 2 precedes folder 10.
    parts = re.split(r'(\d+)', normalized_name(str(Path(path))))
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
        self.warnings = []
        self.products = []

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

    def match_files(self, prompt_files, image_files, allowed_image_root=None, direct_images=False):
        prompts = sorted((str(Path(p).resolve()) for p in prompt_files), key=natural_path_key)
        images = sorted((str(Path(p).resolve()) for p in image_files), key=natural_path_key)
        stems = {path: normalized_name(Path(path).stem) for path in images}
        result = []
        for index, prompt in enumerate(prompts):
            name = Path(prompt).stem
            if prompt in self.overrides:
                bound = []
                for override in self.overrides[prompt]:
                    candidate = Path(override).expanduser().resolve()
                    allowed = True
                    if allowed_image_root is not None:
                        root = Path(allowed_image_root).resolve()
                        allowed = candidate.parent == root if direct_images else root in candidate.parents
                    if allowed:
                        bound.append(str(candidate))
                    else:
                        self.warnings.append(f'已忽略产品目录外的手动图片绑定：{candidate}')
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
        self.warnings = []
        self.products = []
        prompt_value = paths.get('prompts', '')
        if not prompt_value:
            return []
        prompt_root = Path(prompt_value).expanduser().resolve()
        if not prompt_root.is_dir():
            raise ValueError(f'目录不存在：{prompt_root}')
        image_value = paths.get('images', '')
        image_root = Path(image_value).expanduser().resolve() if image_value else None
        if image_root is not None and not image_root.is_dir():
            raise ValueError(f'目录不存在：{image_root}')

        root_prompts = sorted(
            (str(path.resolve()) for path in prompt_root.iterdir()
             if path.is_file() and path.suffix.lower() == '.txt'
             and self._path_in_scope(path, prompt_root, direct=True)),
            key=natural_path_key,
        )
        product_dirs = []
        for directory in prompt_root.iterdir():
            if not directory.is_dir():
                continue
            if not self._same_direct_child(directory, prompt_root, directory.name):
                self.warnings.append(f'已忽略提示词目录外的产品链接：{directory}')
                continue
            entries = directory.rglob('*') if self.recursive else directory.iterdir()
            if any(path.is_file() and path.suffix.lower() == '.txt'
                   and self._path_in_scope(path, directory) for path in entries):
                product_dirs.append(directory)
        product_dirs.sort(key=natural_path_key)
        self.products = [directory.name for directory in product_dirs]

        groups = []
        if root_prompts:
            groups.append(('', prompt_root, image_root, root_prompts, bool(product_dirs)))
        for directory in product_dirs:
            entries = directory.rglob('*') if self.recursive else directory.iterdir()
            prompts = sorted(
                (str(path.resolve()) for path in entries
                 if path.is_file() and path.suffix.lower() == '.txt'
                 and self._path_in_scope(path, directory)),
                key=natural_path_key,
            )
            product_images = image_root / directory.name if image_root is not None else None
            groups.append((directory.name, directory, product_images, prompts, False))

        tasks = []
        group_total = len(groups)
        image_extensions = {'.jpg', '.jpeg', '.png', '.webp'}
        used_subdirs = set()
        for group_index, (product, _, group_image_root, prompts, direct_images) in enumerate(groups, 1):
            image_issue = ''
            if product and (group_image_root is None or not group_image_root.is_dir()):
                image_issue = f'产品“{product}”缺少同名图片目录'
            elif product and not self._same_direct_child(group_image_root, image_root, product):
                image_issue = f'产品“{product}”的同名图片目录是越界链接'
            if image_issue:
                self.warnings.append(image_issue + '，已跳过该产品')
                images = []
            elif group_image_root is None:
                images = []
            else:
                entries = group_image_root.iterdir() if direct_images or not self.recursive else group_image_root.rglob('*')
                images = []
                for path in entries:
                    if not path.is_file() or path.suffix.lower() not in image_extensions:
                        continue
                    if not self._path_in_scope(path, group_image_root, direct=direct_images):
                        self.warnings.append(f'已忽略产品图片目录外的链接：{path}')
                        continue
                    images.append(str(path.resolve()))
                images.sort(key=natural_path_key)
            # Pure flat inputs retain the old recursive scan and external manual
            # selection behavior. Product/mixed inputs keep strict product scopes.
            allowed_root = group_image_root if product_dirs else None
            matched = self.match_files(prompts, images, allowed_root, direct_images=direct_images)
            output_subdir = self._safe_product_folder(product, used_subdirs) if product else ''
            task_total = len(matched)
            for task_index, task in enumerate(matched, 1):
                skip_reason = image_issue
                if skip_reason:
                    task.update(images=[], matched=False, match_method='产品图片目录缺失')
                task.update(
                    product=product,
                    product_index=group_index,
                    product_total=group_total,
                    product_task_index=task_index,
                    product_task_total=task_total,
                    output_subdir=output_subdir,
                    skip_reason=skip_reason,
                )
                tasks.append(task)
        return tasks

    @staticmethod
    def _path_in_scope(path, root, direct=False):
        candidate = Path(path).resolve()
        scope = Path(root).resolve()
        return candidate.parent == scope if direct else scope in candidate.parents

    @staticmethod
    def _same_direct_child(path, root, expected_name):
        if path is None or root is None:
            return False
        candidate = Path(path).resolve()
        scope = Path(root).resolve()
        return candidate.parent == scope and normalized_name(candidate.name) == normalized_name(expected_name)

    @staticmethod
    def _safe_product_folder(product, used):
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', product).strip().rstrip('. ')
        if not value or value in {'.', '..'}:
            value = 'product'
        if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
            value = '_' + value
        value = value[:120]
        candidate = value
        suffix = 2
        while normalized_name(candidate) in used:
            candidate = f'{value}_{suffix}'
            suffix += 1
        used.add(normalized_name(candidate))
        return candidate
