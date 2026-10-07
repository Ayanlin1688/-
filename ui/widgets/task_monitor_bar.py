"""常驻任务监看带：把「正在跑什么、跑到哪、还有多久」提升到主界面第一屏。

数据由工作台喂入（与弹窗内 CurrentTaskCard 同源）：update_task / update_progress /
update_counts；空闲态用 show_idle()。完整详情仍在「生成参数与任务详情」弹窗。
配色统一取自 materials.MONITOR（颜色单一来源）。
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QFrame, QHBoxLayout

from qfluentwidgets import FluentIcon as FIF

from core.i18n import tr
from core.task_manager import STATUS_TEXT, ACTIVE
from ..components.custom_widgets import ProgressBar, PushButton
from ..materials import MONITOR, register_theme_callback, is_light
from .workspace_surface import BreathingDot, label, style_button, BLUE


def _dual(key):
    light_color, dark_color = MONITOR[key]
    return QColor(light_color), QColor(dark_color)


class TaskMonitorBar(QFrame):
    skip_clicked = pyqtSignal()
    details_clicked = pyqtSignal()
    resolve_clicked = pyqtSignal(object)

    _STATE_KEYS = {
        'failed': 'state_failed',
        'completed': 'state_completed',
        'processing': 'state_active',
        'uploading': 'state_active',
        'submitting': 'state_active',
        'downloading': 'state_active', 'queued': 'state_active', 'cooling': 'state_wait', 'paused': 'state_wait',
        'retry_wait': 'state_wait',
        'submission_unknown': 'state_wait',
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName('taskMonitorBar')
        self.setFixedHeight(62)
        self._apply_surface_style()
        self.task_info = {}

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 0, 14, 0)
        root.setSpacing(14)

        self.dot = BreathingDot(BLUE, active=False)
        self.dot.setFixedSize(14, 18)
        self.live_label = label(tr('当前任务'), 12, MONITOR['live'][1], True)
        self.live_label.setTextColor(*_dual('live'))
        root.addWidget(self.dot)
        root.addWidget(self.live_label)

        self.title = label(tr('尚未开始任务'), 13, MONITOR['title'][1], True)
        self.title.setTextColor(*_dual('title'))
        self.title.setMinimumWidth(170)
        root.addWidget(self.title)

        self.state_label = label('', 11, MONITOR['dim'][1])
        self.state_label.setTextColor(*_dual('dim'))
        root.addWidget(self.state_label)

        self.product_chip = label('', 11, MONITOR['dim'][1])
        self.model_chip = label('', 11, MONITOR['dim'][1])
        for chip in (self.product_chip, self.model_chip):
            chip.setObjectName('monitorChip')
            chip.setTextColor(*_dual('dim'))
            chip.setVisible(False)
        root.addWidget(self.product_chip)
        root.addWidget(self.model_chip)

        root.addSpacing(2)
        self.progress = ProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress, 1)

        self.percent = label('0%', 13, MONITOR['title'][1], True, mono=True)
        self.percent.setTextColor(*_dual('title'))
        self.percent.setFixedWidth(52)
        self.percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        root.addWidget(self.percent)

        self.timing = label('', 11, MONITOR['dim'][1], mono=True)
        self.timing.setTextColor(*_dual('dim'))
        root.addWidget(self.timing)

        self.skip_button = style_button(PushButton(FIF.SKIP_FORWARD, tr('跳过')))
        self.details_button = style_button(PushButton(FIF.INFO, tr('详情')))
        self.resolve_button = style_button(PushButton(FIF.INFO, tr('处理待确认')))
        for button in (self.skip_button, self.details_button, self.resolve_button):
            button.setFixedHeight(30)
        self.skip_button.setEnabled(False)
        self.skip_button.setToolTip(tr('当前没有可跳过的运行中任务'))
        self.skip_button.clicked.connect(self.skip_clicked)
        self.details_button.clicked.connect(self.details_clicked)
        self.resolve_button.setVisible(False)
        self.resolve_button.setToolTip(tr('当前没有待确认提交'))
        self.resolve_button.clicked.connect(lambda: self.resolve_clicked.emit(self.task_info))
        root.addWidget(self.skip_button)
        root.addWidget(self.details_button)
        root.addWidget(self.resolve_button)

        register_theme_callback(self._refresh_style)

    def _apply_surface_style(self):
        try:
            light = is_light()
        except Exception:
            light = False
        if light:
            self.setStyleSheet(
                '#taskMonitorBar{background:rgba(62,99,200,.07); border:1px solid rgba(62,99,200,.22); border-radius:10px;}'
                'QLabel#monitorChip{background:rgba(0,0,0,.04); border:1px solid rgba(0,0,0,.12); border-radius:6px; padding:2px 8px;}')
        else:
            self.setStyleSheet(
                '#taskMonitorBar{background:rgba(91,141,239,.10); border:1px solid rgba(91,141,239,.30); border-radius:10px;}'
                'QLabel#monitorChip{background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.16); border-radius:6px; padding:2px 8px;}')

    def _refresh_style(self):
        try:
            self._apply_surface_style()
        except RuntimeError:
            pass

    def update_counts(self, running, waiting):
        self._counts = (int(running), int(waiting))

    def update_task(self, index, task):
        task = dict(task or {})
        self.task_info = task
        status = task.get('status')
        if not status:
            self.show_idle()
            return
        failed = status == 'failed'
        self.title.setText(f"{index + 1:02d} · {task.get('prompt_name') or task.get('prompt_path') or ''}")
        self.title.setTextColor(*_dual('title_error' if failed else 'title'))
        self.state_label.setText(tr(STATUS_TEXT.get(status, status)))
        self.state_label.setTextColor(*_dual(self._STATE_KEYS.get(status, 'state_idle')))
        product = task.get('product') or ''
        self.product_chip.setText(product)
        self.product_chip.setVisible(bool(product))
        model = task.get('model') or task.get('requested_model') or ''
        self.model_chip.setText(model)
        self.model_chip.setVisible(bool(model))
        self.dot.active = status in ACTIVE
        self.dot.update()
        is_unknown = status == 'submission_unknown'
        self.resolve_button.setVisible(is_unknown)
        self.resolve_button.setEnabled(is_unknown)
        self.resolve_button.setToolTip(tr('处理该条提交待确认记录') if is_unknown else tr('当前没有待确认提交'))

    def update_progress(self, progress, elapsed, eta):
        self.progress.setValue(int(progress))
        self.percent.setText(f'{int(progress)}%')
        remaining = f'{eta:.0f} {tr("秒")}' if eta >= 0 else '—'
        self.timing.setText(f"{tr('已用')} {elapsed:.0f} {tr('秒')} · {tr('剩余')} {remaining}")

    def show_idle(self):
        self.task_info = {}
        self.title.setText(tr('尚未开始任务'))
        self.title.setTextColor(*_dual('title'))
        self.state_label.setText('')
        self.product_chip.setVisible(False)
        self.model_chip.setVisible(False)
        self.progress.setValue(0)
        self.percent.setText('0%')
        self.timing.setText('')
        self.dot.active = False
        self.dot.update()
        self.skip_button.setEnabled(False)
        self.skip_button.setToolTip(tr('当前没有可跳过的运行中任务'))
        self.resolve_button.setVisible(False)
        self.resolve_button.setEnabled(False)
        self.resolve_button.setToolTip(tr('当前没有待确认提交'))
