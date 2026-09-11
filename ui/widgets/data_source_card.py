"""Data source directories card."""

from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget, QSizePolicy
from PyQt5.QtCore import pyqtSignal
from pathlib import Path

from ..components.custom_widgets import CaptionLabel, LineEdit, PushButton, StrongBodyLabel, TransparentToolButton, make_card
from qfluentwidgets import FluentIcon as FIF
from ..components.match_dialog import MatchDialog


class DataSourceCard(QWidget):
    directories_changed = pyqtSignal()
    overrides_changed = pyqtSignal()
    def __init__(self, config_manager, log_callback, parent=None) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.log_callback = log_callback
        self.fields: dict[str, LineEdit] = {}
        self.matches = []
        self.automatic_matches = []
        card = make_card()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(9)
        layout.addWidget(StrongBodyLabel("数据源"))
        labels = [("prompts", "分镜提示词目录"), ("images", "参考图片目录"), ("output", "视频保存目录")]
        for key, text in labels:
            layout.addWidget(CaptionLabel(text))
            row = QHBoxLayout()
            field = LineEdit()
            field.setReadOnly(True)
            field.setPlaceholderText("尚未选择目录")
            field.setText(config_manager.config["paths"].get(key, ""))
            field.setMinimumWidth(0)
            field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            field.setToolTip(field.text())
            self.fields[key] = field
            row.addWidget(field, 1)
            choose = PushButton(FIF.FOLDER, "选择")
            choose.clicked.connect(lambda _=False, k=key, f=field: self._choose(k, f))
            row.addWidget(choose)
            layout.addLayout(row)
        status_row = QHBoxLayout()
        self.status_label = CaptionLabel('请选择提示词与图片目录')
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        match_button = TransparentToolButton(FIF.SEARCH)
        match_button.setToolTip("匹配详情")
        match_button.clicked.connect(self.open_match_dialog)
        status_row.addWidget(match_button)
        layout.addLayout(status_row)

    def _choose(self, key: str, field: LineEdit) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择目录")
        if directory:
            directory = str(Path(directory).resolve())
            field.setText(directory)
            field.setToolTip(directory)
            self.config_manager.update(("paths", key), directory)
            self.log_callback(f"已设置{key}目录：{directory}", "success")
            self.directories_changed.emit()

    def set_matches(self, matches, automatic=None):
        self.matches = matches
        if automatic is not None:
            self.automatic_matches = automatic
        count = sum(bool(t['images']) for t in matches)
        self.status_label.setText(f'匹配：已匹配 {count} 组 / 未匹配 {len(matches)-count} 组')

    def open_match_dialog(self) -> None:
        self.log_callback("打开匹配详情", "info")
        dialog = MatchDialog(self.window(), self.log_callback, self.config_manager, self.matches, self.automatic_matches)
        dialog.saved.connect(self.overrides_changed)
        dialog.exec_()
