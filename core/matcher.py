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


def _prefix_with_number_boundary(longer, shorter):
    """Return whether *shorter* is a safe prefix of *longer*.

    Numeric boundaries matter for series names: ``1`` may match ``1(2)`` but
    must never absorb ``10``.  The check is kept symmetric by
    :func:`prefix_matches`, since either the prompt or image filename can be
    the shorter stem.
    """
    if not longer.startswith(shorter):
        return False
    return not (shorter[-1:].isdecimal() and
                longer[len(shorter):len(shorter) + 1].isdecimal())


def prefix_matches(stem, prefix):
    """Match filename prefixes in either direction with numeric boundaries."""
    return (_prefix_with_number_boundary(stem, prefix) or
            _prefix_with_number_boundary(prefix, stem))


def prompt_number(name, fallback):
    # Parenthesized suffixes are view numbers, never the storyboard series.
    name = re.sub(r'(?:\(\d+\))+$', '', normalized_name(name))
    match = re.search(r'(\d+)$', name) or re.search(r'(\d+)', name)
    return int(match.group(1)) if match else fallback


def image_series_number(stem):
    # Numbered image series use the first numeric group, not a later view
    # number in ``(N)``.  The first group also supports names such as
    # ``图片2.jpg`` and ``玫瑰毯子1(3).jpg``.
    match = re.match(r'(\d+)', stem)
    if match:
        return int(match.group(1))
    match = re.search(r'\d+', stem)
    return int(match.group(0)) if match else None


class StoryboardMatcher:
    def __init__(self, recursive=True, overrides=None, unmatched_policy='跳过并警告'):
        self.recursive = recursive
        self.overrides = overrides or {}
        self.warnings = []
        self.products = []
        self.unmatched_policy = unmatched_policy

    @classmethod
    def from_config(cls, config):
        return cls(config.get('scan_settings', {}).get('recursive', True), config.get('match_overrides', {}),
                   config.get('task_strategy', {}).get('unmatched_prompt', '跳过并警告'))

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
        for prompt in prompts:
            name = Path(prompt).stem
            image_methods = {}
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
                image_methods = {path: method for path in bound}
            else:
                normalized = normalized_name(name)
                number = prompt_number(normalized, None)
                # Priority describes each image's evidence, not an early exit:
                # an exact base must not hide differently named views of it.
                for path in images:
                    if stems[path] == normalized:
                        image_methods[path] = '完全匹配'
                    elif prefix_matches(stems[path], normalized):
                        image_methods[path] = '前缀匹配'
                    elif number is not None and image_series_number(stems[path]) == number:
                        image_methods[path] = f'序号匹配（系列{number}）'
                bound = list(image_methods)
                methods = sorted(set(image_methods.values()), key=lambda value:
                                 0 if value == '完全匹配' else 1 if value == '前缀匹配' else 2)
                method = ' + '.join(methods)
            result.append(dict(prompt_path=prompt, prompt_name=name, images=bound, matched=bool(bound),
                               image_match_methods=image_methods,
                               match_method=method if bound else '未绑定图片'))
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
        selected_product = ''
        selected_image_root = image_root
        selected_image_parent = image_root
        if root_prompts and not product_dirs and image_root is not None:
            candidate = image_root / prompt_root.name
            if candidate.is_dir():
                selected_product = prompt_root.name
                selected_image_root = candidate
            elif normalized_name(image_root.name) == normalized_name(prompt_root.name):
                selected_product = prompt_root.name
                selected_image_parent = image_root.parent
            elif any(directory != prompt_root and directory.is_dir()
                     and self._same_direct_child(directory, prompt_root.parent, directory.name)
                     and (image_root / directory.name).is_dir()
                     and any(p.is_file() and p.suffix.lower() == '.txt' for p in directory.iterdir())
                     for directory in prompt_root.parent.iterdir()):
                # Sibling product folders establish that images is their root.
                # A missing product child must not fall back to another product.
                selected_product = prompt_root.name
                selected_image_root = candidate
            if selected_product:
                self.products = [selected_product]
        if root_prompts:
            groups.append((selected_product, prompt_root, selected_image_root, root_prompts, bool(product_dirs)))
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
            missing_directory = False
            if product and (group_image_root is None or not group_image_root.is_dir()):
                image_issue = f'产品“{product}”缺少同名图片目录'
                missing_directory = True
            elif product and not self._same_direct_child(group_image_root,
                    selected_image_parent if selected_product else image_root, product):
                image_issue = f'产品“{product}”的同名图片目录是越界链接'
            if image_issue:
                as_text = missing_directory and self.unmatched_policy == '仍提交文生视频'
                paused = missing_directory and self.unmatched_policy == '暂停任务'
                self.warnings.append(image_issue + ('，按设置提交文生视频' if as_text else
                                                   '，按设置暂停等待处理' if paused else '，已跳过该产品'))
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
            allowed_root = group_image_root if product_dirs or selected_product else None
            matched = self.match_files(prompts, images, allowed_root, direct_images=direct_images)
            output_subdir = self._safe_product_folder(product, used_subdirs) if product else ''
            task_total = len(matched)
            for task_index, task in enumerate(matched, 1):
                skip_reason = image_issue
                if skip_reason:
                    task.update(images=[], matched=False, match_method='产品图片目录缺失')
                    if missing_directory and self.unmatched_policy in {'仍提交文生视频', '暂停任务'}:
                        skip_reason = ''
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
