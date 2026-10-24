"""Data source directories card."""

from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget, QSizePolicy
from PyQt5.QtCore import Qt, pyqtSignal
from pathlib import Path

from ..components.custom_widgets import CaptionLabel, LineEdit, PushButton, StrongBodyLabel, TransparentToolButton, make_card
from core.i18n import tr
from qfluentwidgets import FluentIcon as FIF
from ..components.match_dialog import MatchDialog
from ..components.studio_dialog import safe_delete_dialog
from ..responsive import WrapLayout


class DataSourceCard(QWidget):
    directories_changed = pyqtSignal()
    overrides_changed = pyqtSignal()
    def __init__(self, config_manager, log_callback, parent=None) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.log_callback = log_callback
        self.fields: dict[str, LineEdit] = {}
        self.choose_buttons = []
        self.matches = []
        self.automatic_matches = []
        self.before_match_dialog = None
        self.active_match_dialog = None
        card = make_card()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        self.heading = StrongBodyLabel(tr('数据源')); layout.addWidget(self.heading)
        labels = [("prompts", tr('分镜提示词目录')), ("images", tr('参考图片目录')), ("output", tr('视频保存目录'))]
        self._caption_labels = {}
        for key, text in labels:
            row = WrapLayout(spacing=10)
            caption_widget = CaptionLabel(text)
            caption_widget.setMinimumWidth(104)
            row.addWidget(caption_widget, 0, Qt.AlignVCenter)
            field = LineEdit()
            field.setReadOnly(True)
            field.setPlaceholderText(tr('尚未选择目录'))
            field.setText(config_manager.config["paths"].get(key, ""))
            field.setMinimumWidth(200)
            field.setFixedHeight(32)
            field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            field.setToolTip(field.text())
            self.fields[key] = field
            self._caption_labels[key] = caption_widget
            row.addWidget(field, 1)
            choose = PushButton(FIF.FOLDER, tr('选择'))
            choose.setFixedHeight(32)
            choose.clicked.connect(lambda _=False, k=key, f=field: self._choose(k, f))
            row.addWidget(choose, 0, Qt.AlignVCenter)
            self.choose_buttons.append(choose)
            layout.addLayout(row)
        divider = QWidget(); divider.setFixedHeight(1); divider.setStyleSheet('background:rgba(255,255,255,0.08);')
        layout.addSpacing(2); layout.addWidget(divider); layout.addSpacing(2)
        status_row = QHBoxLayout()
        self.status_label = CaptionLabel(tr('请选择提示词与图片目录'))
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        self.match_button = TransparentToolButton(FIF.SEARCH)
        self.match_button.setToolTip(tr('匹配详情'))
        self.match_button.clicked.connect(self.open_match_dialog)
        status_row.addWidget(self.match_button)
        layout.addLayout(status_row)
        self._match_counts = None
        self._status_override = None
        self._busy = False

    def _choose(self, key: str, field: LineEdit) -> None:
        directory = QFileDialog.getExistingDirectory(self, tr('选择目录'), self.config_manager.config['paths'].get(key, ''))
        if directory:
            directory = str(Path(directory).resolve())
            field.setText(directory)
            field.setToolTip(directory)
            self.config_manager.update(("paths", key), directory)
            self.log_callback(tr('已设置{key}目录：{directory}').format(key=key, directory=directory), "success")
            self.directories_changed.emit()

    def set_busy(self, busy):
        self._busy = bool(busy)
        reason = tr('队列、匹配扫描或下载进行中，完成后可选择目录')
        for button in self.choose_buttons:
            button.setEnabled(not busy)
            button.setToolTip(reason if busy else tr('选择目录'))
        self.match_button.setEnabled(not busy)
        self.match_button.setToolTip(reason if busy else tr('匹配详情'))

    def set_matches(self, matches, automatic=None):
        self.matches = matches
        self._status_override = None
        if automatic is not None:
            self.automatic_matches = automatic
        count = sum(bool(t['images']) for t in matches)
        products = {task.get('product') for task in matches if task.get('product')}
        self._match_counts = (len(products), len(matches), count)
        self._render_status()

    def set_status_override(self, state=None):
        """记录一条临时状态（如扫描失败），语言切换后按同一状态重新翻译。"""
        self._status_override = state
        self._render_status()

    def _render_status(self):
        if self._status_override == 'scan_failed':
            self.status_label.setText(tr('扫描失败，请检查目录'))
            return
        if self._match_counts is None:
            self.status_label.setText(tr('请选择提示词与图片目录'))
            return
        products, prompts, matched = self._match_counts
        self.status_label.setText(
            tr('已识别 {products} 个产品，共 {prompts} 个提示词；已匹配 {matched} / 未匹配 {unmatched}').format(
                products=products, prompts=prompts, matched=matched, unmatched=prompts - matched))

    def open_match_dialog(self, prompt_path=None) -> None:
        if self.active_match_dialog is not None and self.active_match_dialog.isVisible():
            self.active_match_dialog.raise_()
            self.active_match_dialog.activateWindow()
            return
        if self.before_match_dialog is not None:
            if not self.before_match_dialog(prompt_path):
                return
        self.log_callback(tr('打开匹配详情'), "info")
        dialog = MatchDialog(self.window(), self.log_callback, self.config_manager, self.matches, self.automatic_matches)
        self.active_match_dialog = dialog
        if isinstance(prompt_path, str):
            for index, task in enumerate(dialog.matches):
                if task.get('prompt_path') == prompt_path:
                    dialog.prompt_list.setCurrentRow(index)
                    break
        dialog.saved.connect(self.overrides_changed)
        try:
            dialog.exec_()
        finally:
            self.active_match_dialog = None
            safe_delete_dialog(dialog)

    def retranslate_ui(self):
        """语言切换后重建本卡文案；目录数据与匹配结果保持不变。"""
        try:
            self.heading.setText(tr('数据源'))
            for key, text in (('prompts', tr('分镜提示词目录')), ('images', tr('参考图片目录')), ('output', tr('视频保存目录'))):
                label = self._caption_labels.get(key)
                if label is not None:
                    label.setText(text)
            for field in self.fields.values():
                field.setPlaceholderText(tr('尚未选择目录'))
            for button in self.choose_buttons:
                button.setText(tr('选择'))
            self.match_button.setToolTip(tr('匹配详情'))
            self._render_status()
            self.set_busy(self._busy)
        except RuntimeError:
            pass
