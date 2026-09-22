"""Recently downloaded files from persisted history."""
from pathlib import Path
from ..file_actions import open_local
from core.i18n import tr

from PyQt5.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget, QHBoxLayout, QSizePolicy
from PyQt5.QtCore import Qt

from qfluentwidgets import FluentIcon as FIF, TransparentToolButton, IconWidget
from ..components.custom_widgets import CaptionLabel, StrongBodyLabel


class RecentCompletedRow(QWidget):
    def __init__(self, filename: str, size: str, time: str, log_callback, path='', parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 8, 6)
        layout.setSpacing(8)
        text = QVBoxLayout()
        text.setSpacing(2)
        title = StrongBodyLabel(filename)
        title.setMinimumWidth(0); title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred); title.setToolTip(filename)
        text.addWidget(title)
        detail = CaptionLabel(f"{size}\n{time}")
        detail.setMinimumWidth(0); detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        text.addWidget(detail)
        layout.addLayout(text, 1)
        open_button = TransparentToolButton(FIF.FOLDER)
        open_button.setToolTip(tr("打开"))
        open_button.clicked.connect(lambda: open_local(path, log_callback))
        layout.addWidget(open_button)


class RecentCompletedPanel(QWidget):
    def __init__(self, log_callback, parent=None) -> None:
        super().__init__(parent)
        self.log_callback = log_callback
        root = QVBoxLayout(self); root.setContentsMargins(8, 0, 0, 0); root.setSpacing(10)
        root.addWidget(StrongBodyLabel(tr("最近完成")))
        self.list = QListWidget()
        self.list.setSpacing(2)
        self.list.setStyleSheet(
            "QListWidget { background: rgba(0,0,0,0.15); border: 1px solid rgba(255,255,255,0.08); border-radius:12px; }"
            "QListWidget::item { background: transparent; border-radius: 8px; }"
            "QListWidget::item:selected { background: rgba(64,158,255,0.3); }"
        )
        root.addWidget(self.list, 1)
        self.empty_state = QWidget()
        self.empty_state.setObjectName('recentEmptyState')
        empty = QVBoxLayout(self.empty_state)
        empty.setContentsMargins(0, 0, 0, 0)
        empty.addStretch(1)
        icon = IconWidget(FIF.VIDEO); icon.setFixedSize(38, 38)
        empty.addWidget(icon, 0, Qt.AlignHCenter)
        label = CaptionLabel(tr('暂无完成的视频')); label.setAlignment(Qt.AlignCenter); label.setWordWrap(True)
        empty.addWidget(label)
        empty.addStretch(1)
        root.addWidget(self.empty_state, 1)
        self.update_history([])

    def update_history(self, records):
        records = sorted((r for r in records if r.get('status') == 'completed' and r.get('result_path')),
                         key=lambda r: r.get('finished_at', ''), reverse=True)[:20]
        keys = [(r.get('local_id'), r.get('finished_at'), r.get('size_bytes')) for r in records]
        if keys == getattr(self, '_keys', None):
            return  # 数据未变化：跳过重建，避免反复重挂行控件
        self._keys = keys
        # 显式销毁旧行控件：list.clear() 只删条目，setItemWidget 挂载的部件不会随之释放。
        for index in range(self.list.count()):
            widget = self.list.itemWidget(self.list.item(index))
            if widget is not None:
                widget.setParent(None); widget.deleteLater()
        self.list.clear()
        self.list.setVisible(bool(records))
        self.empty_state.setVisible(not records)
        for task in records:
            item = QListWidgetItem(self.list)
            path = task['result_path']
            row = RecentCompletedRow(Path(path).name, f"{task.get('size_bytes', 0)/1024/1024:.1f} MB",
                                     task.get('finished_at', '').replace('T', ' '), self.log_callback, path)
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
        from ..theme import style_controls
        style_controls(self.list)
