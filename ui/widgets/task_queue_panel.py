"""Task queue driven by scheduler snapshots."""
from PyQt5.QtCore import Qt
from core.task_manager import STATUS_TEXT

COLORS = {'已完成': '#22c55e', '生成中': '#5e6ad2', '等待中': '#f59e0b', '失败': '#ef4444',
          '已取消': '#92929b', '已跳过': '#f59e0b', '已暂停': '#f59e0b', '上传中': '#5e6ad2', '提交中': '#5e6ad2', '下载中': '#5e6ad2'}

from PyQt5.QtWidgets import QListWidget, QListWidgetItem, QGridLayout, QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy

from ..components.custom_widgets import BodyLabel, ComboBox, CaptionLabel, StrongBodyLabel, make_card


class TaskQueueRow(QWidget):
    """Fluent list row without a native checkbox or delegate text rendering."""

    def __init__(self, number: int, name: str, status: str, color: str, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        dot = BodyLabel("●")
        dot.setTextColor(color, color)
        layout.addWidget(dot)
        layout.addWidget(CaptionLabel(f"{number:02d}"))
        title = StrongBodyLabel(name)
        title.setMinimumWidth(0)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        title.setToolTip(name)
        layout.addWidget(title, 1)
        state = CaptionLabel(status)
        state.setTextColor(color, color)
        layout.addWidget(state)


class TaskQueuePanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(10)
        root.addWidget(StrongBodyLabel("任务队列"))
        self.filter_box = ComboBox()
        self.filter_box.addItems(["全部", "等待中", "生成中", "已完成", "失败", "已跳过", "已取消", "已暂停"])
        root.addWidget(self.filter_box)
        self.list = QListWidget()
        self.list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.list.setSelectionMode(QListWidget.SingleSelection)
        self.list.setSpacing(2)
        self.list.setStyleSheet(
            "QListWidget { background: #101013; border: 1px solid rgba(255,255,255,0.08); }"
            "QListWidget::item { background: transparent; border-radius: 8px; }"
            "QListWidget::item:selected { background: #5e6ad2; }"
        )
        self.list.currentItemChanged.connect(self._selection_color)
        self.filter_box.currentTextChanged.connect(self._filter)
        root.addWidget(self.list, 1)
        grid = QGridLayout()
        grid.setSpacing(8)
        self.stat_labels = []
        stats = [("成功数", "00", "#22c55e"), ("进行中", "00", "#5e6ad2"), ("失败数", "00", "#ef4444"), ("跳过数", "00", "#f59e0b")]
        for index, (title, value, color) in enumerate(stats):
            card = make_card()
            card.setFixedHeight(66)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 8, 12, 8)
            card_layout.setSpacing(3)
            label = CaptionLabel(title); label.setStyleSheet('font-size: 11px;')
            card_layout.addWidget(label)
            number = StrongBodyLabel(value); number.setStyleSheet(f"color: {color}; font-size: 18px;")
            card_layout.addWidget(number)
            self.stat_labels.append(number)
            grid.addWidget(card, index // 2, index % 2)
        root.addLayout(grid)

    def _filter(self, status):
        from PyQt5.QtCore import Qt
        for index in range(self.list.count()):
            item = self.list.item(index)
            current = item.data(Qt.UserRole)
            matches = current == status or (status == '生成中' and current in {'上传中', '提交中', '下载中'})
            item.setHidden(status != "全部" and not matches)

    def update_tasks(self, tasks):
        selected = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        for index, task in enumerate(tasks):
            status = STATUS_TEXT.get(task.get('status', 'waiting'), '等待中')
            item = QListWidgetItem(self.list)
            item.setData(Qt.UserRole, status)
            row = TaskQueueRow(index + 1, task['prompt_name'], status, COLORS[status])
            item.setToolTip(task['prompt_name'] + '\n' + task.get('error', ''))
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
        self.list.blockSignals(False)
        self.list.setCurrentRow(min(max(0, selected), len(tasks)-1))
        statuses = [t.get('status', 'waiting') for t in tasks]
        counts = [statuses.count('completed'), sum(s in {'uploading', 'submitting', 'queued', 'processing', 'downloading'} for s in statuses),
                  statuses.count('failed'), statuses.count('skipped')]
        for label, value in zip(self.stat_labels, counts):
            label.setText(f'{value:02d}')
        self._filter(self.filter_box.currentText())

    def _selection_color(self, current, previous):
        from PyQt5.QtCore import Qt
        for item in (previous, current):
            if item is None:
                continue
            row = self.list.itemWidget(item)
            if row is None:
                continue
            color = "#ffffff" if item is current else COLORS.get(item.data(Qt.UserRole), '#92929b')
            row.findChild(BodyLabel).setTextColor(color, color)
            row.findChildren(CaptionLabel)[-1].setTextColor(color, color)
