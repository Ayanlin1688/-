"""Task queue driven by scheduler snapshots."""
from PyQt5.QtCore import Qt, pyqtSignal
from core.task_manager import STATUS_TEXT, TERMINAL, ACTIVE
from core.i18n import tr

COLORS = {'已完成': '#67c23a', '生成中': '#409eff', '等待中': '#e6a23c', '失败': '#f56c6c',
          '重试中': '#e6a23c', '等待冷却': '#e6a23c',
          '重复': '#92929b', '提交待确认': '#e6a23c',
          '已取消': '#92929b', '已跳过': '#e6a23c', '已暂停': '#e6a23c', '上传中': '#409eff', '提交中': '#409eff', '下载中': '#409eff'}
from ..motion import StatusDot
from core.prompt_detector import short_model_name, SOURCE_TEXT
from core.model_catalog import builtin_models
from ..components.model_selector import usable, model_label

from PyQt5.QtWidgets import QListWidget, QListWidgetItem, QGridLayout, QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy
from qfluentwidgets import IconWidget, FluentIcon as FIF, RoundMenu, Action, PushButton

from ..components.custom_widgets import BodyLabel, ComboBox, CaptionLabel, StrongBodyLabel, make_card


class TaskQueueRow(QWidget):
    """Fluent list row without a native checkbox or delegate text rendering."""

    def __init__(self, number: int, name: str, status: str, color: str, parent=None, model='', source='') -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        dot = StatusDot(color, active=status in {'生成中', '上传中', '提交中', '下载中'})
        dot.setTextColor(color, color)
        layout.addWidget(dot)
        layout.addWidget(CaptionLabel(f"{number:02d}"))
        self.title = StrongBodyLabel(name)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.title.setToolTip(name)
        labels = QVBoxLayout()
        labels.setSpacing(1)
        labels.addWidget(self.title)
        self.model_label = CaptionLabel(f'[{short_model_name(model)}]' + (f' · {source}' if source else '') if model else '')
        self.model_label.setStyleSheet('font-size:11px;')
        self.model_label.setMinimumWidth(0)
        self.model_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.model_label.setToolTip(model + '\n' + source)
        self.model_label.setVisible(bool(model))
        labels.addWidget(self.model_label)
        layout.addLayout(labels, 1)
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
    task_selected = pyqtSignal(int)
    model_override_requested = pyqtSignal(int, str)
    reset_models_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._programmatic_selection = False
        self._catalog = builtin_models()
        self._default_model = ''
        self.models_editable = True
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(10)
        root.addWidget(StrongBodyLabel("任务队列"))
        self.filter_box = ComboBox()
        self.filter_box.addItems([tr('全部'), tr('等待中'), tr('生成中'), tr('上传中'), tr('提交中'), tr('下载中'), tr('重试中'), tr('等待冷却'), tr('已暂停'), tr('已完成'), tr('失败'), tr('重复'), tr('提交待确认'), tr('已跳过'), tr('已取消')])
        root.addWidget(self.filter_box)
        self.reset_models_button = PushButton(FIF.SYNC, '重置模型识别')
        self.reset_models_button.setToolTip('全部重置为自动识别')
        self.reset_models_button.clicked.connect(self.reset_models_requested)
        root.addWidget(self.reset_models_button)
        self.list = QListWidget()
        self.list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.list.setSelectionMode(QListWidget.SingleSelection)
        self.list.setSpacing(2)
        self.list.setStyleSheet(
            "QListWidget { background: rgba(0,0,0,0.15); border: 1px solid rgba(255,255,255,0.08); border-radius:12px; }"
            "QListWidget::item { background: transparent; border-radius: 8px; }"
            "QListWidget::item:selected { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba(64,158,255,0.5),stop:1 rgba(64,158,255,0.18)); }"
        )
        self.list.currentItemChanged.connect(self._selection_color)
        self.list.currentItemChanged.connect(self._selected_task_changed)
        self.list.itemClicked.connect(self._item_clicked)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_model_menu)
        self.filter_box.currentTextChanged.connect(self._filter)
        root.addWidget(self.list, 1)
        self.current_product_label = CaptionLabel('当前产品 —')
        self.product_progress_label = CaptionLabel('已完成产品 0/0')
        root.addWidget(self.current_product_label)
        root.addWidget(self.product_progress_label)
        grid = QGridLayout()
        grid.setSpacing(8)
        self.stat_labels = []
        stats = [("成功数", "00", "#67c23a"), ("进行中", "00", "#409eff"), ("失败数", "00", "#f56c6c"), ("跳过数", "00", "#e6a23c")]
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
            current = tr(STATUS_TEXT.get(task.get('status', 'waiting'), '等待中'))
            matches = status == tr('全部') or current == status or (status == tr('生成中') and task.get('status') in ACTIVE)
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
            model = task.get('model') or task.get('requested_model', '')
            source = task.get('model_source', '')
            source_label = '手动' if source == 'manual' else '自动' if source == 'auto' and model != self._default_model else ''
            row = TaskQueueRow(number, title, status, COLORS[status], model=model, source=source_label)
            item.setToolTip(title + '\n' + task.get('error', ''))
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
            self._task_items[index] = item
            previous_product = product
        self.list.blockSignals(False)
        if tasks:
            self.select_task(selected if isinstance(selected, int) and 0 <= selected < len(tasks) else 0)
        statuses = [t.get('status', 'waiting') for t in tasks]
        counts = [statuses.count('completed'), sum(s in ACTIVE for s in statuses),
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

    def set_model_catalog(self, catalog, default_model):
        self._catalog = catalog
        self._default_model = default_model

    def set_models_editable(self, editable):
        self.models_editable = bool(editable)
        self.reset_models_button.setEnabled(editable)

    def model_menu_for(self, index):
        menu = RoundMenu('强制使用模型', self)
        for model, record in self._catalog.items():
            action = Action(model_label(record), menu)
            action.setData(model)
            action.setToolTip(record.get('description') or model)
            action.setEnabled(self.models_editable and usable(record))
            action.triggered.connect(lambda checked=False, value=model: self.model_override_requested.emit(index, value))
            menu.addAction(action)
        return menu

    def _show_model_menu(self, position):
        item = self.list.itemAt(position)
        index = item.data(Qt.UserRole + 1) if item is not None else None
        if not self.models_editable or not isinstance(index, int):
            return
        menu = RoundMenu(parent=self)
        menu.addMenu(self.model_menu_for(index))
        reset = Action('恢复自动识别', menu)
        reset.triggered.connect(lambda: self.model_override_requested.emit(index, ''))
        menu.addAction(reset)
        menu.exec(self.list.mapToGlobal(position))
        menu.deleteLater()

    def select_task(self, index):
        item = self._task_items.get(index)
        if item is not None:
            self._programmatic_selection = True
            try:
                self.list.setCurrentItem(item)
            finally:
                self._programmatic_selection = False

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
            if not self._programmatic_selection:
                self.task_selected.emit(index)

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
                # Selection brightens text; keep the status dot's semantic hue.
                dot_color = COLORS.get(item.data(Qt.UserRole), color)
                dot.setTextColor(dot_color, dot_color)
            row.findChildren(CaptionLabel)[-1].setTextColor(color, color)
