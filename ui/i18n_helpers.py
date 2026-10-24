"""语言切换时重翻控件树的公共工具。

页面/对话框实现 ``retranslate_ui()`` 时只需调用 ``retranslate_tree(self)``：
遍历子树中所有静态文本控件，把它们从「源语言文本」重新翻译到当前语言。

关键设计：每个控件第一次被遍历时记录其源语言文本（动态属性 ``_i18n_source_*``）。
源语言固定为中文（或开发期有意保留的英文原文，例如语言选项 English），
切到英文时正向翻译，切回中文时直接还原源文本。这样：
- 反复切换幂等，不会把英文译文二次翻译；
- 程序后续 setText 更新的动态文案会被识别为新源文本并重新记录；
- 用户输入内容（QLineEdit 正文）与表格数据单元格永不改动。

覆盖：QLabel / QAbstractButton / 分组标题 / 占位符 / QComboBox 选项（业务值在
itemData 中不受影响；模型选择器由页面重建）/ 表头 / SettingCard 标题与说明 /
窗口标题 / tooltip。
"""
from __future__ import annotations

import re

from PyQt5.QtCore import QSignalBlocker
from PyQt5.QtWidgets import (
    QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit, QPlainTextEdit,
    QTableWidget, QTextEdit, QWidget,
)

from core.i18n import REVERSE_EN, current_language, translate_text

_CJK = re.compile(r'[\u4e00-\u9fff]')

_SOURCE = '_i18n_source_text'
_RENDERED = '_i18n_rendered_text'
_ITEM_SOURCES = '_i18n_item_sources'


# ---------------------------------------------------------------------------
# 源文本记录与本地化
# ---------------------------------------------------------------------------
def _infer_source(current: str) -> str:
    """在没有记录时推断文本的源语言形态。"""
    if not isinstance(current, str) or not current:
        return current
    if _CJK.search(current):
        return current
    # 纯英文文本：如果它是某个中文词条的译文，说明它来自一次翻译，还原为中文源文。
    # 有意保留的英文原文（如语言选项 English、API Base URL）不在英文译文表中，
    # 或由 register_item_sources() 显式登记源文，因此不会被误回译。
    return REVERSE_EN.get(current, current)


def _localize_source(source: str) -> str:
    if not isinstance(source, str) or not source:
        return source
    if current_language() == 'zh-CN':
        return source
    return translate_text(source)


def _remember_source(widget, source_attr: str, rendered_attr: str, current: str) -> str:
    source = getattr(widget, source_attr, None)
    rendered = getattr(widget, rendered_attr, None)
    # 代码在两次语言切换之间更新过文案：把当前文本当成新的源文本重新记录。
    if not isinstance(source, str) or (isinstance(rendered, str) and current != rendered):
        source = _infer_source(current)
        try:
            setattr(widget, source_attr, source)
        except Exception:
            pass
    return source


def _apply_text(widget, text: str, setter, source_attr: str, rendered_attr: str) -> None:
    if not isinstance(text, str) or not text:
        return
    source = _remember_source(widget, source_attr, rendered_attr, text)
    translated = _localize_source(source)
    if not translated or translated == text:
        return
    try:
        setter(translated)
        setattr(widget, rendered_attr, translated)
    except Exception:
        pass


def _apply_placeholder(widget) -> None:
    try:
        text = widget.placeholderText()
    except AttributeError:
        return
    _apply_text(widget, text, widget.setPlaceholderText, '_i18n_placeholder_source',
                '_i18n_placeholder_rendered')


def _apply_tooltip(widget) -> None:
    try:
        text = widget.toolTip()
    except Exception:
        return
    _apply_text(widget, text, widget.setToolTip, '_i18n_tooltip_source',
                '_i18n_tooltip_rendered')


# ---------------------------------------------------------------------------
# 各控件类型
# ---------------------------------------------------------------------------
def _is_setting_card(widget) -> bool:
    return (hasattr(widget, 'titleLabel') and hasattr(widget, 'contentLabel')
            and hasattr(widget, 'setTitle'))


def _setting_card_text(card, label) -> str:
    """响应式卡片会用省略号覆盖 titleLabel.text()，完整源串缓存在 _card_label_texts。"""
    texts = getattr(card, '_card_label_texts', None)
    if isinstance(texts, dict):
        try:
            full = texts.get(label)
        except TypeError:
            full = None
        if isinstance(full, str) and full:
            return full
    return label.text()


def _retranslate_setting_card(card) -> None:
    for attribute, setter_name, source_attr in (
            ('titleLabel', 'setTitle', '_i18n_title_source'),
            ('contentLabel', 'setContent', '_i18n_content_source')):
        label = getattr(card, attribute, None)
        if not isinstance(label, QLabel):
            continue
        text = _setting_card_text(card, label)
        if not text:
            continue
        source = _remember_source(card, source_attr, source_attr + '_rendered', text)
        translated = _localize_source(source)
        if translated == text or not translated:
            continue
        setter = getattr(card, setter_name, None)
        try:
            if callable(setter):
                setter(translated)
            else:  # pragma: no cover - 防御老版本库
                label.setText(translated)
            setattr(card, source_attr + '_rendered', translated)
        except Exception:
            try:
                label.setText(translated)
            except Exception:
                pass


def _is_combo_widget(widget) -> bool:
    """QFluentWidgets 的 ComboBox 不是 QComboBox（自绘 QPushButton/LineEdit），用鸭子类型识别。"""
    return (isinstance(widget, QComboBox)
            or (hasattr(widget, 'itemText') and hasattr(widget, 'setItemText')
                and hasattr(widget, 'currentIndex') and hasattr(widget, 'count')))


def _retranslate_combo(combo) -> None:
    # ModelComboBox 的选项由模型目录生成（ID/能力标签），由页面刷新模型时重建，
    # 这里不逐项翻译，避免破坏 currentData 之外的展示逻辑。
    if hasattr(combo, 'descriptions'):
        _apply_placeholder(combo)
        return
    try:
        count = combo.count()
    except Exception:
        return
    sources = getattr(combo, _ITEM_SOURCES, None)
    if not isinstance(sources, list) or len(sources) != count:
        sources = [None] * count
    blocker = QSignalBlocker(combo)
    try:
        for index in range(count):
            try:
                current = combo.itemText(index)
            except Exception:
                continue
            if not isinstance(current, str) or not current:
                continue
            source = sources[index]
            if not isinstance(source, str) or not source:
                source = _infer_source(current)
                sources[index] = source
            translated = _localize_source(source)
            if translated and translated != current:
                try:
                    combo.setItemText(index, translated)
                except Exception:
                    pass
        try:
            setattr(combo, _ITEM_SOURCES, sources)
        except Exception:
            pass
        _apply_placeholder(combo)
    finally:
        del blocker


def _retranslate_header_items(table, orientation: str, count: int) -> None:
    """表头统一重翻。

    QTableWidgetItem 的 Python 包装每次读取都会重建，动态属性不持久，
    因此源文本缓存挂在 QTableWidget 自身。
    """
    sources_attr = f'_i18n_{orientation}_header_sources'
    rendered_attr = f'_i18n_{orientation}_header_rendered'
    sources = getattr(table, sources_attr, None)
    rendered = getattr(table, rendered_attr, None)
    if not isinstance(sources, list) or len(sources) != count:
        sources = [None] * count
    if not isinstance(rendered, list) or len(rendered) != count:
        rendered = [None] * count
    for index in range(count):
        if orientation == 'horizontal':
            item = table.horizontalHeaderItem(index)
        else:
            item = table.verticalHeaderItem(index)
        if item is None:
            continue
        text = item.text()
        if not isinstance(text, str) or not text:
            continue
        source = sources[index]
        if (not isinstance(source, str) or not source
                or (isinstance(rendered[index], str) and text != rendered[index])):
            source = _infer_source(text)
            sources[index] = source
        translated = _localize_source(source)
        if translated and translated != text:
            try:
                item.setText(translated)
                rendered[index] = translated
            except Exception:
                pass
        else:
            rendered[index] = text
    try:
        setattr(table, sources_attr, sources)
        setattr(table, rendered_attr, rendered)
    except Exception:
        pass


def _retranslate_headers(table) -> None:
    _retranslate_header_items(table, 'horizontal', table.columnCount())
    _retranslate_header_items(table, 'vertical', table.rowCount())


def _retranslate_widget(widget) -> None:
    try:
        if widget.isWindow() and widget.windowTitle():
            _apply_text(widget, widget.windowTitle(), widget.setWindowTitle,
                        '_i18n_window_title_source', '_i18n_window_title_rendered')
    except Exception:
        pass
    if _is_setting_card(widget):
        _retranslate_setting_card(widget)
    try:
        if _is_combo_widget(widget):
            _retranslate_combo(widget)
        elif isinstance(widget, QLineEdit):
            _apply_placeholder(widget)
        elif isinstance(widget, (QTextEdit, QPlainTextEdit)):
            _apply_placeholder(widget)
        elif isinstance(widget, QLabel):
            _apply_text(widget, widget.text(), widget.setText, _SOURCE, _RENDERED)
        elif isinstance(widget, QTableWidget):
            _retranslate_headers(widget)
        elif isinstance(widget, QGroupBox):
            _apply_text(widget, widget.title(), widget.setTitle,
                        '_i18n_group_source', '_i18n_group_rendered')
        elif isinstance(widget, QAbstractButton):
            _apply_text(widget, widget.text(), widget.setText, _SOURCE, _RENDERED)
    except Exception:
        pass
    _apply_tooltip(widget)


def _is_skipped(widget, skip) -> bool:
    if not skip:
        return False
    try:
        return isinstance(widget, tuple(skip))
    except TypeError:  # pragma: no cover - 单类传入
        return isinstance(widget, skip)


def retranslate_tree(root, skip=()) -> int:
    """把 root 及其整棵子树中的静态界面文本重翻到当前语言。

    返回实际遍历到的控件数量（诊断用）。控件级异常单独吞掉，保证切换流程不中断。
    """
    if root is None:
        return 0
    visited = 0
    if not _is_skipped(root, skip):
        _retranslate_widget(root)
        visited += 1
    try:
        children = root.findChildren(QWidget)
    except (AttributeError, RuntimeError):
        return visited
    for widget in children:
        if _is_skipped(widget, skip):
            continue
        _retranslate_widget(widget)
        visited += 1
    return visited


def translate_multiline(text) -> str:
    """逐行翻译多行文案（错误详情 / 状态块），单行失败时保留原文。"""
    if not isinstance(text, str) or not text:
        return text
    return '\n'.join(translate_text(line) for line in text.split('\n'))


def register_item_sources(combo, sources) -> None:
    """登记下拉框的源语言选项文本（构造期已知原文时调用，避免英文原文被回译）。"""
    try:
        combo._i18n_item_sources = list(sources)
    except Exception:
        pass
