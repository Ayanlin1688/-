"""Incremental history rows for ecommerce groups, without model or file operations."""
from datetime import datetime

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget
from qfluentwidgets import CardWidget, PushButton, StrongBodyLabel

from core.i18n import tr
from .. import materials
from ..responsive import WrapLayout
from ..theme import style_controls
from ..tokens import px


RESUMABLE_STATUSES = frozenset(('partial', 'failed', 'analyzing', 'generating'))


def _label():
    label = QLabel()
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    return label


class EcommerceHistoryRow(QWidget):
    def __init__(self, group, panel):
        super().__init__(panel)
        self.panel = panel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, px(8), 0, px(8))
        self.name_label, self.platform_label = _label(), _label()
        self.status_label, self.time_label, self.summary_label = _label(), _label(), _label()
        for label in (self.name_label, self.platform_label, self.status_label,
                      self.time_label, self.summary_label):
            layout.addWidget(label)
        actions = WrapLayout(spacing=px(8))
        self.view_button = PushButton(tr('查看结果'))
        self.resume_button = PushButton(tr('继续生成（断点恢复）'))
        self.open_button = PushButton(tr('打开文件夹'))
        self.delete_button = PushButton(tr('删除记录'))
        for button, signal in ((self.view_button, panel.view_requested),
                               (self.resume_button, panel.resume_requested),
                               (self.open_button, panel.open_requested),
                               (self.delete_button, panel.delete_requested)):
            button.clicked.connect(lambda _checked=False, sig=signal: sig.emit(self.group['group_id']))
            actions.addWidget(button)
        layout.addLayout(actions)
        style_controls(self)
        self.update_group(group)

    def update_group(self, group):
        self.group = dict(group)
        self.name_label.setText(group.get('product_name') or group['group_id'][:8])
        self.platform_label.setText(' / '.join(p.get('name', '') for p in group.get('platforms', [])))
        self.status_label.setText(self.panel.stage_text(group['status']))
        try:
            created = datetime.fromtimestamp(group['created_at']).strftime('%Y-%m-%d %H:%M')
        except (ValueError, OSError, OverflowError, TypeError):
            created = ''
        self.time_label.setText(created)
        totals = group.get('totals') or {}
        self.summary_label.setText(tr('成功 {success} / 失败 {failed} / 跳过 {skipped} / 总数 {total}').format(
            success=totals.get('succeeded', 0), failed=totals.get('failed', 0),
            skipped=totals.get('skipped', 0), total=totals.get('total', 0)))
        self.set_busy(self.panel.busy)
        self.refresh_colors()

    def set_busy(self, busy):
        for button in (self.view_button, self.open_button, self.delete_button):
            button.setEnabled(not busy)
        self.resume_button.setEnabled(not busy and self.group['status'] in RESUMABLE_STATUSES)

    def refresh_colors(self):
        colors = materials.palette()
        color = {'failed': 'error', 'partial': 'warning', 'cancelled': 'text2',
                 'analyzing': 'accent', 'generating': 'accent'}.get(self.group['status'])
        self.status_label.setStyleSheet('color: %s;' % (colors[color] if color else materials.SUCCESS))
        for label in (self.name_label, self.platform_label, self.time_label, self.summary_label):
            label.setStyleSheet('color: %s;' % colors['text1'])


class EcommerceHistoryPanel(CardWidget):
    refresh_requested = pyqtSignal()
    view_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    open_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)

    def __init__(self, stage_text, parent=None):
        super().__init__(parent)
        self.stage_text = stage_text
        self.busy = False
        self.rows = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(18), px(16), px(18), px(16))
        header = WrapLayout()
        header.addWidget(StrongBodyLabel(tr('历史生成记录')))
        self.refresh_button = PushButton(tr('刷新'))
        self.refresh_button.clicked.connect(self.refresh_requested)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)
        self.notice_label = _label()
        self.empty_label = _label()
        self.empty_label.setText(tr('暂无历史记录'))
        layout.addWidget(self.notice_label)
        layout.addWidget(self.empty_label)
        self.rows_layout = QVBoxLayout()
        layout.addLayout(self.rows_layout)

    def update_groups(self, groups, persistence_available=True):
        keys = {group['group_id'] for group in groups}
        for key in list(self.rows):
            if key not in keys:
                row = self.rows.pop(key)
                self.rows_layout.removeWidget(row)
                row.hide()
                row.deleteLater()
        ordered = {}
        for index, group in enumerate(groups):
            key = group['group_id']
            row = self.rows.get(key)
            if row is None:
                row = EcommerceHistoryRow(group, self)
            else:
                row.update_group(group)
            self.rows_layout.insertWidget(index, row)
            ordered[key] = row
        self.rows = ordered
        self.empty_label.setVisible(not groups)
        self.notice_label.setText('' if persistence_available else tr('数据库不可用，仅展示本次会话结果'))
        self.notice_label.setVisible(not persistence_available)
        self.refresh_colors()

    def set_busy(self, busy):
        self.busy = bool(busy)
        for row in self.rows.values():
            row.set_busy(self.busy)

    def refresh_colors(self):
        self.notice_label.setStyleSheet('color: %s;' % materials.palette()['warning'])
        self.empty_label.setStyleSheet('color: %s;' % materials.palette()['text2'])
        for row in self.rows.values():
            row.refresh_colors()
