"""Frameless Fluent binding editor with real-image loading and local ordering."""

from pathlib import Path
from PyQt5.QtCore import Qt, QSize, pyqtSignal
import copy
from PyQt5.QtGui import QColor, QPainter, QPixmap
from PyQt5.QtWidgets import QListWidgetItem, QSplitter, QVBoxLayout, QHBoxLayout, QWidget, QFileDialog
# 1.11.3 does not export FramelessDialog; use its frameless-window dependency.
from qframelesswindow import FramelessDialog
from qfluentwidgets import (
    CaptionLabel, ImageLabel, ListWidget, PrimaryPushButton, PushButton,
    StrongBodyLabel, TransparentToolButton, FluentIcon as FIF, Theme,
)
from ..theme import style_controls, style_page


class MatchDialog(FramelessDialog):
    saved = pyqtSignal()

    def __init__(self, parent=None, log_callback=None, config_manager=None, matches=None, automatic_matches=None):
        super().__init__(parent)
        self.log_callback = log_callback or (lambda *args: None)
        self.config_manager = config_manager
        self.matches = copy.deepcopy(matches or [])
        self.bindings = {t['prompt_path']: list(t['images']) for t in self.matches}
        self.automatic = {t['prompt_path']: list(t['images']) for t in automatic_matches or self.matches}
        self.dirty = set()
        self.setObjectName("matchDialog")
        self.setWindowTitle("图片-提示词匹配详情")
        self.resize(900, 650)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 40, 20, 20)
        root.setSpacing(16)
        root.addWidget(StrongBodyLabel("图片与提示词匹配详情"))
        root.addWidget(CaptionLabel('拖动图片行调整 @参考图1/2/3 的顺序；修改后点击保存调整'))
        splitter = QSplitter(Qt.Horizontal)
        self.prompt_list = ListWidget()
        for task in self.matches:
            item = QListWidgetItem(Path(task['prompt_path']).name)
            item.setData(Qt.UserRole, task['prompt_path'])
            item.setToolTip(task['prompt_path'])
            item.setIcon(FIF.DOCUMENT.icon())
            self.prompt_list.addItem(item)
        splitter.addWidget(self.prompt_list)
        detail = QWidget()
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(StrongBodyLabel("图片绑定详情"))
        self.binding_summary = CaptionLabel('已绑定0张参考图')
        layout.addWidget(self.binding_summary)
        self.picture_list = ListWidget()
        self.picture_list.setDragDropMode(ListWidget.InternalMove)
        self.picture_list.setDefaultDropAction(Qt.MoveAction)
        layout.addWidget(self.picture_list, 1)
        actions = QHBoxLayout()
        add = PushButton(FIF.ADD, "添加图片")
        remove = PushButton(FIF.REMOVE, "移除")
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        actions.addWidget(add)
        actions.addWidget(remove)
        actions.addStretch(1)
        layout.addLayout(actions)
        splitter.addWidget(detail)
        splitter.setSizes([290, 550])
        root.addWidget(splitter, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        rematch = PushButton(FIF.SYNC, "重新自动匹配")
        save = PrimaryPushButton(FIF.SAVE, "保存调整")
        close = PushButton("关闭")
        rematch.clicked.connect(self._rematch)
        save.clicked.connect(self._save)
        close.clicked.connect(self.accept)
        for button in (rematch, save, close):
            footer.addWidget(button)
        root.addLayout(footer)
        self.prompt_list.currentRowChanged.connect(self._show_details)
        self.picture_list.model().rowsMoved.connect(self._remember)
        self.prompt_list.setCurrentRow(0)
        style_page(self)
        style_controls(self)

    def _show_details(self, row):
        self.picture_list.clear()
        self.binding_summary.setText('已绑定0张参考图')
        if row < 0:
            return
        key = self.matches[row]['prompt_path']
        paths = self.bindings.get(key, [])
        for path in paths:
            self._append_picture(path)
        self._refresh_picture_labels()

    def _append_picture(self, path):
        item = QListWidgetItem(self.picture_list)
        item.setData(Qt.UserRole, path)
        item.setSizeHint(QSize(360, 100))
        host = QWidget()
        # All row children are display-only. Route mouse drags to the Fluent list.
        host.setAttribute(Qt.WA_TransparentForMouseEvents)
        row = QHBoxLayout(host)
        row.setContentsMargins(8, 10, 8, 10)
        pixmap = QPixmap(path)
        missing = pixmap.isNull()
        item.setData(Qt.UserRole + 1, missing)
        if pixmap.isNull():
            pixmap = QPixmap(80, 80)
            pixmap.fill(QColor("#343958"))
            painter = QPainter(pixmap)
            painter.drawPixmap(24, 24, FIF.PHOTO.icon(Theme.DARK).pixmap(32, 32))
            painter.end()
        else:
            pixmap = pixmap.scaled(80, 80, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            pixmap = pixmap.copy((pixmap.width()-80)//2, (pixmap.height()-80)//2, 80, 80)
        thumb = ImageLabel(pixmap)
        thumb.setBorderRadius(8, 8, 8, 8)
        row.addWidget(thumb)
        label = CaptionLabel('')
        label.setObjectName('pictureCaption')
        row.addWidget(label, 1)
        handle = TransparentToolButton(FIF.MOVE)
        handle.setToolTip("拖动图片行调整顺序")
        handle.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(handle)
        self.picture_list.setItemWidget(item, host)

    def _refresh_picture_labels(self):
        count = self.picture_list.count()
        self.binding_summary.setText(f'已绑定{count}张参考图 · 从上到下对应 Picture 1–{count}' if count else '已绑定0张参考图')
        for index in range(count):
            item = self.picture_list.item(index)
            host = self.picture_list.itemWidget(item)
            if host is not None:
                name = Path(item.data(Qt.UserRole)).name
                status = '文件缺失或图片不可读' if item.data(Qt.UserRole + 1) else '本地图片'
                host.findChild(CaptionLabel, 'pictureCaption').setText(f'Picture {index+1} · {name}\n{status}')

    def _remember(self, *_):
        self._refresh_picture_labels()
        item = self.prompt_list.currentItem()
        if item:
            key = item.data(Qt.UserRole)
            self.bindings[key] = [self.picture_list.item(i).data(Qt.UserRole) for i in range(self.picture_list.count())]
            self.dirty.add(key)

    def _add(self):
        if self.prompt_list.currentRow() < 0:
            self._notice('请先选择目录并扫描提示词')
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "选择参考图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        for path in paths:
            self._append_picture(str(Path(path).resolve()))
        self._remember()

    def _remove(self):
        row = self.picture_list.currentRow()
        if row >= 0:
            self.picture_list.takeItem(row)
            self._remember()

    def _save(self):
        if self.config_manager:
            overrides = dict(self.config_manager.config.get('match_overrides', {}))
            for key in self.dirty:
                overrides[key] = self.bindings[key]
            try:
                if not self.config_manager.update(('match_overrides',), overrides):
                    self._notice('保存失败，请查看执行日志；本次修改仍保留在内存')
                    return
            except Exception as error:
                self._notice(f'保存失败：{error}')
                return
        self.saved.emit()
        self._notice("匹配调整已保存")

    def _rematch(self):
        self.bindings = copy.deepcopy(self.automatic)
        self.dirty.update(self.bindings)
        self._show_details(self.prompt_list.currentRow())
        self._notice('已恢复本次扫描的自动匹配结果，点击保存调整生效')

    def _notice(self, text):
        from qfluentwidgets import InfoBar
        self.log_callback(text, "info")
        InfoBar.info("匹配详情", text, parent=self, duration=2200)
