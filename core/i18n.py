"""极简运行时多语言：中文为源语言；英文映射表在 i18n_catalog*.py。

- tr('设置')：按当前语言返回译文；未收录的字符串自动回退原文（渐进翻译，不阻塞）。
- set_language() 在启动与设置切换时调用；语言实际变化时通过 LanguageBus 广播新语言码。
- register_retranslate(widget)：把控件登记到语言总线；语言变化时调用其
  retranslate_ui()（弱引用登记，控件销毁后自动失效，不会抛异常）。
- translate_text()：双向文本翻译（中↔英），支持 `{name}` 占位模板的整句匹配，
  供界面遍历重翻工具（ui/i18n_helpers.py）与动态状态文案使用。
- 新增界面代码直接写 tr('中文原文')，即可被后续翻译覆盖，无需改动调用点。
"""
from __future__ import annotations

import re
import weakref

try:  # 纯函数测试可能只导入本模块：QtCore 导入不依赖 QApplication。
    from PyQt5.QtCore import QObject, pyqtSignal
    _QT_CORE_AVAILABLE = True
except Exception:  # pragma: no cover - PyQt5 是运行期硬依赖，这里仅做导入容错
    QObject = object  # type: ignore[assignment]
    pyqtSignal = None  # type: ignore[assignment]
    _QT_CORE_AVAILABLE = False

from .i18n_catalog import EN as EN_BASE

# 中文为源语言；英文译文按模块合并（workspace / ecommerce 由其它模块维护，
# 文件尚不存在时保持基础词表，导入失败不得影响主程序）。
EN = dict(EN_BASE)
try:
    from .i18n_catalog_workspace import EN_WORKSPACE
    EN.update(EN_WORKSPACE)
except ImportError:
    pass
try:
    from .i18n_catalog_ecommerce import EN_ECOMMERCE
    EN.update(EN_ECOMMERCE)
except ImportError:
    pass

CURRENT = 'zh-CN'

_CJK = re.compile(r'[\u4e00-\u9fff]')
_PLACEHOLDER = re.compile(r'\{[^{}]*\}')


def normalize(code) -> str:
    return 'en-US' if str(code or '').lower().startswith('en') else 'zh-CN'


def current_language() -> str:
    return CURRENT


def tr(text: str) -> str:
    if CURRENT == 'en-US':
        return EN.get(text, text)
    return text


# ---------------------------------------------------------------------------
# 双向翻译（含 {占位符} 模板整句匹配）
# ---------------------------------------------------------------------------
def _build_reverse(catalog):
    """英文译文 → 中文原文；只收含中文的源串，避免把原生英文文案反向翻译。"""
    reverse = {}
    for source, target in catalog.items():
        if (isinstance(source, str) and isinstance(target, str) and source and target
                and source != target and _CJK.search(source)):
            reverse.setdefault(target, source)
    return reverse


def _template_regex(template):
    pieces = []
    count = 0
    last = 0
    for match in _PLACEHOLDER.finditer(template):
        pieces.append(re.escape(template[last:match.start()]))
        pieces.append('(.+?)')
        count += 1
        last = match.end()
    if not count:
        return None
    pieces.append(re.escape(template[last:]))
    try:
        return re.compile('^' + ''.join(pieces) + '$', re.DOTALL)
    except re.error:  # pragma: no cover - 防御异常模板
        return None


def _build_templates(catalog, reverse=False):
    """返回按源串长度降序排列的模板：[(源长度, 正则, 目标串), ...]。"""
    entries = []
    for source, target in catalog.items():
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if not source or not target or source == target:
            continue
        if reverse:
            # 输入是英文译文（可含占位符），输出中文原文。
            if not _CJK.search(source):
                continue
            english, chinese = target, source
        else:
            if not _CJK.search(source):
                continue
            english, chinese = source, target
        if _PLACEHOLDER.search(english) is None:
            continue
        regex = _template_regex(english)
        if regex is None:
            continue
        entries.append((len(english), regex, chinese))
    entries.sort(key=lambda entry: -entry[0])
    return entries


def _render_template(template, values):
    result = []
    iterator = iter(values)
    last = 0
    for match in _PLACEHOLDER.finditer(template):
        result.append(template[last:match.start()])
        try:
            result.append(str(next(iterator)))
        except StopIteration:
            result.append(match.group(0))
        last = match.end()
    result.append(template[last:])
    return ''.join(result)


def _match_templates(text, entries, translate_values=False):
    for _length, regex, target in entries:
        match = regex.match(text)
        if match is not None:
            values = match.groups()
            if translate_values:
                values = tuple(translate_text(value, _depth=1) for value in values)
            return _render_template(target, values)
    return None


REVERSE_EN = _build_reverse(EN)
_TEMPLATES_FORWARD = _build_templates(EN, reverse=False)
_TEMPLATES_REVERSE = _build_templates(EN, reverse=True)


def translate_text(text, _depth=0):
    """把一段界面文本翻到当前语言（幂等；无法识别时原样返回）。

    支持：精确词条、英文回译、`{name}` 占位模板整句替换；模板捕获到的占位值
    会再浅层翻译一次（例如 `{capability}线路：{host}` 里的中文能力名）。
    不处理用户数据：只有精确命中词条或模板的文本才会变化。
    """
    if not isinstance(text, str) or not text:
        return text
    if CURRENT == 'en-US':
        if _CJK.search(text):
            translated = EN.get(text)
            if translated is not None and translated != text:
                return translated
            rendered = _match_templates(text, _TEMPLATES_FORWARD, translate_values=_depth < 1)
            return text if rendered is None else rendered
        source = REVERSE_EN.get(text)
        if source is not None:
            translated = EN.get(source)
            if translated is not None:
                return translated
        rendered = _match_templates(text, _TEMPLATES_REVERSE)
        return text if rendered is None else rendered
    # 中文模式：英文界面文案回译中文；中文源文案保持原样。
    if _CJK.search(text):
        return text
    source = REVERSE_EN.get(text)
    if source is not None:
        return source
    rendered = _match_templates(text, _TEMPLATES_REVERSE)
    return text if rendered is None else rendered


# ---------------------------------------------------------------------------
# 语言总线
# ---------------------------------------------------------------------------
if _QT_CORE_AVAILABLE:

    class LanguageBus(QObject):
        """语言总线：语言实际变化时发射新语言码（同语言重复设置不发射）。"""

        language_changed = pyqtSignal(str)

else:  # pragma: no cover - 仅用于完全没有 PyQt5 的纯函数环境

    class _FallbackSignal:
        def __init__(self):
            self._slots = []

        def connect(self, slot):
            if slot not in self._slots:
                self._slots.append(slot)

        def emit(self, value):
            for slot in list(self._slots):
                try:
                    slot(value)
                except Exception:
                    pass

    class LanguageBus:
        """语言总线（无 Qt 降级实现，接口与 QObject 版本一致）。"""

        def __init__(self):
            self.language_changed = _FallbackSignal()


_BUS = None
_REGISTERED = weakref.WeakSet()
_BUS_CONNECTED = False


def language_bus() -> LanguageBus:
    """返回全局语言总线单例。"""
    global _BUS
    if _BUS is None:
        _BUS = LanguageBus()
    return _BUS


def _dispatch_retranslate(_language):
    for widget in list(_REGISTERED):
        try:
            widget.retranslate_ui()
        except Exception:
            # 单个控件重翻失败不应影响其它界面，也不应打断语言切换。
            pass


def _install_bus_dispatcher():
    global _BUS_CONNECTED
    if _BUS_CONNECTED:
        return
    try:
        language_bus().language_changed.connect(_dispatch_retranslate)
        _BUS_CONNECTED = True
    except Exception:  # pragma: no cover - 信号连接失败时退化为无总线
        pass


def register_retranslate(widget) -> bool:
    """登记控件到语言总线：语言变化时调用 widget.retranslate_ui()。

    使用弱引用集合保存，控件被销毁后不保留引用；没有 retranslate_ui 的控件忽略。
    重复登记同一控件不会重复调用（集合天然去重）。
    """
    if widget is None or not callable(getattr(widget, 'retranslate_ui', None)):
        return False
    try:
        _REGISTERED.add(widget)
    except TypeError:  # pragma: no cover - 不支持弱引用的对象
        return False
    _install_bus_dispatcher()
    return True


def registered_retranslate_count() -> int:
    """当前登记到语言总线的控件数量（用于测试/诊断，弱引用失效后自动减少）。"""
    return len(_REGISTERED)


def set_language(code) -> str:
    global CURRENT
    normalized = normalize(code)
    changed = normalized != CURRENT
    CURRENT = normalized
    if changed:
        try:
            language_bus().language_changed.emit(normalized)
        except Exception:  # pragma: no cover - 广播异常不得中断语言切换
            pass
    return normalized
