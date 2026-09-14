"""Compact Fluent workspace task table."""
from pathlib import Path
from PyQt5.QtCore import Qt, QSize, pyqtSignal
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QListWidgetItem, QSizePolicy
from qfluentwidgets import ListWidget, ComboBox, PushButton, TransparentToolButton, RoundMenu, Action, ProgressBar, FluentIcon as FIF
from core.task_manager import STATUS_TEXT, ACTIVE, TERMINAL
from core.prompt_detector import short_model_name
from .task_queue_panel import TaskQueuePanel
from .workspace_surface import WorkspaceCard, BreathingDot, label, style_button, BLUE, GREEN, RED, YELLOW, MUTED
from ..motion import Shimmer

def state_color(s): return GREEN if s=='completed' else RED if s=='failed' else BLUE if s in ACTIVE else YELLOW if s in {'retry_wait','cooldown_wait','submission_unknown'} else MUTED
def grid_for(host):
    g=QGridLayout(host); g.setContentsMargins(16,0,16,0); g.setHorizontalSpacing(12)
    for c,m,st in [(0,14,0),(1,32,0),(2,150,3),(3,70,1),(4,82,1),(5,56,0),(6,68,0),(7,50,0),(8,94,1),(9,128,1),(10,100,1),(11,76,0)]: g.setColumnMinimumWidth(c,m); g.setColumnStretch(c,st)
    return g

class TaskProgress(ProgressBar):
    def __init__(self,p=None):
        super().__init__(p); self.setFixedHeight(4); self.setProperty('studioStyled',True); self.setCustomBarColor(BLUE,BLUE)
        self._studio_shimmer = Shimmer(self)

class ExpandedTaskRow(QWidget):
    action_requested=pyqtSignal(int,str); reorder_requested=pyqtSignal(int,object); preview_requested=pyqtSignal(str)
    def __init__(self,index,task,defaults,parent=None):
        super().__init__(parent); self.index=index; self.defaults=defaults; self.task={}; self.editable=True; self.busy=False; self.setFixedHeight(56)
        root=QHBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0); host=QWidget(); host.setFixedHeight(56); g=grid_for(host)
        self.dot=BreathingDot(MUTED); self.number=label(f'{index+1:02d}',12,MUTED,mono=True); self.title=label('',13,'#f5f5f5',True); self.title.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred); self.product=label('',12,MUTED); self.product.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred); self.model_label=label('',11,BLUE,True)
        self.ratio=label('—',12,MUTED); self.resolution=label('—',12,MUTED); self.duration=label('—',12,MUTED); self.images_button=style_button(TransparentToolButton(FIF.PHOTO)); self.images_button.setFixedSize(56,30); self.images_button.clicked.connect(lambda:self._preview())
        ph=QWidget(); pl=QHBoxLayout(ph); pl.setContentsMargins(0,0,0,0); pl.setSpacing(6); self.progress=TaskProgress(); self.percentage=label('0%',11,MUTED,mono=True); self.percentage.setFixedWidth(35); pl.addWidget(self.progress,1); pl.addWidget(self.percentage); self.timing=label('—',12,MUTED,mono=True); self.status_label=label('',11); self.more_button=style_button(TransparentToolButton(FIF.MORE)); self.more_button.setFixedSize(30,30); self.more_button.clicked.connect(lambda:self.action_requested.emit(self.index,'menu'))
        for c,w in enumerate((self.dot,self.number,self.title,self.product,self.model_label,self.ratio,self.resolution,self.duration,self.images_button,ph,self.timing,self.status_label)): g.addWidget(w,0,c)
        g.addWidget(self.more_button,0,11); root.addWidget(host); self.update_task(task)
    def _preview(self):
        if self.task.get('images'): self.preview_requested.emit(self.task['images'][0])
    def update_task(self,t):
        self.task=dict(t); s=t.get('status','waiting'); col=state_color(s); self.dot.active=s in ACTIVE; self.dot.setTextColor(col,col); name=Path(t.get('prompt_path','')).name or (t.get('prompt_name','')+'.txt'); self.title.setText(name); self.product.setText(t.get('product') or '未分组'); model=t.get('model') or t.get('requested_model') or self.defaults.get('model',''); src=' · 手动' if t.get('model_source')=='manual' else ' · 自动' if t.get('model_source')=='auto' else ''; short='V2' if model=='video-v2' else 'V3' if model=='video-v3' else short_model_name(model); self.model_label.setText(f'[{short}]'+src); self.model_label.setToolTip(model+src); chip=BLUE if 'H3' in model else '#a78bfa' if ('v2' in model.lower() or 'seedance' in model.lower()) else GREEN; self.model_label.setStyleSheet(f'color:{chip};background:rgba(255,255,255,0.08);border-radius:8px;padding:0 7px;'); self.status_label.setText(STATUS_TEXT.get(s,s)); self.status_label.setStyleSheet(f'color:{col};background:rgba(255,255,255,0.08);border-radius:6px;padding:0 6px;'); val=100 if s=='completed' else max(0,min(100,int(t.get('progress',0)))); self.progress.setValue(val); self.progress.setError(s=='failed'); self.percentage.setText(f'{val}%'); p={**self.defaults,**(t.get('effective_parameters') or t.get('_display_parameters',{}))}; self.ratio.setText(str(p.get('aspect_ratio') or p.get('ratio') or '—')); self.resolution.setText(str(p.get('resolution') or '—')); d=p.get('duration'); self.duration.setText(f'{d}s' if d not in (None,'') else '—'); self.timing.setText(f'第{self.index+1}位' if s=='waiting' else f'重试{t.get("retry_count",0)}/{t.get("max_retries",self.defaults.get("max_retries",0))}' if s=='failed' else '已完成' if s=='completed' else '—'); self.images_button.setText(f'IMG {len(t.get("images",[]))}'); self.update()
    def set_editable(self,e,busy=False): self.editable=e; self.busy=busy; self.images_button.setEnabled(bool(self.task.get('images')))
    def paintEvent(self,e):
        from PyQt5.QtGui import QPainter,QColor,QPen
        s=self.task.get('status'); p=QPainter(self); c=QColor(BLUE if s in ACTIVE else RED if s=='failed' else '#ffffff'); c.setAlpha(14 if s in ACTIVE or s=='failed' else 0); p.fillRect(self.rect(),c); p.fillRect(0,0,3,self.height(),QColor(BLUE if s in ACTIVE else RED)) if s in ACTIVE or s=='failed' else None; p.setPen(QPen(QColor(255,255,255,10),1)); p.drawLine(0,self.height()-1,self.width(),self.height()-1)

class WorkspaceTaskTable(TaskQueuePanel):
    action_requested=pyqtSignal(int,str); reorder_requested=pyqtSignal(int,object); preview_requested=pyqtSignal(str)
    def __init__(self,defaults,parent=None):
        QWidget.__init__(self,parent); self.defaults=defaults; self._tasks=[]; self.rows=[]; self._catalog={}; self._default_model=''; self.models_editable=True; self.busy=False; self._task_items={}
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0); self.surface=WorkspaceCard(); lay=QVBoxLayout(self.surface); lay.setContentsMargins(1,1,1,1); lay.setSpacing(0); tools=QHBoxLayout(); tools.setContentsMargins(18,8,18,8); tools.addWidget(label('任务队列',16,'#f5f5f5',True)); self.count_label=label('0 个任务'); tools.addWidget(self.count_label); self.product_progress_label=label('产品 0/0'); tools.addWidget(self.product_progress_label); tools.addStretch(1); self.filter_box=ComboBox(); self.filter_box.addItems(['全部','等待中','生成中','已完成','失败','已跳过','已取消']); self.filter_box.currentTextChanged.connect(self._filter); tools.addWidget(self.filter_box); self.reset_models_button=style_button(PushButton('重置模型识别')); self.reset_models_button.clicked.connect(self.reset_models_requested); tools.addWidget(self.reset_models_button); self.params_button=style_button(PushButton(FIF.SETTING,'生成参数')); tools.addWidget(self.params_button); self.match_button=style_button(PushButton(FIF.PHOTO,'匹配详情')); tools.addWidget(self.match_button); self.cancel_button=style_button(PushButton('取消全部')); tools.addWidget(self.cancel_button); lay.addLayout(tools); h=QWidget(); h.setFixedHeight(34); g=grid_for(h); h.setStyleSheet('background:rgba(0,0,0,0.18);border-bottom:1px solid rgba(255,255,255,0.08);');
        for c,txt in enumerate(('●','#','提示词','产品','模型','比例','分辨率','时长','参考图','进度','用时/剩余','状态')): g.addWidget(label(txt,11,'#a1a1aa'),0,c)
        lay.addWidget(h); self.list=ListWidget(); self.list.setSpacing(0); self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.list.currentItemChanged.connect(self._selected_task_changed); self.list.setContextMenuPolicy(Qt.CustomContextMenu); self.list.customContextMenuRequested.connect(self._context_menu); lay.addWidget(self.list,1); root.addWidget(self.surface,1); self.empty_label=label('暂无任务 · 选择分镜提示词目录后自动扫描匹配',14); self.empty_label.setAlignment(Qt.AlignCenter); root.addWidget(self.empty_label)
    def update_tasks(self,tasks):
        self._tasks=list(tasks); self.list.clear(); self.rows=[]; self._task_items={}
        for i,t in enumerate(self._tasks):
            it=QListWidgetItem(self.list); it.setData(Qt.UserRole+1,i); row=ExpandedTaskRow(i,t,self.defaults); row.action_requested.connect(self.action_requested); row.reorder_requested.connect(self.reorder_requested); row.preview_requested.connect(self.preview_requested); it.setSizeHint(QSize(0,56)); self.list.setItemWidget(it,row); self.rows.append(row); self._task_items[i]=it
        self.list.setVisible(bool(tasks)); self.empty_label.setVisible(not tasks); self.count_label.setText(f'{len(tasks)} 个任务'); products={t.get('product') or '未分组' for t in tasks}; done=sum(all(t.get('status') in TERMINAL for t in tasks if (t.get('product') or '未分组')==p) for p in products); self.product_progress_label.setText(f'产品 {done}/{len(products)}'); self._filter(self.filter_box.currentText())
    def _filter(self,s):
        for i,t in enumerate(self._tasks): self._task_items[i].setHidden(s!='全部' and STATUS_TEXT.get(t.get('status','waiting'),'等待中')!=s and not(s=='生成中' and t.get('status') in ACTIVE))
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
