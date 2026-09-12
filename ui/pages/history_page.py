"""Persisted task records and manual download recovery in a Fluent table."""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QHeaderView, QTableWidgetItem
from qfluentwidgets import ComboBox, PushButton, TableWidget, TitleLabel, CaptionLabel, TransparentToolButton, FluentIcon as FIF
from core.task_manager import STATUS_TEXT
from ..file_actions import open_local


class HistoryPage(QWidget):
    redownload_requested = pyqtSignal(object)

    def __init__(self, log_callback, config_manager, parent=None):
        super().__init__(parent)
        self.setObjectName('historyPage')
        self.log_callback = log_callback
        self.config_manager = config_manager
        root = QVBoxLayout(self); root.setContentsMargins(22, 18, 22, 18); root.setSpacing(14)
        header = QHBoxLayout(); title_box = QVBoxLayout(); title_box.addWidget(TitleLabel('任务历史')); title_box.addWidget(CaptionLabel('查看生成记录 · 重新下载会沿用 task_id，不重复提交'))
        header.addLayout(title_box); header.addStretch(1)
        self.filter_box = ComboBox(); self.filter_box.addItems(['全部状态', '已完成', '生成中', '失败', '已取消', '已跳过']); header.addWidget(self.filter_box)
        self.open_button = PushButton(FIF.FOLDER, '打开输出文件夹')
        self.open_button.clicked.connect(self.open_output)
        header.addWidget(self.open_button); root.addLayout(header)
        columns = ['序号', '产品', '任务名', '模型', '状态', '提交时间', '完成时间', '文件大小', '操作']
        self.table = TableWidget(); self.table.setColumnCount(len(columns)); self.table.setHorizontalHeaderLabels(columns)
        self.table.setBorderVisible(True); self.table.setBorderRadius(8); self.table.setEditTriggers(self.table.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self.table.verticalHeader().setDefaultSectionSize(48)
        root.addWidget(self.table, 1)
        self.filter_box.currentTextChanged.connect(self._filter)
        self.update_history(config_manager.config['history'])

    def update_history(self, records):
        self.records = list(reversed(records))
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.records))
        for row, task in enumerate(self.records):
            status = STATUS_TEXT.get(task.get('status'), task.get('status', '—'))
            values = [str(len(self.records)-row), task.get('product', '') or '—', task.get('prompt_name', ''), task.get('model', ''), status,
                      task.get('created_at', '').replace('T', '\n') or '—', task.get('finished_at', '').replace('T', '\n') or '—',
                      f"{task['size_bytes']/1024/1024:.1f} MB" if task.get('size_bytes') else '—']
            for column, value in enumerate(values):
                item = QTableWidgetItem(value); item.setTextAlignment(Qt.AlignCenter)
                item.setToolTip(task.get('error', '') or task.get('task_id', ''))
                self.table.setItem(row, column, item)
            action = QWidget(); layout = QHBoxLayout(action); layout.setContentsMargins(4, 2, 4, 2)
            open_file = TransparentToolButton(FIF.FOLDER); open_file.setToolTip('打开文件')
            open_file.setEnabled(bool(task.get('result_path')))
            open_file.clicked.connect(lambda _, t=task: open_local(t.get('result_path'), self.log_callback))
            retry = TransparentToolButton(FIF.SYNC); retry.setToolTip('重新查询并下载（不创建新任务）')
            retry.setEnabled(bool(task.get('task_id')))
            retry.clicked.connect(lambda _, t=task: self.redownload_requested.emit(t))
            layout.addWidget(open_file); layout.addWidget(retry)
            self.table.setCellWidget(row, len(values), action)
        self._filter(self.filter_box.currentText())
        from ..theme import style_controls
        style_controls(self.table)

    def open_output(self):
        row = self.table.currentRow()
        task = self.records[row] if 0 <= row < len(self.records) else {}
        folder = task.get('output_dir') or task.get('result_path') or self.config_manager.config['paths']['output']
        open_local(folder, self.log_callback, folder=True)

    def _filter(self, status):
        for row in range(self.table.rowCount()):
            self.table.setRowHidden(row, status != '全部状态' and self.table.item(row, 4).text() != status)
