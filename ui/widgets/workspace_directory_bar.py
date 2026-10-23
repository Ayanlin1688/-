"""Compact workspace directory strip; the detailed cards remain in the tool dialog."""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QSizePolicy
from qfluentwidgets import IconWidget, TransparentPushButton, FluentIcon as FIF
from core.i18n import tr
from .workspace_surface import ElidedLabel, WorkspaceCard, label
from .status_dot import Dot
from ..responsive import WrapLayout


class DirectoryCard(WorkspaceCard):
    """单个目录信息卡：图标方块 + 两行文字 + 下拉指示（点击整卡选择目录）。"""

    choose_requested = pyqtSignal()

    def __init__(self, title, icon=FIF.FOLDER, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(56)
        self.setMinimumWidth(190)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self); row.setContentsMargins(10, 8, 10, 8); row.setSpacing(10)
        self.glyph_host = QWidget(); self.glyph_host.setFixedSize(30, 30)
        g = QVBoxLayout(self.glyph_host); g.setContentsMargins(0, 0, 0, 0)
        glyph = IconWidget(icon); glyph.setFixedSize(16, 16)
        g.addWidget(glyph, 0, Qt.AlignCenter)
        row.addWidget(self.glyph_host)
        col = QVBoxLayout(); col.setContentsMargins(0, 0, 0, 0); col.setSpacing(1)
        self.title = label(title, 11, '#9CA3AF')
        col.addWidget(self.title)
        self.path = ElidedLabel(tr('未选择'))
        col.addWidget(self.path)
        row.addLayout(col, 1)
        row.addWidget(label('▾', 12, '#9CA3AF'))

    def mouseReleaseEvent(self, event):
        try:
            self.choose_requested.emit()
        except RuntimeError:
            pass
        super().mouseReleaseEvent(event)

    def set_value(self, text, suffix=''):
        self.path.setFullText(str(text) if text else tr('未选择'), suffix)


class WorkspaceDirectoryBar(QWidget):
    choose_requested = pyqtSignal(str)
    match_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        row = WrapLayout(self, spacing=10)
        self._tints = {'prompts': '74,141,255', 'images': '139,92,246', 'output': '52,199,89'}
        self.fields = {}
        for key, title, icon in [('prompts', tr('提示词'), FIF.DOCUMENT),
                                 ('images', tr('参考图'), FIF.PHOTO),
                                 ('output', tr('保存至'), FIF.VIDEO)]:
            field = DirectoryCard(title, icon)
            field.choose_requested.connect(lambda k=key: self.choose_requested.emit(k))
            self.fields[key] = field
            row.addWidget(field, 1)
        match_host = QWidget()
        match_row = QHBoxLayout(match_host)
        match_row.setContentsMargins(0, 0, 0, 0)
        self.match_dot = Dot(8, '#22C55E')
        match_row.addWidget(self.match_dot)
        self.match_status = label(f"{tr('已匹配')} 0/0", 12, '#22C55E', True)
        match_row.addWidget(self.match_status)
        self.match_button = TransparentPushButton(FIF.SEARCH, tr('匹配详情'))
        self.match_button.setMinimumSize(118, 32)
        self.match_button.setToolTip(tr('查看每个提示词绑定的参考图'))
        self.match_button.clicked.connect(self.match_requested)
        match_row.addWidget(self.match_button)
        row.addWidget(match_host)
        from ..materials import register_theme_callback
        register_theme_callback(self._apply_mode_style)
        self._apply_mode_style()

    def _apply_mode_style(self):
        try:
            from PyQt5.QtGui import QColor
            from ..materials import is_light, map_text_color
            light = is_light()
            alpha = 0.18 if light else 0.26
            for key, field in self.fields.items():
                rgb = self._tints.get(key, '74,141,255')
                field.glyph_host.setStyleSheet(f'background:rgba({rgb},{alpha}); border-radius:9px;')
                tone = map_text_color('#9ca3af')
                name = tone.name() if isinstance(tone, QColor) else str(tone)
                field.path.setTextColor(QColor(name), QColor(name))
        except RuntimeError:
            pass

    def refresh(self, paths, tasks, metrics=None):
        metrics = metrics or {}
        self.fields['prompts'].set_value(paths.get('prompts'), f'  · {len(tasks)}条')
        self.fields['images'].set_value(paths.get('images'), f'  · {metrics.get("images", 0)}张')
        self.fields['output'].set_value(paths.get('output'), f'  · {metrics.get("videos", 0)}个 · {metrics.get("bytes", 0)/1024**3:.1f}GB')
        matched = sum(bool(t.get('images')) for t in tasks)
        self.match_status.setText(f"{tr('已匹配')} {matched}/{len(tasks)}")
        from PyQt5.QtGui import QColor
        from ..materials import is_light
        # 整行统一一种语义色：全匹配=绿、部分匹配=琥珀、无任务=灰。
        if not tasks:
            tone = '#8B93A3'
        elif matched == len(tasks):
            tone = '#1E8A4F' if is_light() else '#22C55E'
        else:
            tone = '#9C6B0A' if is_light() else '#E5B94E'
        self.match_status.setTextColor(QColor(tone), QColor(tone))

    def set_busy(self, busy):
        busy = bool(busy)
        reason = tr('队列、匹配扫描或下载进行中，完成后可选择目录或查看匹配详情')
        for field in self.fields.values():
            field.setEnabled(not busy)
            field.setToolTip(reason if busy else tr('选择目录'))
        self.match_button.setEnabled(not busy)
        self.match_button.setToolTip(reason if busy else tr('查看每个提示词绑定的参考图'))
