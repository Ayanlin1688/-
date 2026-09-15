"""Compact workspace directory strip; the detailed cards remain in the tool dialog."""
from pathlib import Path
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QSizePolicy
from qfluentwidgets import CardWidget, IconWidget, PushButton, TransparentToolButton, FluentIcon as FIF
from .workspace_surface import ElidedLabel, label, style_button


class DirectoryField(QWidget):
    choose_requested = pyqtSignal()
    def __init__(self, title, icon=FIF.FOLDER, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(7)
        glyph = IconWidget(icon); glyph.setFixedSize(14, 14)
        row.addWidget(glyph)
        row.addWidget(label(title, 12, '#9CA3AF', True)); self.path = ElidedLabel('未选择')
        self.path.setStyleSheet('background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.10); border-radius:8px; padding:4px 8px;')
        row.addWidget(self.path, 1); button = style_button(TransparentToolButton(FIF.FOLDER)); button.setFixedSize(28, 28)
        button.setToolTip('选择目录'); button.clicked.connect(self.choose_requested); row.addWidget(button)

    def set_value(self, text, suffix=''):
        self.path.setFullText((str(text) if text else '未选择') + suffix)


class WorkspaceDirectoryBar(CardWidget):
    choose_requested = pyqtSignal(str)
    match_requested = pyqtSignal()
    def __init__(self, parent=None):
        super().__init__(parent); self.setBorderRadius(10); self.setProperty('studioStyled', True)
        self.setFixedHeight(48)
        row = QHBoxLayout(self); row.setContentsMargins(14, 8, 14, 8); row.setSpacing(14)
        self.fields = {}
        for key, title, icon in [('prompts', '提示词', FIF.DOCUMENT), ('images', '参考图', FIF.PHOTO), ('output', '保存至', FIF.VIDEO)]:
            field = DirectoryField(title, icon); field.choose_requested.connect(lambda k=key: self.choose_requested.emit(k)); self.fields[key] = field
            row.addWidget(field, 1)
        self.match_status = label('✓ 已匹配 0/0', 12, '#22C55E', True); row.addWidget(self.match_status)
        self.match_button = PushButton('匹配详情')
        self.match_button.setToolTip('查看图片与提示词的匹配详情')
        self.match_button.clicked.connect(self.match_requested)
        row.addWidget(self.match_button)
        from ..materials import register_theme_callback
        register_theme_callback(self._apply_mode_style)
        self._apply_mode_style()

    def _apply_mode_style(self):
        try:
            from ..materials import is_light
            if is_light():
                chip = 'background:rgba(15,26,52,0.05); border:1px solid rgba(15,26,52,0.12); border-radius:8px; padding:4px 8px;'
                button_style = ('QPushButton {background:transparent; border:0; color:#3E6FD1; font-size:12px; padding:2px 6px;}'
                                ' QPushButton:hover {color:#2A55AE; background:rgba(15,26,52,0.06); border-radius:6px;}'
                                ' QPushButton:disabled {color:rgba(15,26,52,0.35);}')
            else:
                chip = 'background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.10); border-radius:8px; padding:4px 8px;'
                button_style = ('QPushButton {background:transparent; border:0; color:#8AB4F8; font-size:12px; padding:2px 6px;}'
                                ' QPushButton:hover {color:#BFD2FF; background:rgba(255,255,255,0.06); border-radius:6px;}'
                                ' QPushButton:disabled {color:#55555f;}')
            from PyQt5.QtGui import QColor
            from ..materials import map_text_color
            text_color = map_text_color('#9ca3af')
            name = text_color.name() if isinstance(text_color, QColor) else str(text_color)
            for field in self.fields.values():
                field.path.setStyleSheet(chip)
                field.path.setTextColor(QColor(name), QColor(name))
            self.match_button.setStyleSheet(button_style)
        except RuntimeError:
            pass

    def refresh(self, paths, tasks, metrics=None):
        metrics = metrics or {}
        self.fields['prompts'].set_value(paths.get('prompts'), f'  · {len(tasks)}条')
        self.fields['images'].set_value(paths.get('images'), f'  · {metrics.get("images", 0)}张')
        self.fields['output'].set_value(paths.get('output'), f'  · {metrics.get("videos", 0)}个 · {metrics.get("bytes", 0)/1024**3:.1f}GB')
        matched = sum(bool(t.get('images')) for t in tasks); self.match_status.setText(f'✓ 已匹配 {matched}/{len(tasks)}')
