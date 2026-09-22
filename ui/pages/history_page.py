"""Persisted task records and manual download recovery in a Fluent table."""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QHeaderView, QTableWidgetItem, QLabel
from qfluentwidgets import ComboBox, PushButton, TableWidget, TitleLabel, CaptionLabel, TransparentToolButton, IconWidget, StrongBodyLabel, FluentIcon as FIF
from core.task_manager import STATUS_TEXT
from core.i18n import tr
from ..file_actions import open_local

HISTORY_DISPLAY_LIMIT = 100  # 历史页只渲染最近 N 条（页脚文案同步承诺）


class HistoryPage(QWidget):
    redownload_requested = pyqtSignal(object)
    resolve_requested = pyqtSignal(object)
    regenerate_requested = pyqtSignal(object)
    create_requested = pyqtSignal()

    def __init__(self, log_callback, config_manager, parent=None):
        super().__init__(parent)
        self.setObjectName('historyPage')
        self.log_callback = log_callback
        self.config_manager = config_manager
        root = QVBoxLayout(self); root.setContentsMargins(26, 16, 26, 20); root.setSpacing(16)
        header = QHBoxLayout(); title_box = QVBoxLayout(); title_box.addWidget(TitleLabel(tr('任务历史'))); title_box.addWidget(CaptionLabel(tr('查看生成记录 · 重新下载会沿用 task_id，不重复提交')))
        header.addLayout(title_box); header.addStretch(1)
        self.filter_box = ComboBox(); self.filter_box.addItems([tr('全部状态'), tr('已完成'), tr('生成中'), tr('失败'), tr('重复'), tr('提交待确认'), tr('已取消'), tr('已跳过')]); header.addWidget(self.filter_box)
        self.open_button = PushButton(FIF.FOLDER, tr('打开输出文件夹'))
        self.open_button.clicked.connect(self.open_output)
        header.addWidget(self.open_button); root.addLayout(header)
        columns = [tr('序号'), tr('产品'), tr('任务名'), tr('模型'), tr('状态'), tr('提交时间'), tr('完成时间'), tr('文件大小'), tr('操作')]
        self.table = TableWidget(); self.table.setColumnCount(len(columns)); self.table.setHorizontalHeaderLabels(columns)
        self.table.setBorderVisible(True); self.table.setBorderRadius(8); self.table.setEditTriggers(self.table.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self.table.verticalHeader().setDefaultSectionSize(54)
        root.addWidget(self.table, 1)
        self.empty_view = self._build_empty_view()
        root.addWidget(self.empty_view, 1)
        self.footer = QWidget(); footer_row = QHBoxLayout(self.footer); footer_row.setContentsMargins(2, 0, 2, 0)
        self.footer_left = CaptionLabel(''); footer_row.addWidget(self.footer_left); footer_row.addStretch(1)
        from core.version import APP_VERSION
        self.footer_right = CaptionLabel(tr('显示最近 100 条') + f' · v{APP_VERSION}'); footer_row.addWidget(self.footer_right)
        root.addWidget(self.footer)
        self.filter_box.currentTextChanged.connect(self._filter)
        self.update_history(config_manager.history_records())

    def _build_empty_view(self):
        """空态三件套：图标 + 说明 + 主操作（去工作台创建任务）。"""
        view = QWidget()
        column = QVBoxLayout(view)
        column.setContentsMargins(0, 24, 0, 24)
        column.setSpacing(8)
        column.addStretch(1)
        glyph = IconWidget(FIF.HISTORY)
        glyph.setFixedSize(40, 40)  # 空态插画档（图标三档之外的展示尺寸）
        column.addWidget(glyph, 0, Qt.AlignHCenter)
        title = StrongBodyLabel(tr('暂无历史记录'))
        title.setAlignment(Qt.AlignCenter)
        column.addWidget(title)
        hint = CaptionLabel(tr('完成的任务会显示在这里，可先到工作台开始创作'))
        hint.setAlignment(Qt.AlignCenter)
        column.addWidget(hint)
        self.empty_action = PushButton(FIF.ADD, tr('去工作台创建任务'))
        self.empty_action.clicked.connect(lambda: self.create_requested.emit())
        column.addWidget(self.empty_action, 0, Qt.AlignHCenter)
        column.addStretch(1)
        return view

    @staticmethod
    def _tag_colors(status):
        """状态胶囊配色：与样张一致的浅底深字标签（全程 rgba，避免颜色字面量）。"""
        mapping = {
            'completed': ('rgba(52,199,89,0.16)', 'rgba(30,140,70,1)'),
            'duplicate': ('rgba(139,92,246,0.14)', 'rgba(124,80,230,1)'),
            'failed': ('rgba(239,68,68,0.14)', 'rgba(200,55,55,1)'),
            'submission_unknown': ('rgba(240,169,59,0.18)', 'rgba(170,110,25,1)'),
            'cancelled': ('rgba(122,130,148,0.14)', 'rgba(105,113,130,1)'),
            'skipped': ('rgba(122,130,148,0.14)', 'rgba(105,113,130,1)'),
        }
        return mapping.get(status, ('rgba(74,141,255,0.15)', 'rgba(52,110,210,1)'))

    def update_history(self, records):
        # 只渲染最近 100 条（页脚已承诺）；数据未变化时短路，避免整表重建与控件重挂。
        total = len(records)
        display = list(reversed(records))[:HISTORY_DISPLAY_LIMIT]
        keys = [(r.get('local_id'), r.get('status'), r.get('finished_at'),
                 bool(r.get('result_path')), r.get('size_bytes') or 0) for r in display]
        if keys == getattr(self, '_display_keys', None) and total == getattr(self, '_total', -1):
            return
        self._display_keys = keys
        self._total = total
        self.records = display
        has_records = bool(self.records)
        self.table.setVisible(has_records)
        self.empty_view.setVisible(not has_records)
        gb = sum((t.get('size_bytes') or 0) for t in records) / 1024**3
        self.footer_left.setText(tr('共 {n} 条记录 · 存储占用 {gb:.1f} GB').format(n=total, gb=gb))
        self.footer.setVisible(has_records)
        # 显式销毁旧单元格控件：setCellWidget 挂载的部件不随 setRowCount(0) 释放。
        for row in range(self.table.rowCount()):
            for column in range(self.table.columnCount()):
                widget = self.table.cellWidget(row, column)
                if widget is not None:
                    widget.setParent(None); widget.deleteLater()
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.records))
        self._row_status = []
        for row, task in enumerate(self.records):
            status = tr(STATUS_TEXT.get(task.get('status'), task.get('status', '—')))
            self._row_status.append(status)
            values = [str(len(self.records)-row), task.get('product', '') or '—', task.get('prompt_name', ''), task.get('model', ''), status,
                      task.get('created_at', '').replace('T', '\n') or '—', task.get('finished_at', '').replace('T', '\n') or '—',
                      f"{task['size_bytes']/1024/1024:.1f} MB" if task.get('size_bytes') else '—']
            for column, value in enumerate(values):
                if column == 4:
                    tag = QLabel(value); tag.setAlignment(Qt.AlignCenter)
                    bg, fg = self._tag_colors(task.get('status'))
                    tag.setStyleSheet(f'color:{fg}; background:{bg}; border-radius:9px; padding:1px 10px;')
                    wrap = QWidget(); wrap_row = QHBoxLayout(wrap); wrap_row.setContentsMargins(0, 6, 0, 6)
                    wrap_row.addWidget(tag, 0, Qt.AlignCenter)
                    self.table.setCellWidget(row, 4, wrap)
                    continue
                item = QTableWidgetItem(value); item.setTextAlignment(Qt.AlignCenter)
                item.setToolTip(task.get('error', '') or task.get('task_id', ''))
                self.table.setItem(row, column, item)
            action = QWidget(); layout = QHBoxLayout(action); layout.setContentsMargins(4, 2, 4, 2)
            open_file = TransparentToolButton(FIF.FOLDER); open_file.setToolTip(tr('打开文件'))
            open_file.setEnabled(bool(task.get('result_path')))
            open_file.clicked.connect(lambda _, t=task: open_local(t.get('result_path'), self.log_callback))
            retry = TransparentToolButton(FIF.SYNC); retry.setToolTip(tr('重新查询并下载（不创建新任务）'))
            retry.setEnabled(bool(task.get('task_id')))
            retry.clicked.connect(lambda _, t=task: self.redownload_requested.emit(t))
            layout.addWidget(open_file); layout.addWidget(retry)
            if task.get('status') == 'submission_unknown':
                resolve = TransparentToolButton(FIF.INFO)
                resolve.setToolTip(tr('处理待确认提交'))
                resolve.clicked.connect(lambda _, t=task: self.resolve_requested.emit(t))
                layout.addWidget(resolve)
            elif task.get('status') in {'completed', 'duplicate'}:
                regenerate = TransparentToolButton(FIF.PLAY)
                regenerate.setToolTip(tr('重新生成（需确认，会创建新任务）'))
                regenerate.clicked.connect(lambda _, t=task: self.regenerate_requested.emit(t))
                layout.addWidget(regenerate)
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
        statuses = getattr(self, '_row_status', [])
        for row in range(self.table.rowCount()):
            text = statuses[row] if row < len(statuses) else ''
            self.table.setRowHidden(row, status != tr('全部状态') and text != status)
