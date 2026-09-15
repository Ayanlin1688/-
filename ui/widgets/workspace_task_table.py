"""Compact Fluent workspace task table."""
from pathlib import Path
from PyQt5.QtCore import Qt, QRectF, QSize, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QListWidgetItem, QSizePolicy
from qfluentwidgets import (ListWidget, ComboBox, ProgressBar, PushButton, TransparentToolButton, RoundMenu, Action, FluentIcon as FIF)
from qfluentwidgets.common.config import isDarkTheme
from core.task_manager import STATUS_TEXT, ACTIVE, TERMINAL
from core.prompt_detector import short_model_name
from .task_queue_panel import TaskQueuePanel
from .workspace_surface import WorkspaceCard, BreathingDot, ReferenceStrip, label, style_button, BLUE, GREEN, RED, YELLOW, MUTED
from ..motion import Shimmer

def state_color(s): return GREEN if s=='completed' else RED if s=='failed' else BLUE if s in ACTIVE else YELLOW if s in {'retry_wait','cooldown_wait','submission_unknown'} else MUTED
def grid_for(host):
    g=QGridLayout(host); g.setContentsMargins(16,0,16,0); g.setHorizontalSpacing(12)
    for c,m,st in [(0,14,0),(1,32,0),(2,150,3),(3,70,1),(4,82,1),(5,56,0),(6,68,0),(7,50,0),(8,94,1),(9,128,1),(10,100,1),(11,76,0)]: g.setColumnMinimumWidth(c,m); g.setColumnStretch(c,st)
    return g

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
        gradient = QLinearGradient(0, 0, max(width, 1), 0)
        if self.isError():
            gradient.setColorAt(0, QColor('#EF4444')); gradient.setColorAt(1, QColor('#DC2626'))
        else:
            gradient.setColorAt(0, QColor('#5B8DEF')); gradient.setColorAt(1, QColor('#7C6CF0'))
        painter.setBrush(gradient)
        radius = self.height() / 2
        painter.drawRoundedRect(QRectF(0, 0, width, self.height()), radius, radius)

class ExpandedTaskRow(QWidget):
    action_requested=pyqtSignal(int,str); reorder_requested=pyqtSignal(int,object); preview_requested=pyqtSignal(str)
    def __init__(self,index,task,defaults,parent=None):
        super().__init__(parent); self.index=index; self.defaults=defaults; self.task={}; self.editable=True; self.busy=False; self._hover=False; self.setFixedHeight(52)
        root=QHBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0); host=QWidget(); host.setFixedHeight(52); g=grid_for(host)
        self.dot=BreathingDot(MUTED); self.number=label(f'{index+1:02d}',12,MUTED,mono=True); self.title=label('',13,'#f5f5f5',True); self.title.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred); self.product=label('',12,MUTED); self.product.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred); self.model_label=label('',11,BLUE,True)
        self.ratio=label('—',12,MUTED); self.resolution=label('—',12,MUTED); self.duration=label('—',12,MUTED); self.images_button=style_button(TransparentToolButton(FIF.PHOTO)); self.images_button.setFixedSize(56,30); self.images_button.clicked.connect(lambda:self._preview())
        ph=QWidget(); pl=QHBoxLayout(ph); pl.setContentsMargins(0,0,0,0); pl.setSpacing(6); self.progress=TaskProgress(); self.percentage=label('0%',11,MUTED,mono=True); self.percentage.setFixedWidth(35); pl.addWidget(self.progress,1); pl.addWidget(self.percentage); self.timing=label('—',12,MUTED,mono=True); self.status_label=label('',11); self.more_button=style_button(TransparentToolButton(FIF.MORE)); self.more_button.setFixedSize(30,30); self.more_button.clicked.connect(lambda:self.action_requested.emit(self.index,'menu'))
        for c,w in enumerate((self.dot,self.number,self.title,self.product,self.model_label,self.ratio,self.resolution,self.duration,self.images_button,ph,self.timing,self.status_label)): g.addWidget(w,0,c)
        g.addWidget(self.more_button,0,11); root.addWidget(host); self.update_task(task)
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
    def _preview(self):
        if self.task.get('images'): self.preview_requested.emit(self.task['images'][0])
    def update_task(self,t):
        self.task=dict(t); s=t.get('status','waiting'); col=state_color(s); self.dot.active=s in ACTIVE; self.dot.setTextColor(col,col); name=Path(t.get('prompt_path','')).name or (t.get('prompt_name','')+'.txt'); self.title.setText(name); self.product.setText(t.get('product') or '未分组'); model=t.get('model') or t.get('requested_model') or self.defaults.get('model',''); src=' · 手动' if t.get('model_source')=='manual' else ' · 自动' if t.get('model_source')=='auto' else ''; short='V2' if model=='video-v2' else 'V3' if model=='video-v3' else short_model_name(model); self.model_label.setText(f'[{short}]'+src); self.model_label.setToolTip(model+src); self.model_label.setStyleSheet(self._model_chip_style(model)); self.status_label.setText(STATUS_TEXT.get(s,s)); chip=QColor(col); self.status_label.setStyleSheet(f'color:{col};background:rgba({chip.red()},{chip.green()},{chip.blue()},0.15);border-radius:6px;padding:0 6px;'); val=100 if s=='completed' else max(0,min(100,int(t.get('progress',0)))); self.progress.setValue(val); self.progress.setError(s=='failed'); self.percentage.setText(f'{val}%'); p={**self.defaults,**(t.get('effective_parameters') or t.get('_display_parameters',{}))}; self.ratio.setText(str(p.get('aspect_ratio') or p.get('ratio') or '—')); self.resolution.setText(str(p.get('resolution') or '—')); d=p.get('duration'); self.duration.setText(f'{d}s' if d not in (None,'') else '—'); self.timing.setText('—'); self.images_button.setText(f'IMG ×{len(t.get("images",[]))}'); self.update()
        if hasattr(self, 'references'):
            self.references.set_editable(self.editable and not self.busy)
            self.references.set_paths(t.get('images', []))

    @staticmethod
    def _model_chip_style(model):
        if 'H3' in model:
            start, end = '#3B82F6', '#2563EB'
        elif 'v2' in model.lower() or 'seedance' in model.lower():
            start, end = '#8B5CF6', '#7C3AED'
        else:
            start, end = '#10B981', '#059669'
        return (f'color:#FFFFFF;background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {start},stop:1 {end});'
                'border-radius:6px;padding:0 7px;')

    def enterEvent(self, event):
        self._hover = True; self.update(); super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False; self.update(); super().leaveEvent(event)
    def set_editable(self,e,busy=False): self.editable=e; self.busy=busy; self.images_button.setEnabled(bool(self.task.get('images')))
    def paintEvent(self,e):
        s=self.task.get('status'); p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        if s in ACTIVE:
            tint=QLinearGradient(0,0,self.width(),0); tint.setColorAt(0,QColor(91,141,239,16)); tint.setColorAt(1,QColor(91,141,239,4)); p.fillRect(self.rect(),tint)
        elif s=='failed':
            tint=QLinearGradient(0,0,self.width(),0); tint.setColorAt(0,QColor(239,68,68,16)); tint.setColorAt(1,QColor(239,68,68,4)); p.fillRect(self.rect(),tint)
        elif self._hover:
            p.fillRect(self.rect(),QColor(255,255,255,5))
        if s in ACTIVE or s=='failed':
            bar=QLinearGradient(0,0,0,self.height())
            if s=='failed':
                bar.setColorAt(0,QColor('#EF4444')); bar.setColorAt(1,QColor('#DC2626'))
            else:
                bar.setColorAt(0,QColor('#5B8DEF')); bar.setColorAt(1,QColor('#7C6CF0'))
            p.setPen(Qt.NoPen); p.setBrush(bar); p.drawRoundedRect(QRectF(0,1,3,self.height()-2),1.5,1.5)
        p.setPen(QPen(QColor(255,255,255,10),1)); p.drawLine(0,self.height()-1,self.width(),self.height()-1)

class WorkspaceTaskTable(TaskQueuePanel):
    action_requested=pyqtSignal(int,str); reorder_requested=pyqtSignal(int,object); preview_requested=pyqtSignal(str)
    def __init__(self,defaults,parent=None):
        QWidget.__init__(self,parent); self.defaults=defaults; self._tasks=[]; self.rows=[]; self._catalog={}; self._default_model=''; self.models_editable=True; self.busy=False; self._task_items={}; self._collapsed=set()
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0); self.surface=WorkspaceCard(); lay=QVBoxLayout(self.surface); lay.setContentsMargins(1,1,1,1); lay.setSpacing(0); tools=QHBoxLayout(); tools.setContentsMargins(18,8,18,8); tools.addWidget(label('任务队列',16,'#f5f5f5',True)); self.count_label=label('0 个任务'); tools.addWidget(self.count_label); self.product_progress_label=label('产品 0/0'); tools.addWidget(self.product_progress_label); tools.addStretch(1); self.filter_box=ComboBox(); self.filter_box.addItems(['全部','等待中','生成中','上传中','提交中','重试中','等待冷却','已完成','失败','已跳过','已取消']); self.filter_box.currentTextChanged.connect(self._filter); tools.addWidget(self.filter_box); self.reset_models_button=style_button(PushButton('重置模型识别')); self.reset_models_button.clicked.connect(self.reset_models_requested); tools.addWidget(self.reset_models_button); self.params_button=style_button(PushButton(FIF.SETTING,'生成参数')); tools.addWidget(self.params_button); self.match_button=style_button(PushButton(FIF.PHOTO,'匹配详情')); tools.addWidget(self.match_button); self.cancel_button=style_button(PushButton('取消全部')); tools.addWidget(self.cancel_button); lay.addLayout(tools); h=QWidget(); h.setFixedHeight(34); g=grid_for(h); h.setStyleSheet('background:rgba(0,0,0,0.18);border-bottom:1px solid rgba(255,255,255,0.08);');
        for c,txt in enumerate(('●','#','提示词','产品','模型','比例','分辨率','时长','参考图','进度','用时/剩余','状态')):
            head=label(txt,11,'#9CA3AF'); font=head.font(); font.setLetterSpacing(QFont.AbsoluteSpacing,0.5); head.setFont(font); g.addWidget(head,0,c)
        lay.addWidget(h); self.list=ListWidget(); self.list.setSpacing(0); self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.list.currentItemChanged.connect(self._selected_task_changed); self.list.setContextMenuPolicy(Qt.CustomContextMenu); self.list.customContextMenuRequested.connect(self._context_menu); lay.addWidget(self.list,1); root.addWidget(self.surface,1); self.empty_label=label('暂无任务 · 选择分镜提示词目录后自动扫描匹配',14); self.empty_label.setAlignment(Qt.AlignCenter); root.addWidget(self.empty_label)
    def update_tasks(self,tasks):
        incoming=list(tasks)
        old_keys=[(t.get('local_id'), t.get('prompt_path'), t.get('prompt_name')) for t in self._tasks]
        new_keys=[(t.get('local_id'), t.get('prompt_path'), t.get('prompt_name')) for t in incoming]
        # Reuse row widgets when task identity/order is unchanged so progress,
        # thumbnails and transient animations survive state refreshes.
        if self.rows and old_keys == new_keys and len(self.rows) == len(incoming):
            self._tasks=incoming
            for i, (task, row) in enumerate(zip(incoming, self.rows)):
                row.index=i; row.update_task(task)
                item=self._task_items.get(i)
                if item is not None:
                    item.setData(Qt.UserRole+1, i)
                    item.setData(Qt.UserRole, STATUS_TEXT.get(task.get('status','waiting'), task.get('status','waiting')))
            self.count_label.setText(f'{len(incoming)} 个任务')
            self.list.setVisible(bool(incoming)); self.empty_label.setVisible(not incoming)
            self._filter(self.filter_box.currentText())
            return
        self._tasks=incoming; self.list.clear(); self.rows=[]; self._task_items={}
        for i,t in enumerate(self._tasks):
            it=QListWidgetItem(self.list); it.setData(Qt.UserRole+1,i); it.setData(Qt.UserRole, STATUS_TEXT.get(t.get('status','waiting'), t.get('status','waiting'))); row=ExpandedTaskRow(i,t,self.defaults); row.action_requested.connect(self.action_requested); row.reorder_requested.connect(self.reorder_requested); row.preview_requested.connect(self.preview_requested); it.setSizeHint(QSize(0,52)); self.list.setItemWidget(it,row); self.rows.append(row); self._task_items[i]=it
        self.list.setVisible(bool(tasks)); self.empty_label.setVisible(not tasks); self.count_label.setText(f'{len(tasks)} 个任务'); products={t.get('product') or '未分组' for t in tasks}; done=sum(all(t.get('status') in TERMINAL for t in tasks if (t.get('product') or '未分组')==p) for p in products); self.product_progress_label.setText(f'产品 {done}/{len(products)}'); self._filter(self.filter_box.currentText())
    def _filter(self,s):
        for i,t in enumerate(self._tasks):
            hidden=(s!='全部' and STATUS_TEXT.get(t.get('status','waiting'),'等待中')!=s and not(s=='生成中' and t.get('status') in ACTIVE))
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
    def set_models_editable(self,e): self.models_editable=bool(e); self.reset_models_button.setEnabled(bool(e))
    def _selected_task_changed(self,c,p):
        if c is not None and isinstance(c.data(Qt.UserRole+1),int): self.task_selected.emit(c.data(Qt.UserRole+1))
    def _context_menu(self,pos):
        it=self.list.itemAt(pos); i=it.data(Qt.UserRole+1) if it else None
        if isinstance(i,int): self.action_requested.emit(i,'menu')
    def action_menu(self,i):
        m=RoundMenu(parent=self)
        for text,a in [('重试','retry'),('跳过','skip'),('查看日志','logs'),('打开文件夹','folder'),('任务详情 / 确认提交','details')]:
            x=Action(text,m); x.triggered.connect(lambda checked=False,v=a:self.action_requested.emit(i,v)); m.addAction(x)
        return m
    def model_menu_for(self,i):
        m=RoundMenu('强制使用模型',self)
        for model in self._catalog:
            x=Action(model,m); x.setData(model); x.triggered.connect(lambda checked=False,v=model:self.model_override_requested.emit(i,v)); m.addAction(x)
        x=Action('恢复自动识别',m); x.triggered.connect(lambda:self.model_override_requested.emit(i,'')); m.addAction(x); return m
