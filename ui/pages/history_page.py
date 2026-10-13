"""Persisted task records and manual download recovery in a Fluent table."""
from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QSize
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QHeaderView, QTableWidgetItem, QLabel, QStyle, QStyleOptionButton, QStyleOptionViewItem
from ..responsive import CoalescedCall, WrapLayout, scrollable_body
from ..tokens import px
from qfluentwidgets import CheckBox, ComboBox, InfoBar, PushButton, TableWidget, TableItemDelegate, TitleLabel, CaptionLabel, TransparentToolButton, IconWidget, StrongBodyLabel, setCustomStyleSheet, FluentIcon as FIF
from core.background import BackgroundJobs
from core.task_manager import STATUS_TEXT
from core.i18n import tr
from ..file_actions import open_local
from ..history_cleanup import DELETABLE_STATUSES, output_summary
from ..components.history_dialog import DeleteHistoryDialog
from ..components.studio_dialog import is_dialog_alive, safe_delete_dialog
from ..tokens import interface_scale

HISTORY_DISPLAY_LIMIT = 100  # 历史页只渲染最近 N 条（页脚文案同步承诺）


def _stamp(value):
    """时间戳单行化：'2026-09-22T10:01:00' → '09-22 10:01'（列宽友好，不折行）。"""
    text = str(value or '').replace('T', ' ')
    return text[5:16] if len(text) >= 16 else (text or '—')


class SelectionCheckBox(CheckBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        rules = 'CheckBox { min-width: 0px; }'
        setCustomStyleSheet(self, rules, rules)
        self.ensurePolished()

    def nextCheckState(self):
        self.setCheckState(Qt.Unchecked if self.checkState() == Qt.Checked else Qt.Checked)


class HistoryItemDelegate(TableItemDelegate):
    def updateEditorGeometry(self, editor, option, index):
        if index.column() == 0:
            editor.setGeometry(option.rect)
        else:
            super().updateEditorGeometry(editor, option, index)

    def _drawIndicator(self, painter, option, index):
        if index.column() != 0:
            super()._drawIndicator(painter, option, index)

    def paint(self, painter, option, index):
        if index.column() == 0:
            option = QStyleOptionViewItem(option)
            option.state &= ~QStyle.State_HasFocus
        super().paint(painter, option, index)


class SelectionHeader(QHeaderView):
    def __init__(self, parent):
        super().__init__(Qt.Horizontal, parent)
        self.checkBox = SelectionCheckBox(self.viewport())
        option = QStyleOptionButton()
        option.initFrom(self.checkBox)
        indicator = self.checkBox.style().subElementRect(QStyle.SE_CheckBoxIndicator, option, self.checkBox)
        self._checkbox_size = indicator.size() + QSize(2, 2)
        self.checkBox.setFixedSize(self._checkbox_size)
        self.checkBox.setTristate(True)
        self.checkBox.setToolTip(tr('全选当前显示且可删除的记录'))
        self.sectionResized.connect(self._position)
        self.sectionMoved.connect(self._position)
        self.geometriesChanged.connect(self._position)

    def _position(self, *_):
        visible = self.count() > 0 and not self.isSectionHidden(0)
        self.checkBox.setVisible(visible)
        if not visible:
            return
        width = min(round(self._checkbox_size.width() * interface_scale()), self.sectionSize(0))
        height = min(round(self._checkbox_size.height() * interface_scale()), self.viewport().height())
        self.checkBox.setFixedSize(width, height)
        self.checkBox.move(self.sectionViewportPosition(0) + (self.sectionSize(0) - width) // 2,
                           (self.viewport().height() - height) // 2)

    def refresh_interface_scale(self):
        self._position()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position()

    def scrollContentsBy(self, horizontal, vertical):
        super().scrollContentsBy(horizontal, vertical)
        self._position()


class HistoryPage(QWidget):
    redownload_requested = pyqtSignal(object)
    resolve_requested = pyqtSignal(object)
    batch_resolve_requested = pyqtSignal(object)
    regenerate_requested = pyqtSignal(object)
    create_requested = pyqtSignal()
    delete_requested = pyqtSignal(object, bool)
    clean_cache_requested = pyqtSignal()

    def __init__(self, log_callback, config_manager, parent=None):
        super().__init__(parent)
        self.setObjectName('historyPage')
        self.log_callback = log_callback
        self.config_manager = config_manager
        self._recovery_busy = False
        self._maintenance_busy = False
        self._selected_ids = set()
        self._all_records = []
        self.jobs = BackgroundJobs(self)
        root = scrollable_body(self); root.setContentsMargins(26, 16, 26, 20); root.setSpacing(16)
        header = WrapLayout(); title_box = QVBoxLayout(); title_box.addWidget(TitleLabel(tr('任务历史')))
        caption = CaptionLabel(tr('查看生成记录 · 重新下载会沿用 task_id，不重复提交'))
        caption.setWordWrap(True)
        title_box.addWidget(caption)
        header.addLayout(title_box); header.addStretch(1)
        self.filter_box = ComboBox(); self.filter_box.addItems([tr('全部状态'), tr('等待中'), tr('生成中'), tr('上传中'), tr('提交中'), tr('下载中'), tr('重试中'), tr('等待冷却'), tr('已暂停'), tr('已完成'), tr('失败'), tr('重复'), tr('提交待确认'), tr('已取消'), tr('已跳过')]); self.filter_box.setFixedHeight(32); self.filter_box.setMaximumWidth(180); header.addWidget(self.filter_box)
        self.open_button = PushButton(FIF.FOLDER, tr('打开输出文件夹'))
        self.open_button.clicked.connect(self.open_output)
        self.batch_resolve_button = PushButton(FIF.INFO, tr('批量处理待确认提交'))
        self.batch_resolve_button.setToolTip(tr('当前没有待确认提交'))
        self.batch_resolve_button.clicked.connect(self._request_batch_resolve)
        header.addWidget(self.batch_resolve_button)
        header.addWidget(self.open_button); root.addLayout(header)
        actions = WrapLayout()
        self.delete_button = PushButton(FIF.DELETE, tr('批量删除（0）'))
        self.delete_button.clicked.connect(self._request_delete)
        self.clean_cache_button = PushButton(FIF.SYNC, tr('清理缓存'))
        self.clean_cache_button.setToolTip(tr('清理输出、参考图目录及临时目录中超过1天的缓存；保留视频和参考图片'))
        self.clean_cache_button.clicked.connect(self.clean_cache_requested.emit)
        actions.addWidget(self.delete_button)
        actions.addWidget(self.clean_cache_button)
        actions.addStretch(1)
        root.addLayout(actions)
        columns = ['', tr('序号'), tr('产品'), tr('任务名'), tr('模型'), tr('状态'), tr('提交时间'), tr('完成时间'), tr('文件大小'), tr('操作')]
        self.table = TableWidget()
        self.table.setItemDelegate(HistoryItemDelegate(self.table))
        self.selection_header = SelectionHeader(self.table)
        self.table.setHorizontalHeader(self.selection_header)
        self.select_all = self.selection_header.checkBox
        self.select_all.stateChanged.connect(self._select_all)
        self.table.setColumnCount(len(columns)); self.table.setHorizontalHeaderLabels(columns)
        # 对齐规范：产品/任务名/模型左、提交/完成/大小右、其余居中（与数据行一致）。
        for column in range(len(columns)):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is None:
                continue
            if column in (2, 3, 4):
                header_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            elif column in (6, 7, 8):
                header_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            else:
                header_item.setTextAlignment(Qt.AlignCenter)
        self.table.setBorderVisible(True); self.table.setBorderRadius(8); self.table.setEditTriggers(self.table.NoEditTriggers)
        self.table.setWordWrap(False)  # 单行显示：长内容截断而不折行（行高恒定 54）
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setMinimumSectionSize(44)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.verticalHeader().setDefaultSectionSize(54)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self._selection_scale = interface_scale()
        self.table.setColumnWidth(0, px(92))
        self.table.setMinimumHeight(240)
        self._column_relayout = CoalescedCall(self, self._fit_columns)
        root.addWidget(self.table, 1)
        self.empty_view = self._build_empty_view()
        root.addWidget(self.empty_view, 1)
        self.footer = QWidget(); footer_row = WrapLayout(self.footer); footer_row.setContentsMargins(2, 0, 2, 0)
        self.footer_left = CaptionLabel(''); footer_row.addWidget(self.footer_left); footer_row.addStretch(1)
        from core.version import APP_VERSION
        self.footer_right = CaptionLabel(tr('显示最近 100 条') + f' · v{APP_VERSION}'); footer_row.addWidget(self.footer_right)
        root.addWidget(self.footer)
        self.filter_box.currentTextChanged.connect(self._filter)
        self.update_history(config_manager.history_records())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_column_relayout'):
            self._column_relayout.schedule()

    def refresh_interface_scale(self):
        scale = interface_scale()
        if self._selection_scale != scale:
            self._selection_scale = scale
            self.table.setColumnWidth(0, px(92))
        self._column_relayout.schedule()

    def _fit_columns(self):
        widths = [px(value) for value in (92, 72, 120, 180, 120, 110, 166, 166, 100, 160)]
        widths[0] = self.table.columnWidth(0)
        extra = max(0, self.table.viewport().width() - sum(widths))
        widths[3] += extra // 2
        widths[2] += extra - extra // 2
        for index, width in enumerate(widths):
            if self.table.columnWidth(index) != width:
                self.table.setColumnWidth(index, width)

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
        hint.setWordWrap(True)
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
        self._all_records = list(records)
        display = list(reversed(records))[:HISTORY_DISPLAY_LIMIT]
        keys = [(r.get('local_id'), r.get('product'), r.get('prompt_name'), r.get('model'),
                 r.get('status'), r.get('created_at'), r.get('finished_at'), r.get('result_path'),
                 r.get('task_id'), r.get('error'), r.get('size_bytes') or 0) for r in display]
        if keys == getattr(self, '_display_keys', None) and total == getattr(self, '_total', -1):
            gb = sum((task.get('size_bytes') or 0) for task in records) / 1024**3
            self.footer_left.setText(tr('共 {n} 条记录 · 存储占用 {gb:.1f} GB').format(n=total, gb=gb))
            self._sync_selection()
            return
        self._display_keys = keys
        self._total = total
        has_records = bool(display)
        self.table.setVisible(has_records)
        self.empty_view.setVisible(not has_records)
        gb = sum((t.get('size_bytes') or 0) for t in records) / 1024**3
        self.footer_left.setText(tr('共 {n} 条记录 · 存储占用 {gb:.1f} GB').format(n=total, gb=gb))
        self.footer.setVisible(has_records)
        has_pending = any(t.get('status') == 'submission_unknown' for t in records)
        self.batch_resolve_button.setEnabled(has_pending and not self._recovery_busy)
        self.batch_resolve_button.setToolTip(
            tr('待确认弹窗打开中，请先完成或关闭当前核对') if self._recovery_busy else
            tr('处理所有待确认提交；每条都需明确选择，不会静默重复提交')
            if has_pending else tr('当前没有待确认提交'))
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
            self._add_selection(row, task)
            for column, value in enumerate(values, 1):
                if column == 5:
                    tag = QLabel(value); tag.setAlignment(Qt.AlignCenter)
                    bg, fg = self._tag_colors(task.get('status'))
                    tag.setStyleSheet(f'color:{fg}; background:{bg}; border-radius:6px; padding:2px 10px;')
                    wrap = QWidget(); wrap_row = QHBoxLayout(wrap); wrap_row.setContentsMargins(0, 6, 0, 6)
                    wrap_row.addWidget(tag, 0, Qt.AlignCenter)
                    self.table.setCellWidget(row, 5, wrap)
                    continue
                item = QTableWidgetItem(value)
                if column in (2, 3, 4):
                    item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                elif column in (6, 7, 8):
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
                resolve.setEnabled(not self._recovery_busy)
                resolve.setToolTip(
                    tr('待确认弹窗打开中，请先完成或关闭当前核对') if self._recovery_busy else
                    tr('处理待确认提交'))
                resolve.clicked.connect(lambda _, t=task: self.resolve_requested.emit(t))
                layout.addWidget(resolve)
            elif task.get('status') in {'completed', 'duplicate'}:
                regenerate = TransparentToolButton(FIF.PLAY)
                regenerate.setToolTip(tr('重新生成（需确认，会创建新任务）'))
                regenerate.clicked.connect(lambda _, t=task: self.regenerate_requested.emit(t))
                layout.addWidget(regenerate)
            self.table.setCellWidget(row, len(values) + 1, action)
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
            self._update_selection(row, task)
            for column, value in enumerate(values, 1):
                if column == 5:
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
            action = self.table.cellWidget(row, len(values) + 1)
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
                if resolve is None:
                    resolve = next((button for button in buttons if button.toolTip() ==
                                    tr('待确认弹窗打开中，请先完成或关闭当前核对')), None)
                if current_resolve and resolve is None:
                    resolve = TransparentToolButton(FIF.INFO, action)
                    action.layout().addWidget(resolve)
                if current_resolve and resolve is not None:
                    resolve.setEnabled(not self._recovery_busy)
                    resolve.setToolTip(
                        tr('待确认弹窗打开中，请先完成或关闭当前核对') if self._recovery_busy else
                        tr('处理待确认提交'))
                    try:
                        resolve.clicked.disconnect()
                    except TypeError:
                        pass
                    resolve.clicked.connect(lambda _, t=task: self.resolve_requested.emit(t))
                elif not current_resolve and resolve is not None:
                    resolve.setParent(None); resolve.deleteLater()
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

    def set_recovery_busy(self, busy):
        """Disable history-page recovery entry points while a dialog is open."""
        self._recovery_busy = bool(busy)
        for row, task in enumerate(getattr(self, 'records', [])):
            self._update_selection(row, task)
        self._sync_selection()
        has_pending = any(t.get('status') == 'submission_unknown'
                          for t in self._all_records)
        self.batch_resolve_button.setEnabled(has_pending and not self._recovery_busy)
        self.batch_resolve_button.setToolTip(
            tr('待确认弹窗打开中，请先完成或关闭当前核对') if self._recovery_busy else
            tr('处理所有待确认提交；每条都需明确选择，不会静默重复提交')
            if has_pending else tr('当前没有待确认提交'))
        for row in range(self.table.rowCount()):
            action = self.table.cellWidget(row, self.table.columnCount() - 1)
            if action is None:
                continue
            for button in action.findChildren(TransparentToolButton):
                if button.toolTip() in {tr('处理待确认提交'),
                                        tr('待确认弹窗打开中，请先完成或关闭当前核对')}:
                    button.setEnabled(not self._recovery_busy)
                    button.setToolTip(
                        tr('待确认弹窗打开中，请先完成或关闭当前核对') if self._recovery_busy else
                        tr('处理待确认提交'))

    def _request_batch_resolve(self):
        pending = [task for task in self._all_records if task.get('status') == 'submission_unknown']
        if not pending:
            self.batch_resolve_button.setToolTip(tr('当前没有待确认提交；队列结束并产生待确认记录后可使用'))
            InfoBar.info(tr('没有待确认提交'), tr('当前无需核对；可继续创建新任务'), parent=self, duration=3500)
            return
        self.batch_resolve_requested.emit(None)

    def _add_selection(self, row, task):
        self.table.setItem(row, 0, QTableWidgetItem(''))
        host = QWidget()
        host.setObjectName('historySelectionHost')
        host.setAutoFillBackground(False)
        host.setStyleSheet('QWidget#historySelectionHost { background: transparent; border: none; }')
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        checkbox = SelectionCheckBox()
        checkbox.setObjectName('historySelection')
        checkbox.setFixedSize(self.selection_header._checkbox_size)
        layout.addWidget(checkbox, 0, Qt.AlignCenter)
        self.table.setCellWidget(row, 0, host)
        checkbox.stateChanged.connect(lambda state, key=task.get('local_id'): self._selection_changed(key, state))
        self._update_selection(row, task)

    def _update_selection(self, row, task):
        checkbox = self.table.cellWidget(row, 0).findChild(CheckBox)
        eligible = bool(task.get('local_id')) and task.get('status') in DELETABLE_STATUSES
        if not eligible:
            self._selected_ids.discard(task.get('local_id'))
        checkbox.blockSignals(True)
        checkbox.setChecked(task.get('local_id') in self._selected_ids)
        checkbox.blockSignals(False)
        checkbox.setEnabled(eligible and not self._maintenance_busy and not self._recovery_busy)
        checkbox.setToolTip(tr('正在清理或核对，请等待完成') if self._maintenance_busy or self._recovery_busy else
                            tr('选择此记录进行删除') if eligible else tr('任务仍在运行或排队，请先结束/暂停并结束队列后删除'))

    def _selection_changed(self, local_id, state):
        if state == Qt.Checked:
            self._selected_ids.add(local_id)
        else:
            self._selected_ids.discard(local_id)
        self._sync_selection()

    def _select_all(self, state):
        if state == Qt.PartiallyChecked:
            return
        for row, task in enumerate(getattr(self, 'records', [])):
            if not self.table.isRowHidden(row):
                checkbox = self.table.cellWidget(row, 0).findChild(CheckBox)
                if checkbox.isEnabled():
                    checkbox.setChecked(state == Qt.Checked)

    def _sync_selection(self):
        eligible = {task.get('local_id') for row, task in enumerate(getattr(self, 'records', []))
                    if not self.table.isRowHidden(row) and task.get('local_id') and task.get('status') in DELETABLE_STATUSES}
        self._selected_ids.intersection_update(eligible)
        for row, task in enumerate(getattr(self, 'records', [])):
            self._update_selection(row, task)
        count = len(self._selected_ids)
        busy = self._maintenance_busy or self._recovery_busy
        self.delete_button.setText(tr('批量删除（{count}）').format(count=count))
        self.delete_button.setEnabled(bool(count) and not busy)
        self.delete_button.setToolTip(tr('正在清理或核对，请等待完成') if busy else
                                     tr('请先勾选可删除的历史记录') if not count else tr('确认删除已选择的记录'))
        self.clean_cache_button.setEnabled(not busy)
        self.clean_cache_button.setToolTip(tr('正在清理或核对，请等待完成') if busy else
                                          tr('清理超过1天的临时缓存，保留视频与参考图片'))
        self.select_all.blockSignals(True)
        self.select_all.setCheckState(Qt.Checked if eligible and count == len(eligible) else
                                     Qt.PartiallyChecked if count else Qt.Unchecked)
        self.select_all.setEnabled(bool(eligible) and not busy)
        self.select_all.setToolTip(tr('正在清理或核对，请等待完成') if busy else
                                  tr('全选当前显示且可删除的记录') if eligible else tr('当前没有可删除的记录'))
        self.select_all.blockSignals(False)
        self.batch_resolve_button.setEnabled(not busy and any(
            task.get('status') == 'submission_unknown' for task in self._all_records))
        self.batch_resolve_button.setToolTip(tr('正在清理或核对，请等待完成') if busy else
                                            tr('处理所有待确认提交；每条都需明确选择') if any(
                                                task.get('status') == 'submission_unknown' for task in self._all_records)
                                            else tr('当前没有待确认提交'))

    def set_maintenance_busy(self, busy):
        self._maintenance_busy = bool(busy)
        for row, task in enumerate(getattr(self, 'records', [])):
            self._update_selection(row, task)
        self._sync_selection()

    def _request_delete(self):
        if self._maintenance_busy or self._recovery_busy:
            InfoBar.info(tr('当前无法删除'), tr('请等待当前清理或核对完成'), parent=self, duration=4000)
            return
        records = [dict(task) for task in getattr(self, 'records', []) if task.get('local_id') in self._selected_ids]
        if not records:
            InfoBar.info(tr('未选择记录'), tr('请先勾选可删除的历史记录'), parent=self, duration=4000)
            return
        workspace = getattr(self.window(), 'workspace_page', None)
        if workspace is not None and not workspace._maintenance_allowed():
            return
        if workspace is not None and not workspace._prepare_recovery_dialog(self._request_delete):
            return
        self.set_maintenance_busy(True)
        if workspace is not None:
            workspace._set_history_maintenance_busy(True)
            workspace._set_recovery_entry_busy(True)

        def done(summary):
            delete_files = False
            accepted = False
            dialog = None
            try:
                if workspace is not None and workspace.closing.is_set():
                    return
                dialog = DeleteHistoryDialog(records, summary, self.window())
                accepted = bool(dialog.exec()) and is_dialog_alive(dialog)
                if accepted:
                    delete_files = dialog.delete_files.isChecked()
                    accepted = not dialog.has_uncertain or dialog.uncertain_confirm.isChecked()
            finally:
                safe_delete_dialog(dialog)
                self.set_maintenance_busy(False)
                if workspace is not None:
                    workspace._set_recovery_entry_busy(False)
                    workspace._set_history_maintenance_busy(False)
            if accepted:
                self.delete_requested.emit(records, delete_files)

        def failed(message):
            self.set_maintenance_busy(False)
            if workspace is not None:
                workspace._set_recovery_entry_busy(False)
                workspace._set_history_maintenance_busy(False)
            InfoBar.error(tr('未打开删除确认'), message, parent=self, duration=6000)

        output_root = self.config_manager.config['paths'].get('output')
        try:
            self.jobs.start(lambda: output_summary(records, output_root), done, failed)
        except Exception as error:
            failed(str(error))

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
        self._sync_selection()
