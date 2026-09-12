"""Generation parameter card with collapsible advanced section."""

from PyQt5.QtCore import pyqtSignal, QPropertyAnimation, QEasingCurve, QSignalBlocker
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget, QSizePolicy
from qfluentwidgets import FluentIcon as FIF, PushButton

from ..components.custom_widgets import CaptionLabel, ComboBox, LineEdit, SpinBox, StrongBodyLabel, SwitchButton, make_card
from ..components.model_options import apply_model_options
from ..components.model_selector import ModelComboBox, catalog_snapshot
from core.model_parameters import MODELS, H3, h3_size, model_options


class ParamsCard(QWidget):
    values_changed = pyqtSignal(str, object)
    parameters_adjusted = pyqtSignal(str, str)

    def __init__(self, config_manager, parent=None) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.advanced_expanded = True
        card = make_card()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        root = QVBoxLayout(card)
        root.setContentsMargins(20, 20, 20, 20)
        title_row = QHBoxLayout()
        title_row.addWidget(StrongBodyLabel("生成参数"))
        title_row.addStretch(1)
        self.expand_button = PushButton(FIF.UP, "高级参数")
        self.expand_button.clicked.connect(self.toggle_advanced)
        title_row.addWidget(self.expand_button)
        root.addLayout(title_row)

        workspace = config_manager.config["workspace"]
        grid = QGridLayout()
        self.model = ModelComboBox(); self.model.set_models(catalog_snapshot(config_manager), workspace['model'])
        self.ratio = ComboBox(); self.ratio.addItems(["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "2:3", "3:2"]); self.ratio.setCurrentText(workspace["aspect_ratio"])
        self.resolution = ComboBox(); self.resolution.addItems(["480p", "720p", "768p", "1080p", "2K", "4K"]); self.resolution.setCurrentText(workspace["resolution"])
        self.duration = SpinBox(); self.duration.setRange(1, 30); self.duration.setValue(workspace["duration"])
        fields = [("模型", self.model), ("比例", self.ratio), ("分辨率", self.resolution), ("时长（秒）", self.duration)]
        for index, (text, control) in enumerate(fields):
            control.setMinimumWidth(0)
            control.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            grid.addWidget(CaptionLabel(text), (index // 2) * 2, index % 2)
            grid.addWidget(control, (index // 2) * 2 + 1, index % 2)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        root.addLayout(grid)
        self.size_hint = CaptionLabel('')
        self.size_hint.setWordWrap(True)
        root.addWidget(self.size_hint)
        self.pool_hint = CaptionLabel('')
        self.pool_hint.setWordWrap(True)
        root.addWidget(self.pool_hint)
        self.refresh_pool_hint()

        self.advanced = QWidget()
        advanced_form = QGridLayout(self.advanced)
        advanced_form.setContentsMargins(0, 10, 0, 0)
        advanced_form.setHorizontalSpacing(10)
        advanced_form.setVerticalSpacing(6)
        self.audio = SwitchButton(); self.audio.setChecked(workspace["generate_audio"])
        self.poll = SpinBox(); self.poll.setRange(3, 60); self.poll.setValue(workspace["poll_interval"])
        self.retries = SpinBox(); self.retries.setRange(0, 20); self.retries.setValue(workspace["max_retries"])
        self.threshold = SpinBox(); self.threshold.setRange(1, 100); self.threshold.setValue(workspace["skip_threshold"])
        self.seed = LineEdit(); self.seed.setText(workspace["seed"]); self.seed.setPlaceholderText("留空则随机")
        advanced_fields = [("生成音频", self.audio), ("轮询间隔（秒）", self.poll), ("最大重试次数", self.retries), ("模型连续失败阈值", self.threshold)]
        for index, (text, control) in enumerate(advanced_fields):
            row, pair = divmod(index, 2)
            control.setMinimumWidth(0)
            control.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            advanced_form.addWidget(CaptionLabel(text), row * 2, pair)
            advanced_form.addWidget(control, row * 2 + 1, pair)
        advanced_form.setColumnStretch(0, 1); advanced_form.setColumnStretch(1, 1)
        advanced_form.addWidget(CaptionLabel("随机种子"), 4, 0)
        advanced_form.addWidget(self.seed, 5, 0, 1, 2)
        root.addWidget(self.advanced)
        self._advanced_height = max(1, self.advanced.sizeHint().height())
        self._advanced_animation = QPropertyAnimation(self.advanced, b"maximumHeight", self)
        self._advanced_animation.setDuration(180)
        self._advanced_animation.setEasingCurve(QEasingCurve.InOutCubic)
        self._advanced_animation.finished.connect(self._finish_toggle)
        controls = {"model": self.model, "aspect_ratio": self.ratio, "resolution": self.resolution, "duration": self.duration, "generate_audio": self.audio, "poll_interval": self.poll, "max_retries": self.retries, "skip_threshold": self.threshold, "seed": self.seed}
        for key, control in controls.items():
            if hasattr(control, "currentTextChanged"):
                control.currentTextChanged.connect(lambda value, k=key: self._save(k, value))
            elif hasattr(control, "valueChanged"):
                control.valueChanged.connect(lambda value, k=key: self._save(k, value))
            elif hasattr(control, "textChanged"):
                control.textChanged.connect(lambda value, k=key: self._save(k, value))
            elif hasattr(control, "checkedChanged"):
                control.checkedChanged.connect(lambda value, k=key: self._save(k, value))
            elif hasattr(control, "stateChanged"):
                control.stateChanged.connect(lambda value, k=key: self._save(k, bool(value)))
        self._refresh_model_options()
        self.refresh_task_settings()

    def refresh_task_settings(self):
        for widget, key in ((self.retries, 'max_retries'), (self.threshold, 'failure_skip_threshold')):
            blocker = QSignalBlocker(widget)
            widget.setValue(self.config_manager.config['task_strategy'][key])
            del blocker

    def refresh_pool_hint(self, *_):
        pool = self.config_manager.config['model_pool']
        self.pool_hint.setText(f'已启用模型池 · {pool["strategy"]}；实际模型见当前任务，参数按模型适配' if pool.get('enabled') else '')
        self.pool_hint.setVisible(bool(pool.get('enabled')))

    def _save(self, key: str, value) -> None:
        self.config_manager.update(("workspace", key), value)
        self.values_changed.emit(key, value)
        if key in ('model', 'aspect_ratio', 'resolution', 'duration'):
            self._refresh_model_options()

    def _refresh_model_options(self):
        model = self.model.currentText()
        catalog = catalog_snapshot(self.config_manager)
        workspace = self.config_manager.config['workspace']
        before = {key: workspace[key] for key in ('aspect_ratio', 'resolution', 'duration')}
        values = apply_model_options(model, self.ratio, self.resolution, self.duration, self.audio, self.seed, catalog)
        changes = []
        labels = {'aspect_ratio': '比例', 'resolution': '分辨率', 'duration': '时长'}
        for key, value in values.items():
            if value != before[key]:
                self.config_manager.update(('workspace', key), value)
                self.values_changed.emit(key, value)
                changes.append(f'{labels[key]} {before[key]} → {value}')
        if model == H3:
            hint = f"输出尺寸：{h3_size(values['aspect_ratio'], values['resolution'])} · {values['aspect_ratio']}"
        elif model == 'grok-imagine-1.5-video':
            hint = '多张参考图请选择 480p 或 720p'
        else:
            options = model_options(model, catalog)
            durations = options['durations'] or []
            hint = ('支持时长：' + ' / '.join(map(str, durations)) + ' 秒' if len(durations) <= 3 and durations
                    else f'支持时长：{min(durations)}–{max(durations)} 秒' if durations else '提交协议尚未确认')
        if changes:
            message = f'{model} 已调整不支持的选项：' + '；'.join(changes)
            self.parameters_adjusted.emit(message, 'warning')
            hint += '\n' + '；'.join(changes)
        self.size_hint.setText(hint)

    def refresh_catalog(self, *_):
        self.model.set_models(catalog_snapshot(self.config_manager), self.config_manager.config['workspace']['model'])
        selected = self.model.currentText()
        if selected and selected != self.config_manager.config['workspace']['model']:
            self.config_manager.update(('workspace', 'model'), selected)
            self.values_changed.emit('model', selected)
        self._refresh_model_options()

    def toggle_advanced(self) -> None:
        self._advanced_animation.stop()
        self.advanced_expanded = not self.advanced_expanded
        if self.advanced_expanded:
            self.advanced.setVisible(True)
            self.advanced.setMaximumHeight(0)
            target = self._advanced_height
            start = 0
        else:
            start = self.advanced.height()
            target = 0
        self._advanced_animation.setStartValue(start)
        self._advanced_animation.setEndValue(target)
        self._advanced_animation.start()
        self.expand_button.setIcon(FIF.UP if self.advanced_expanded else FIF.DOWN)

    def _finish_toggle(self):
        self.advanced.setVisible(self.advanced_expanded)
        if self.advanced_expanded:
            self.advanced.setMaximumHeight(16777215)
