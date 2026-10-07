"""Compact Fluent workspace task table."""
from pathlib import Path
from PyQt5.QtCore import Qt, QRectF, QSize, QPoint, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QListWidgetItem, QSizePolicy
from qfluentwidgets import (ListWidget, ComboBox, ProgressBar, PushButton, PrimaryPushButton, TransparentToolButton, RoundMenu, Action, IconWidget, FluentIcon as FIF, InfoBar)
from qfluentwidgets.common.config import isDarkTheme
from core.task_manager import STATUS_TEXT, ACTIVE, TERMINAL
from core.i18n import tr
from core.prompt_detector import short_model_name
from core.task_state import submission_images
from .task_queue_panel import TaskQueuePanel
from .workspace_surface import WorkspaceCard, BreathingDot, ReferenceStrip, label, style_button, BLUE, GREEN, RED, YELLOW, MUTED
from ..motion import Shimmer

# ---------------------------------------------------------------------------
# 单一列宽来源：表头与数据行共用同一组列定义与同一套解析结果，
# 列宽随容器宽度内容感知地分配（权重 + 上限），避免单列吃掉全部余量。
# (key, min, max, weight, align)  weight=0 表示定宽列
COLUMN_SPEC = [
    ('number', 44, 44, 0, 'left'),
    ('title', 190, 340, 3, 'left'),
    ('product', 94, 170, 1, 'left'),
    ('model', 96, 150, 1, 'left'),
    ('ratio', 52, 64, 0, 'left'),
    ('resolution', 62, 76, 0, 'left'),
    ('duration', 46, 58, 0, 'left'),
    ('images', 76, 88, 0, 'left'),
    ('progress', 118, 230, 2, 'left'),
    ('timing', 96, 150, 1, 'left'),
    ('status', 84, 116, 0, 'left'),
    ('actions', 142, 142, 0, 'center'),
]
HEADERS = ['#', '提示词', '产品', '模型', '比例', '分辨率', '时长', '参考图', '进度', '用时/剩余', '状态', '']
CELL_SPACING = 16


def resolve_widths(total_width):
    """在给定内容宽度下解析各列宽度（权重分配 + 上限 + 余量均摊 + 不足时收缩）。"""
    count = len(COLUMN_SPEC)
    available = max(0, int(total_width) - CELL_SPACING * (count - 1))
    widths = [spec[1] for spec in COLUMN_SPEC]
    flex = [(i, spec) for i, spec in enumerate(COLUMN_SPEC) if spec[3] > 0]
    fixed = sum(spec[1] for spec in COLUMN_SPEC if spec[3] == 0)
    extra = available - fixed - sum(spec[1] for _, spec in flex)
    if extra > 0 and flex:
        weight_sum = sum(spec[3] for _, spec in flex)
        for index, spec in flex:
            widths[index] = min(spec[2], spec[1] + extra * spec[3] / weight_sum)
        leftover = available - fixed - sum(widths[index] for index, _ in flex)
        if leftover > 1:
            share = leftover / len(flex)
            for index, _ in flex:
                widths[index] += share
    elif extra < 0 and flex:
        # 空间不足：先按权重从弹性列收缩，再按“宽列先减”补齐保底后的缺口。
        # 输出始终适配可用宽度，避免 QGridLayout 压缩依赖行内控件弹性导致表头与数据列漂移。
        deficit = -extra
        weight_sum = sum(spec[3] for _, spec in flex)
        for index, spec in flex:
            floor = min(spec[1], 56)
            widths[index] = max(floor, int(spec[1] - deficit * spec[3] / weight_sum))
        leftover = available - fixed - sum(widths[index] for index, _ in flex)
        if leftover < 0:
            for index, spec in sorted(flex, key=lambda pair: widths[pair[0]], reverse=True):
                if leftover >= 0:
                    break
                floor = min(spec[1], 56)
                take = min(max(0, widths[index] - floor), -leftover)
                if take:
                    widths[index] -= take
                    leftover += take
    return [max(0, int(round(value))) for value in widths]


def apply_grid_columns(grid, widths):
    for index, width in enumerate(widths):
        grid.setColumnMinimumWidth(index, width)
        grid.setColumnStretch(index, 0)
    # 尾随吸收列兜住取整误差，保证表头与数据行的可见列边界完全一致。
    grid.setColumnMinimumWidth(len(widths), 0)
    grid.setColumnStretch(len(widths), 1)


def state_color(s): return GREEN if s=='completed' else RED if s=='failed' else BLUE if s in ACTIVE else YELLOW if s in {'retry_wait','cooldown_wait','submission_unknown'} else MUTED


class TaskProgress(ProgressBar):
    def __init__(self, p=None):
        super().__init__(p)
        self.setFixedHeight(4)
        self.setProperty('studioStyled', True)
        self.setCustomBarColor(BLUE, BLUE)
        self._studio_shimmer = Shimmer(self)

    def paintEvent(self, event):
        # 渐变填充：运行中蓝→紫，失败红渐变；圆角 2px（高度 4px / 半径 2px）。
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        track = self.darkBackgroundColor if isDarkTheme() else self.lightBackgroundColor
        painter.setPen(track)
        y = self.height() // 2
        painter.drawLine(0, y, self.width(), y)
        if self.minimum() >= self.maximum():
            return
        span = self.maximum() - self.minimum()
        width = int(self.val / span * self.width()) if span else 0
        if width <= 0:
            return
        painter.setPen(Qt.NoPen)
        if self.isError():
            painter.setBrush(QColor('#EF4444'))
        else:
            from ..materials import palette
            painter.setBrush(QColor(palette()['accent']))
        radius = self.height() / 2
        painter.drawRoundedRect(QRectF(0, 0, width, self.height()), radius, radius)


class ExpandedTaskRow(QWidget):
    action_requested=pyqtSignal(int,str); reorder_requested=pyqtSignal(int,object); preview_requested=pyqtSignal(str)
    def __init__(self,index,task,defaults,parent=None):
        super().__init__(parent); self.index=index; self.defaults=defaults; self.task={}; self.editable=True; self.busy=False; self._hover=False; self.setFixedHeight(52)
        root=QHBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        self.host=QWidget(); self.host.setFixedHeight(52); self.grid=QGridLayout(self.host); self.grid.setContentsMargins(16,0,16,0); self.grid.setHorizontalSpacing(CELL_SPACING)
        # 序号列：状态呼吸点 + 两位序号合为一格，省出的列位交给行尾操作按钮。
        number_box=QWidget(); nb=QHBoxLayout(number_box); nb.setContentsMargins(0,0,0,0); nb.setSpacing(6)
        self.dot=BreathingDot(MUTED); self.number=label(f'{index+1:02d}',12,MUTED,mono=True)
        nb.addWidget(self.dot); nb.addWidget(self.number,1)
        self.title=label('',13,'#f5f5f5',True); self.title.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred); self.product=label('',12,MUTED); self.product.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred); self.model_label=label('',11,BLUE,True)
        self.ratio=label('—',12,MUTED,mono=True); self.resolution=label('—',12,MUTED,mono=True); self.duration=label('—',12,MUTED,mono=True)
        self.images_button=style_button(PushButton()); self.images_button.setFixedSize(76,30); self.images_button.clicked.connect(lambda:self.action_requested.emit(self.index,'match'))
        self.images_button.setToolTip(tr('查看每个提示词绑定的参考图'))
        ph=QWidget(); pl=QHBoxLayout(ph); pl.setContentsMargins(0,0,0,0); pl.setSpacing(8); self.progress=TaskProgress(); self.percentage=label('0%',11,MUTED,mono=True); self.percentage.setFixedWidth(35); pl.addWidget(self.progress,1); pl.addWidget(self.percentage)
        self.timing=label('—',12,MUTED,mono=True)
        self.status_label=label('',11); self.status_label.setFixedHeight(22); self.status_label.setAlignment(Qt.AlignCenter)
        self.resolve_button=style_button(PushButton(FIF.INFO,tr('处理待确认'))); self.resolve_button.setFixedHeight(30); self.resolve_button.setToolTip(tr('处理该条提交待确认记录')); self.resolve_button.clicked.connect(lambda:self.action_requested.emit(self.index,'resolve'))
        self.more_button=style_button(TransparentToolButton(FIF.MORE)); self.more_button.setFixedSize(30,30); self.more_button.setToolTip(tr('更多操作')); self.more_button.setProperty('studioKeepWidth', True); self.more_button.clicked.connect(lambda:self.action_requested.emit(self.index,'menu'))
        action_box=QWidget(); action_layout=QHBoxLayout(action_box); action_layout.setContentsMargins(0,0,0,0); action_layout.setSpacing(4); action_layout.addWidget(self.resolve_button); action_layout.addWidget(self.more_button)
        cells=(number_box,self.title,self.product,self.model_label,self.ratio,self.resolution,self.duration,self.images_button,ph,self.timing,self.status_label,action_box)
        self._cells=cells
        # 对齐规范：文本列左、时间列右、比例/分辨率/时长居中（单元格逐格锁宽后由内容对齐决定观感）。
        self.ratio.setAlignment(Qt.AlignCenter); self.resolution.setAlignment(Qt.AlignCenter)
        self.duration.setAlignment(Qt.AlignCenter); self.percentage.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.timing.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for column,cell in enumerate(cells):
            if column in (10,11):
                grid_align=Qt.AlignVCenter | Qt.AlignHCenter
                self.grid.addWidget(cell,0,column,grid_align)
            else:
                self.grid.addWidget(cell,0,column)
        root.addWidget(self.host); self.update_task(task)
        # Backwards-compatible reference strip kept outside the compact row.
        # It preserves drag/reorder and preview APIs used by existing integrations
        # while the primary table remains a dense single-line design.
        self.references = ReferenceStrip(self)
        self.references.setGeometry(-1000, -1000, 320, 88)
        self.references.reordered.connect(lambda paths: self.reorder_requested.emit(self.index, paths))
        self.references.preview_requested.connect(self.preview_requested)
        self.references.set_paths(self.task.get('images', []))
        self.references.show()
        self.download_button = PushButton(self)
        self.download_button.setGeometry(-1000, -1000, 1, 1)
        self.download_button.clicked.connect(lambda: self.action_requested.emit(self.index, 'download'))

    def apply_widths(self, widths):
        apply_grid_columns(self.grid, widths)
        # 逐格锁宽：QGridLayout 的自动分配受控件尺寸提示影响，会与表头漂移；
        # min=max=列宽后列宽成为唯一解（标有 studioKeepWidth 的行尾按钮保持自身尺寸）。
        for column, cell in enumerate(self._cells):
            if cell.property('studioKeepWidth'):
                continue
            if cell.minimumWidth() != widths[column] or cell.maximumWidth() != widths[column]:
                cell.setMinimumWidth(widths[column]); cell.setMaximumWidth(widths[column])

    def _preview(self):
        if self.task.get('images'): self.preview_requested.emit(self.task['images'][0])

    def update_task(self,t):
        self.task=dict(t); s=t.get('status','waiting'); col=state_color(s); self.dot.active=s in ACTIVE; self.dot.setTextColor(col,col)
        name=Path(t.get('prompt_path','')).name or (t.get('prompt_name','')+'.txt'); self.title.setText(name)
        self.title.setToolTip(str(t.get('prompt_path','') or name))
        self.product.setText(tr(t.get('product') or '未分组'))
        model=t.get('model') or t.get('requested_model') or self.defaults.get('model',''); src=(' · '+tr('手动')) if t.get('model_source')=='manual' else (' · '+tr('自动')) if t.get('model_source')=='auto' else ''; short='V2' if model=='video-v2' else 'V3' if model=='video-v3' else short_model_name(model)
        self.model_label.setText(f'[{short}]'+src); self.model_label.setToolTip(model+src); self.model_label.setStyleSheet(self._model_chip_style(model))
        text=tr(STATUS_TEXT.get(s,s))
        if not t.get('images') and s in {'waiting', 'pending'}:
            text=tr('未匹配'); col=YELLOW
        chip=QColor(col)
        metrics=QFontMetrics(self.status_label.font())
        chip_width=min(112, metrics.horizontalAdvance(text)+20)
        limit=self.grid.columnMinimumWidth(10) or COLUMN_SPEC[10][1]
        self.status_label.setFixedWidth(min(chip_width, limit))
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f'color:{col};background:rgba({chip.red()},{chip.green()},{chip.blue()},0.14);border-radius:6px;padding:0 2px;')
        val=100 if s=='completed' else max(0,min(100,int(t.get('progress',0)))); self.progress.setValue(val); self.progress.setError(s=='failed'); self.percentage.setText(f'{val}%')
        p={**self.defaults,**(t.get('effective_parameters') or t.get('_display_parameters',{}))}; self.ratio.setText(str(p.get('aspect_ratio') or p.get('ratio') or '—')); self.resolution.setText(str(p.get('resolution') or '—')); d=p.get('duration'); self.duration.setText(f'{d}s' if d not in (None,'') else '—'); self.timing.setText('—'); self.images_button.setText(f'IMG ×{len(t.get("images",[]))}'); self.update()
        total = len(t.get('images', []))
        actual = t.get('submitted_image_count', t.get('_display_image_count'))
        if actual is None:
            actual = len(submission_images(t, model))
        self.images_button.setText(f'{actual}/{total}' if actual != total else f'IMG ×{total}')
        elapsed = t.get('poll_elapsed_seconds')
        if elapsed is not None:
            seconds = max(0, int(elapsed))
            self.timing.setText(f'{seconds//60}分{seconds%60}秒')
            self.timing.setToolTip(f'已轮询{seconds//60}分{seconds%60}秒')
        self.progress.setPaused(s not in ACTIVE)
        is_unknown = s == 'submission_unknown'
        self.resolve_button.setVisible(is_unknown)
        self.resolve_button.setEnabled(is_unknown and not self.busy)
        self.resolve_button.setToolTip(
            tr('处理该条提交待确认记录') if is_unknown and not self.busy else
            tr('队列、匹配扫描或下载进行中，完成后可处理待确认提交') if is_unknown else
            tr('当前任务不是提交待确认状态'))
        if hasattr(self, 'references'):
            self.references.set_editable(self.editable and not self.busy)
            self.references.set_paths(t.get('images', []))

    @staticmethod
    def _model_chip_style(model):
        if 'H3' in model:
            color = '#2563EB'
        elif 'v2' in model.lower() or 'seedance' in model.lower():
            color = '#7C3AED'
        else:
            color = '#059669'
        return (f'color:#FFFFFF;background:{color};'
                'border-radius:6px;padding:2px 7px;')

    def enterEvent(self, event):
        self._hover = True; self.update(); super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False; self.update(); super().leaveEvent(event)
    def set_editable(self,e,busy=False):
        self.editable=e; self.busy=busy
        self.images_button.setEnabled(not busy)
        self.images_button.setToolTip(
            tr('正在扫描匹配、运行队列或下载中，完成后可查看参考图') if busy else
            tr('查看每个提示词绑定的参考图'))
        self.more_button.setEnabled(not busy)
        self.more_button.setToolTip(
            tr('正在扫描匹配、运行队列或下载中，完成后可使用更多操作') if busy else
            tr('更多操作'))
        if self.task.get('status') == 'submission_unknown':
            self.resolve_button.setEnabled(not busy)
            self.resolve_button.setToolTip(
                tr('队列、匹配扫描或下载进行中，完成后可处理待确认提交') if busy else
                tr('处理该条提交待确认记录'))
    def paintEvent(self,e):
        s=self.task.get('status'); p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        if s in ACTIVE:
            tint=QLinearGradient(0,0,self.width(),0); tint.setColorAt(0,QColor(91,141,239,16)); tint.setColorAt(1,QColor(91,141,239,4)); p.fillRect(self.rect(),tint)
        elif s=='failed':
            tint=QLinearGradient(0,0,self.width(),0); tint.setColorAt(0,QColor(239,68,68,16)); tint.setColorAt(1,QColor(239,68,68,4)); p.fillRect(self.rect(),tint)
        elif self._hover:
            from ..materials import is_light
            p.fillRect(self.rect(), QColor(15,26,52,6) if is_light() else QColor(255,255,255,5))
        if s in ACTIVE or s=='failed':
            if s=='failed':
                p.setBrush(QColor('#EF4444'))
            else:
                from ..materials import palette
                p.setBrush(QColor(palette()['accent']))
            p.setPen(Qt.NoPen); p.drawRoundedRect(QRectF(0,1,3,self.height()-2),1.5,1.5)
        from ..materials import is_light as _is_light_row
        p.setPen(QPen(QColor(15,26,52,14) if _is_light_row() else QColor(255,255,255,10),1)); p.drawLine(0,self.height()-1,self.width(),self.height()-1)


class WorkspaceTaskTable(TaskQueuePanel):
    action_requested=pyqtSignal(int,str); reorder_requested=pyqtSignal(int,object); preview_requested=pyqtSignal(str); select_prompts_requested=pyqtSignal()
    def __init__(self,defaults,parent=None):
        QWidget.__init__(self,parent); self.defaults=defaults; self._tasks=[]; self.rows=[]; self._catalog={}; self._default_model=''; self.models_editable=True; self.busy=False; self._task_items={}; self._collapsed=set(); self._widths=[]; self._layout_signature=None
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0); self.surface=WorkspaceCard(); lay=QVBoxLayout(self.surface); lay.setContentsMargins(1,1,1,1); lay.setSpacing(0)
        tools=QHBoxLayout(); tools.setContentsMargins(16,10,16,10); tools.setSpacing(8)
        tools.addWidget(label(tr('任务队列'),16,'#f5f5f5',True), 0, Qt.AlignVCenter); self.count_label=label(f'0 {tr("个任务")}'); tools.addWidget(self.count_label, 0, Qt.AlignVCenter); self.product_progress_label=label(f"{tr('产品')} 0/0"); tools.addWidget(self.product_progress_label, 0, Qt.AlignVCenter); tools.addStretch(1)
        self.filter_box=ComboBox(); self.filter_box.addItems([tr('全部'),tr('等待中'),tr('生成中'),tr('上传中'),tr('提交中'),tr('下载中'),tr('重试中'),tr('等待冷却'),tr('已暂停'),tr('已完成'),tr('失败'),tr('重复'),tr('提交待确认'),tr('已跳过'),tr('已取消')]); self.filter_box.currentTextChanged.connect(self._filter); self.filter_box.setFixedHeight(32); tools.addWidget(self.filter_box, 0, Qt.AlignVCenter)
        # 低频与破坏性操作收进「更多操作」菜单，头部只留高频入口（筛选 + 匹配详情）。
        self.reset_models_button=style_button(PushButton(tr('重置模型识别'))); self.reset_models_button.setFixedHeight(32); self.reset_models_button.clicked.connect(self.reset_models_requested)
        self.params_button=style_button(PushButton(FIF.SETTING,tr('生成参数'))); self.params_button.setFixedHeight(32)
        self.match_button=style_button(PushButton(FIF.SEARCH,tr('匹配详情'))); self.match_button.setFixedHeight(32); tools.addWidget(self.match_button, 0, Qt.AlignVCenter)
        self.cancel_button=style_button(PushButton(tr('取消全部'))); self.cancel_button.setFixedHeight(32)
        self.more_button=style_button(PushButton(FIF.DOWN,tr('更多操作'))); self.more_button.setFixedHeight(32); self.more_button.clicked.connect(self._open_more_menu); tools.addWidget(self.more_button, 0, Qt.AlignVCenter)
        lay.addLayout(tools)
        self._header_host=QWidget(); self._header_host.setFixedHeight(34); self._header_grid=QGridLayout(self._header_host); self._header_grid.setContentsMargins(16,0,16,0); self._header_grid.setHorizontalSpacing(CELL_SPACING)
        self._apply_header_mode_style()
        from ..materials import register_theme_callback
        register_theme_callback(self._apply_header_mode_style)
        self._header_cells=[]
        for column,txt in enumerate(HEADERS):
            head=label(tr(txt),12,'#9AA6B8')
            self._header_cells.append(head)
            font=head.font(); font.setLetterSpacing(QFont.AbsoluteSpacing,0.6); font.setWeight(QFont.Medium); head.setFont(font)
            if column in (0,1,2,3):
                head.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            elif column == 9:
                head.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            else:
                head.setAlignment(Qt.AlignCenter)
            self._header_grid.addWidget(head,0,column)
        lay.addWidget(self._header_host)
        self.list=ListWidget(); self.list.setSpacing(0); self.list.setFrameShape(self.list.NoFrame); self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.list.setVerticalScrollMode(self.list.ScrollPerPixel)
        try:
            self.list.setViewportMargins(0, 0, 0, 0)
        except Exception:
            pass
        self.list.currentItemChanged.connect(self._selected_task_changed); self.list.setContextMenuPolicy(Qt.CustomContextMenu); self.list.customContextMenuRequested.connect(self._context_menu)
        try:
            self.list.viewport().installEventFilter(self)
        except Exception:
            pass
        lay.addWidget(self.list,1); root.addWidget(self.surface,1)
        # 空状态：卡片内居中引导（图标 + 主文案 + 提示 + 选择目录按钮）。
        self.empty_panel=QWidget(); ep=QVBoxLayout(self.empty_panel); ep.setContentsMargins(24,14,24,50); ep.setSpacing(10); ep.setAlignment(Qt.AlignCenter)
        icon_row=QHBoxLayout(); icon_row.addStretch(1)
        self.empty_icon=IconWidget(FIF.FOLDER, self.empty_panel); self.empty_icon.setFixedSize(48,48); icon_row.addWidget(self.empty_icon)
        icon_row.addStretch(1); ep.addLayout(icon_row)
        self.empty_label=label(tr('暂无任务'),15,'#C7CCD6',True); self.empty_label.setAlignment(Qt.AlignCenter); ep.addWidget(self.empty_label)
        self.empty_hint=label(tr('选择分镜提示词目录后会自动扫描并匹配参考图'),12); self.empty_hint.setAlignment(Qt.AlignCenter); ep.addWidget(self.empty_hint)
        button_row=QHBoxLayout(); button_row.addStretch(1)
        self.empty_button=style_button(PrimaryPushButton(FIF.FOLDER,tr('选择目录')), primary=True); self.empty_button.setFixedHeight(32); self.empty_button.clicked.connect(lambda *_: self.select_prompts_requested.emit()); button_row.addWidget(self.empty_button)
        button_row.addStretch(1); ep.addLayout(button_row)
        self.empty_panel.setVisible(False)
        lay.addWidget(self.empty_panel,1)
        self._apply_column_widths()

    def _apply_header_mode_style(self):
        try:
            from ..materials import is_light
            if is_light():
                self._header_host.setStyleSheet('background:rgba(15,26,52,0.05);border-bottom:1px solid rgba(15,26,52,0.12);')
            else:
                self._header_host.setStyleSheet('background:rgba(0,0,0,0.18);border-bottom:1px solid rgba(255,255,255,0.08);')
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        if obj is self.list.viewport() and event.type() == event.Resize:
            self._apply_column_widths()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_column_widths()

    def _viewport_left_inset(self):
        try:
            return self.list.geometry().x() + self.list.viewport().x()
        except Exception:
            return 0

    def _row_geometry(self):
        """以真实行控件为锚：返回（内容起点相对 surface 的 x，行内容宽度）。

        列表样式会给 item 控件额外的内边距（实测 8px 起步），仅按 viewport 估算
        会让表头与数据列漂移；直接读取行宿主控件的几何得到逐像素准确的锚点。
        """
        try:
            if self.rows:
                host = self.rows[0].host
                origin = host.mapTo(self.surface, host.rect().topLeft()).x()
                return origin, max(0, host.width() - 32)
            if self.list.count():
                rect = self.list.visualItemRect(self.list.item(0))
                origin = self.list.viewport().mapTo(self.surface, rect.topLeft()).x()
                return origin, max(0, rect.width() - 32)
        except (RuntimeError, AttributeError):
            pass
        try:
            viewport = self.list.viewport().width()
        except Exception:
            viewport = 0
        if viewport <= 0:
            viewport = max(0, self.surface.width() - 32)
        return self._viewport_left_inset(), max(0, viewport - 32)

    def _apply_column_widths(self):
        """表头与所有数据行使用同一解析结果，保证逐像素对齐。"""
        origin, content = self._row_geometry()
        widths = resolve_widths(content)
        signature = (origin, content)
        if signature == self._layout_signature and widths == self._widths:
            return
        self._widths = widths
        self._layout_signature = signature
        try:
            host = self._header_host
            left = max(0, origin + 16 - host.x())
            right = max(0, host.width() - left - content)
            self._header_grid.setContentsMargins(left, 0, right, 0)
        except Exception:
            pass
        apply_grid_columns(self._header_grid, widths)
        # 表头逐格锁宽：与数据行使用同一刚性列宽，杜绝布局再分配造成的漂移。
        for column, head in enumerate(getattr(self, '_header_cells', [])):
            try:
                if head.minimumWidth() != widths[column] or head.maximumWidth() != widths[column]:
                    head.setMinimumWidth(widths[column]); head.setMaximumWidth(widths[column])
            except RuntimeError:
                pass
        for row in self.rows:
            try:
                row.apply_widths(widths)
            except RuntimeError:
                pass

    def update_tasks(self,tasks):
        incoming=list(tasks)
        old_keys=[(t.get('local_id'), t.get('prompt_path'), t.get('prompt_name')) for t in self._tasks]
        new_keys=[(t.get('local_id'), t.get('prompt_path'), t.get('prompt_name')) for t in incoming]
        # Reuse row widgets when task identity/order is unchanged so progress,
        # thumbnails and transient animations survive state refreshes.
        if self.rows and old_keys == new_keys and len(self.rows) == len(incoming):
            self._tasks=incoming
            status_changed = False
            for i, (task, row) in enumerate(zip(incoming, self.rows)):
                row.index=i; row.update_task(task)
                item=self._task_items.get(i)
                if item is not None:
                    status_text = tr(STATUS_TEXT.get(task.get('status','waiting'), task.get('status','waiting')))
                    status_changed = status_changed or item.data(Qt.UserRole) != status_text
                    item.setData(Qt.UserRole+1, i)
                    item.setData(Qt.UserRole, status_text)
                    item.setToolTip((task.get('prompt_name') or '') + '\n' + task.get('error', ''))
            self.count_label.setText(f'{len(incoming)} {tr("个任务")}')
            self.list.setVisible(bool(incoming))
            self.empty_panel.setVisible(not incoming)
            self._update_product_summary(incoming)
            if status_changed:
                self._filter(self.filter_box.currentText())
            self._apply_column_widths()
            return
        # 显式销毁旧行控件：list.clear() 只删条目，setItemWidget 挂载的部件不会随之释放。
        for row in self.rows:
            try:
                row.setParent(None); row.deleteLater()
            except RuntimeError:
                pass
        self._tasks=incoming; self.list.clear(); self.rows=[]; self._task_items={}
        for i,t in enumerate(self._tasks):
            it=QListWidgetItem(self.list); it.setData(Qt.UserRole+1,i); it.setData(Qt.UserRole, tr(STATUS_TEXT.get(t.get('status','waiting'), t.get('status','waiting')))); row=ExpandedTaskRow(i,t,self.defaults); row.action_requested.connect(self.action_requested); row.reorder_requested.connect(self.reorder_requested); row.preview_requested.connect(self.preview_requested); it.setSizeHint(QSize(0,56)); self.list.setItemWidget(it,row); self.rows.append(row); self._task_items[i]=it
        self.list.setVisible(bool(tasks)); self.empty_panel.setVisible(not tasks); self.count_label.setText(f'{len(tasks)} {tr("个任务")}'); self._update_product_summary(self._tasks); self._filter(self.filter_box.currentText()); self._apply_column_widths()

    def _update_product_summary(self, tasks):
        products = {task.get('product') or '未分组' for task in tasks}
        completed = sum(
            all(task.get('status') in TERMINAL for task in tasks if (task.get('product') or '未分组') == product)
            for product in products
        )
        self.product_progress_label.setText(f"{tr('产品')} {completed}/{len(products)}")

    def set_busy(self, busy):
        self.busy = bool(busy)
        self.more_button.setEnabled(not self.busy)
        self.more_button.setToolTip(
            tr('队列、匹配扫描或下载进行中，完成后可使用更多操作') if self.busy else
            tr('更多操作'))
        self.params_button.setEnabled(not self.busy)
        self.params_button.setToolTip(
            tr('队列、匹配扫描或下载进行中，完成后可编辑参数') if self.busy else
            tr('生成参数'))
        for row in self.rows:
            row.set_editable(self.models_editable, self.busy)
    def build_more_menu(self):
        """构建「更多操作」菜单（独立出来便于复用与测试）。"""
        menu = RoundMenu(parent=self)
        actions = ((tr('重置模型识别'), self.reset_models_requested.emit),
                   (tr('生成参数'), self.params_button.click),
                   (tr('取消全部'), self.cancel_button.click))
        for text, handler in actions:
            action = Action(text, menu)
            if text == tr('重置模型识别') and not self.models_editable:
                action.setEnabled(False)
                action.setToolTip(tr('队列运行中，完成后可重置模型'))
            action.triggered.connect(lambda checked=False, _handler=handler: _handler())
            menu.addAction(action)
        return menu

    def _open_more_menu(self):
        if self.busy:
            InfoBar.info(tr('当前无法操作'), tr('队列、匹配扫描或下载进行中，完成后可使用更多操作'), parent=self, duration=4000)
            return
        menu = self.build_more_menu()
        menu.exec(self.more_button.mapToGlobal(QPoint(0, self.more_button.height() + 4)))

    def _filter(self,s):
        for i,t in enumerate(self._tasks):
            hidden=(s!=tr('全部') and tr(STATUS_TEXT.get(t.get('status','waiting'),'等待中'))!=s and not(s==tr('生成中') and t.get('status') in ACTIVE))
            hidden=hidden or (t.get('product') or '未分组') in self._collapsed
            self._task_items[i].setHidden(hidden)
    def toggle_group(self,product):
        """折叠/展开某个产品的全部任务行（表格无分组头，仅隐藏/恢复行）。"""
        if product in self._collapsed: self._collapsed.discard(product)
        else: self._collapsed.add(product)
        self._filter(self.filter_box.currentText())
    def task_item(self,i): return self._task_items.get(i)
    def select_task(self,i):
        if self.task_item(i): self.list.setCurrentItem(self.task_item(i))
    def set_model_catalog(self,c,d): self._catalog=c; self._default_model=d
    def set_models_editable(self,e):
        self.models_editable=bool(e); self.reset_models_button.setEnabled(bool(e))
        self.reset_models_button.setToolTip(
            tr('队列或匹配扫描进行中，完成后可重置模型') if not e else
            tr('全部重置为自动识别'))
        for row in self.rows:
            row.set_editable(self.models_editable, self.busy)
    def _selected_task_changed(self,c,p):
        if c is not None and isinstance(c.data(Qt.UserRole+1),int): self.task_selected.emit(c.data(Qt.UserRole+1))
    def _context_menu(self,pos):
        if self.busy:
            InfoBar.info(tr('当前无法操作'), tr('队列、匹配扫描或下载进行中，完成后可查看任务操作'), parent=self, duration=3500)
            return
        it=self.list.itemAt(pos); i=it.data(Qt.UserRole+1) if it else None
        if isinstance(i,int): self.action_requested.emit(i,'menu')
        else: InfoBar.info(tr('没有可操作的任务'), tr('请先选择一条任务后再打开更多操作'), parent=self, duration=3000)
    def action_menu(self,i):
        m=RoundMenu(parent=self)
        for text,a in [('重试','retry'),('跳过','skip'),('查看日志','logs'),('打开文件夹','folder'),('任务详情 / 确认提交','details')]:
            x=Action(tr(text),m); x.triggered.connect(lambda checked=False,v=a:self.action_requested.emit(i,v)); m.addAction(x)
        return m
    def model_menu_for(self,i):
        m=RoundMenu(tr('强制使用模型'),self)
        for model in self._catalog:
            x=Action(model,m); x.setData(model); x.triggered.connect(lambda checked=False,v=model:self.model_override_requested.emit(i,v)); m.addAction(x)
        x=Action(tr('恢复自动识别'),m); x.triggered.connect(lambda:self.model_override_requested.emit(i,'')); m.addAction(x); return m
