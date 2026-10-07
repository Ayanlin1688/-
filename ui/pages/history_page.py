"""Persisted task records and manual download recovery in a Fluent table."""
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QHeaderView, QTableWidgetItem, QLabel
from qfluentwidgets import ComboBox, PushButton, TableWidget, TitleLabel, CaptionLabel, TransparentToolButton, IconWidget, StrongBodyLabel, FluentIcon as FIF
from core.task_manager import STATUS_TEXT
from core.i18n import tr
from ..file_actions import open_local

HISTORY_DISPLAY_LIMIT = 100  # 历史页只渲染最近 N 条（页脚文案同步承诺）


def _stamp(value):
    """时间戳单行化：'2026-09-22T10:01:00' → '09-22 10:01'（列宽友好，不折行）。"""
    text = str(value or '').replace('T', ' ')
    return text[5:16] if len(text) >= 16 else (text or '—')


class HistoryPage(QWidget):
    redownload_requested = pyqtSignal(object)
    resolve_requested = pyqtSignal(object)
    batch_resolve_requested = pyqtSignal(object)
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
        self.filter_box = ComboBox(); self.filter_box.addItems([tr('全部状态'), tr('等待中'), tr('生成中'), tr('上传中'), tr('提交中'), tr('下载中'), tr('重试中'), tr('等待冷却'), tr('已暂停'), tr('已完成'), tr('失败'), tr('重复'), tr('提交待确认'), tr('已取消'), tr('已跳过')]); self.filter_box.setFixedHeight(32); self.filter_box.setMaximumWidth(180); header.addWidget(self.filter_box)
        self.open_button = PushButton(FIF.FOLDER, tr('打开输出文件夹'))
        self.open_button.clicked.connect(self.open_output)
        self.batch_resolve_button = PushButton(FIF.INFO, tr('批量处理待确认提交'))
        self.batch_resolve_button.setToolTip(tr('当前没有待确认提交'))
        self.batch_resolve_button.clicked.connect(self._request_batch_resolve)
        header.addWidget(self.batch_resolve_button)
        header.addWidget(self.open_button); root.addLayout(header)
        columns = [tr('序号'), tr('产品'), tr('任务名'), tr('模型'), tr('状态'), tr('提交时间'), tr('完成时间'), tr('文件大小'), tr('操作')]
        self.table = TableWidget(); self.table.setColumnCount(len(columns)); self.table.setHorizontalHeaderLabels(columns)
        # 对齐规范：产品/任务名/模型左、提交/完成/大小右、其余居中（与数据行一致）。
        for column in range(len(columns)):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is None:
                continue
            if column in (1, 2, 3):
                header_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            elif column in (5, 6, 7):
                header_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            else:
                header_item.setTextAlignment(Qt.AlignCenter)
        self.table.setBorderVisible(True); self.table.setBorderRadius(8); self.table.setEditTriggers(self.table.NoEditTriggers)
        self.table.setWordWrap(False)  # 单行显示：长内容截断而不折行（行高恒定 54）
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
        keys = [(r.get('local_id'), r.get('product'), r.get('prompt_name'), r.get('model'),
                 r.get('status'), r.get('created_at'), r.get('finished_at'), r.get('result_path'),
                 r.get('task_id'), r.get('error'), r.get('size_bytes') or 0) for r in display]
        if keys == getattr(self, '_display_keys', None) and total == getattr(self, '_total', -1):
            return
        self._display_keys = keys
        self._total = total
        has_records = bool(display)
        self.table.setVisible(has_records)
        self.empty_view.setVisible(not has_records)
        gb = sum((t.get('size_bytes') or 0) for t in records) / 1024**3
        self.footer_left.setText(tr('共 {n} 条记录 · 存储占用 {gb:.1f} GB').format(n=total, gb=gb))
        self.footer.setVisible(has_records)
        self.batch_resolve_button.setEnabled(any(t.get('status') == 'submission_unknown' for t in records))
        self.batch_resolve_button.setToolTip(
            tr('处理所有待确认提交；每条都需明确选择，不会静默重复提交')
            if self.batch_resolve_button.isEnabled() else tr('当前没有待确认提交'))
        if self._try_update_existing_rows(display):
            self._filter(self.filter_box.currentText())
            return
        self.records = display
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
                      _stamp(task.get('created_at')), _stamp(task.get('finished_at')),
                      f"{task['size_bytes']/1024/1024:.1f} MB" if task.get('size_bytes') else '—']
            for column, value in enumerate(values):
                if column == 4:
                    tag = QLabel(value); tag.setAlignment(Qt.AlignCenter)
                    bg, fg = self._tag_colors(task.get('status'))
                    tag.setStyleSheet(f'color:{fg}; background:{bg}; border-radius:6px; padding:2px 10px;')
                    wrap = QWidget(); wrap_row = QHBoxLayout(wrap); wrap_row.setContentsMargins(0, 6, 0, 6)
                    wrap_row.addWidget(tag, 0, Qt.AlignCenter)
                    self.table.setCellWidget(row, 4, wrap)
                    continue
                item = QTableWidgetItem(value)
                if column in (1, 2, 3):
                    item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                elif column in (5, 6, 7):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    item.setTextAlignment(Qt.AlignCenter)
                item.setToolTip(task.get('error', '') or task.get('task_id', ''))
                self.table.setItem(row, column, item)
            action = QWidget(); layout = QHBoxLayout(action); layout.setContentsMargins(4, 2, 4, 2)
            open_file = TransparentToolButton(FIF.FOLDER)
            open_file.setToolTip(tr('打开文件') if task.get('result_path') else tr('打开文件（暂无本地文件，任务完成下载后可打开）'))
            open_file.setEnabled(bool(task.get('result_path')))
            open_file.clicked.connect(lambda _, t=task: open_local(t.get('result_path'), self.log_callback))
            retry = TransparentToolButton(FIF.SYNC)
            retry.setToolTip(tr('重新查询并下载（不创建新任务）') if task.get('task_id') else
                             tr('当前没有可查询的远端 task_id'))
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

    def _try_update_existing_rows(self, records):
        """Update table cell contents in place when the displayed task identities are stable."""
        if len(records) != self.table.rowCount() or not records:
            return False
        previous = getattr(self, 'records', [])
        if len(previous) != len(records) or any(
                old.get('local_id') != new.get('local_id')
                for old, new in zip(previous, records)):
            return False
        self.records = records
        self._row_status = []
        for row, task in enumerate(records):
            status = tr(STATUS_TEXT.get(task.get('status'), task.get('status', '—')))
            self._row_status.append(status)
            values = [str(len(records)-row), task.get('product', '') or '—', task.get('prompt_name', ''), task.get('model', ''), status,
                      _stamp(task.get('created_at')), _stamp(task.get('finished_at')),
                      f"{task['size_bytes']/1024/1024:.1f} MB" if task.get('size_bytes') else '—']
            for column, value in enumerate(values):
                if column == 4:
                    tag_host = self.table.cellWidget(row, column)
                    tag = tag_host.findChild(QLabel) if tag_host else None
                    if tag is not None:
                        tag.setText(value)
                        bg, fg = self._tag_colors(task.get('status'))
                        tag.setStyleSheet(f'color:{fg}; background:{bg}; border-radius:6px; padding:2px 10px;')
                    continue
                item = self.table.item(row, column)
                if item is not None:
                    item.setText(value)
                    item.setToolTip(task.get('error', '') or task.get('task_id', ''))
            action = self.table.cellWidget(row, len(values))
            if action is not None:
                buttons = action.findChildren(TransparentToolButton)
                if len(buttons) >= 2:
                    open_file, retry = buttons[:2]
                    for button in (open_file, retry):
                        try:
                            button.clicked.disconnect()
                        except TypeError:
                            pass
                    open_file.setEnabled(bool(task.get('result_path')))
                    open_file.setToolTip(tr('打开文件') if task.get('result_path') else tr('打开文件（暂无本地文件，任务完成下载后可打开）'))
                    open_file.clicked.connect(lambda _, t=task: open_local(t.get('result_path'), self.log_callback))
                    retry.setEnabled(bool(task.get('task_id')))
                    retry.setToolTip(tr('重新查询并下载（不创建新任务）') if task.get('task_id') else tr('当前没有可查询的远端 task_id'))
                    retry.clicked.connect(lambda _, t=task: self.redownload_requested.emit(t))
                current_resolve = task.get('status') == 'submission_unknown'
                resolve = next((button for button in buttons if button.toolTip() == tr('处理待确认提交')), None)
                if current_resolve and resolve is None:
                    resolve = TransparentToolButton(FIF.INFO, action)
                    resolve.setToolTip(tr('处理待确认提交'))
                    resolve.clicked.connect(lambda _, t=task: self.resolve_requested.emit(t))
                    action.layout().addWidget(resolve)
                elif not current_resolve and resolve is not None:
                    resolve.setParent(None); resolve.deleteLater()
                elif resolve is not None:
                    try:
                        resolve.clicked.disconnect()
                    except TypeError:
                        pass
                    resolve.clicked.connect(lambda _, t=task: self.resolve_requested.emit(t))
                regenerate = next((button for button in buttons if button.toolTip() == tr('重新生成（需确认，会创建新任务）')), None)
                if task.get('status') in {'completed', 'duplicate'} and regenerate is None:
                    regenerate = TransparentToolButton(FIF.PLAY, action)
                    regenerate.setToolTip(tr('重新生成（需确认，会创建新任务）'))
                    regenerate.clicked.connect(lambda _, t=task: self.regenerate_requested.emit(t))
                    action.layout().addWidget(regenerate)
                elif task.get('status') not in {'completed', 'duplicate'} and regenerate is not None:
                    regenerate.setParent(None); regenerate.deleteLater()
                elif regenerate is not None:
                    try:
                        regenerate.clicked.disconnect()
                    except TypeError:
                        pass
                    regenerate.clicked.connect(lambda _, t=task: self.regenerate_requested.emit(t))
        return True

    def _request_batch_resolve(self):
        pending = [task for task in getattr(self, 'records', []) if task.get('status') == 'submission_unknown']
        if not pending:
            self.batch_resolve_button.setToolTip(tr('当前没有待确认提交'))
            return
        self.batch_resolve_requested.emit(pending)

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
