"""Task queue driven by scheduler snapshots."""
from PyQt5.QtCore import Qt
from core.task_manager import STATUS_TEXT, TERMINAL

COLORS = {'已完成': '#22c55e', '生成中': '#5e6ad2', '等待中': '#f59e0b', '失败': '#ef4444',
          '已取消': '#92929b', '已跳过': '#f59e0b', '已暂停': '#f59e0b', '上传中': '#5e6ad2', '提交中': '#5e6ad2', '下载中': '#5e6ad2'}

from PyQt5.QtWidgets import QListWidget, QListWidgetItem, QGridLayout, QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy
from qfluentwidgets import IconWidget, FluentIcon as FIF

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
        self.title = StrongBodyLabel(name)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.title.setToolTip(name)
        layout.addWidget(self.title, 1)
        state = CaptionLabel(status)
        state.setTextColor(color, color)
        layout.addWidget(state)


class TaskGroupHeader(QWidget):
    def __init__(self, product, task_count, collapsed=False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(7)
        self.indicator = IconWidget(FIF.RIGHT_ARROW if collapsed else FIF.DOWN)
        self.indicator.setFixedSize(12, 12)
        self.title = StrongBodyLabel(product)
        self.count = CaptionLabel(f'{task_count} 个任务')
        layout.addWidget(self.indicator)
        layout.addWidget(self.title, 1)
        layout.addWidget(self.count)


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
        self.list.currentItemChanged.connect(self._selected_task_changed)
        self.list.itemClicked.connect(self._item_clicked)
        self.filter_box.currentTextChanged.connect(self._filter)
        root.addWidget(self.list, 1)
        self.current_product_label = CaptionLabel('当前产品 —')
        self.product_progress_label = CaptionLabel('已完成产品 0/0')
        root.addWidget(self.current_product_label)
        root.addWidget(self.product_progress_label)
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
        self._tasks = []
        self._task_items = {}
        self._group_items = {}
        self._collapsed = set()

    def _filter(self, status):
        group_matches = {}
        for index, task in enumerate(self._tasks):
            current = STATUS_TEXT.get(task.get('status', 'waiting'), '等待中')
            matches = status == '全部' or current == status or (status == '生成中' and current in {'上传中', '提交中', '下载中'})
            product = task.get('product', '')
            group_matches[product] = group_matches.get(product, False) or matches
            item = self._task_items.get(index)
            if item is not None:
                item.setHidden(not matches or (bool(product) and product in self._collapsed))
        for product, item in self._group_items.items():
            item.setHidden(not group_matches.get(product, False))

    def update_tasks(self, tasks):
        selected_item = self.list.currentItem()
        selected = selected_item.data(Qt.UserRole + 1) if selected_item is not None else None
        self._tasks = list(tasks)
        self._task_items = {}
        self._group_items = {}
        self.list.blockSignals(True)
        self.list.clear()
        grouped_counts = {}
        for task in tasks:
            product = task.get('product', '')
            if product:
                grouped_counts[product] = grouped_counts.get(product, 0) + 1
        previous_product = None
        for index, task in enumerate(tasks):
            product = task.get('product', '')
            if product and product != previous_product:
                header_item = QListWidgetItem(self.list)
                header_item.setData(Qt.UserRole + 2, product)
                header = TaskGroupHeader(product, grouped_counts[product], product in self._collapsed)
                header_item.setSizeHint(header.sizeHint())
                self.list.setItemWidget(header_item, header)
                self._group_items[product] = header_item
            status = STATUS_TEXT.get(task.get('status', 'waiting'), '等待中')
            item = QListWidgetItem(self.list)
            item.setData(Qt.UserRole, status)
            item.setData(Qt.UserRole + 1, index)
            title = f'{product} / {task["prompt_name"]}' if product else task['prompt_name']
            number = task.get('product_task_index', index + 1) if product else index + 1
            row = TaskQueueRow(number, title, status, COLORS[status])
            item.setToolTip(title + '\n' + task.get('error', ''))
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
            self._task_items[index] = item
            previous_product = product
        self.list.blockSignals(False)
        if tasks:
            self.select_task(selected if isinstance(selected, int) and 0 <= selected < len(tasks) else 0)
        statuses = [t.get('status', 'waiting') for t in tasks]
        counts = [statuses.count('completed'), sum(s in {'uploading', 'submitting', 'queued', 'processing', 'downloading'} for s in statuses),
                  statuses.count('failed'), statuses.count('skipped')]
        for label, value in zip(self.stat_labels, counts):
            label.setText(f'{value:02d}')
        product_tasks = {}
        for task in tasks:
            product_tasks.setdefault(task.get('product') or None, []).append(task)
        completed = sum(bool(rows) and all(row.get('status', 'waiting') in TERMINAL for row in rows)
                        for rows in product_tasks.values())
        self.product_progress_label.setText(f'已完成产品 {completed}/{len(product_tasks)}')
        self._filter(self.filter_box.currentText())

    def task_item(self, index):
        return self._task_items.get(index)

    def select_task(self, index):
        item = self._task_items.get(index)
        if item is not None:
            self.list.setCurrentItem(item)

    def toggle_group(self, product):
        if product in self._collapsed:
            self._collapsed.remove(product)
        else:
            self._collapsed.add(product)
        item = self._group_items.get(product)
        if item is not None:
            row = self.list.itemWidget(item)
            row.indicator.setIcon(FIF.RIGHT_ARROW if product in self._collapsed else FIF.DOWN)
        self._filter(self.filter_box.currentText())

    def _item_clicked(self, item):
        product = item.data(Qt.UserRole + 2)
        if product:
            self.toggle_group(product)

    def _selected_task_changed(self, current, _previous):
        index = current.data(Qt.UserRole + 1) if current is not None else None
        if isinstance(index, int) and 0 <= index < len(self._tasks):
            product = self._tasks[index].get('product') or '未分组'
            self.current_product_label.setText(f'当前产品 {product}')

    def _selection_color(self, current, previous):
        from PyQt5.QtCore import Qt
        for item in (previous, current):
            if item is None:
                continue
            row = self.list.itemWidget(item)
            if row is None:
                continue
            color = "#ffffff" if item is current else COLORS.get(item.data(Qt.UserRole), '#92929b')
            dot = row.findChild(BodyLabel)
            if dot is not None:
                dot.setTextColor(color, color)
            row.findChildren(CaptionLabel)[-1].setTextColor(color, color)
