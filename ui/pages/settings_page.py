"""Fluent setting cards backed by the application's JSON config."""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget
from qfluentwidgets import (
    CheckBox, ComboBox, ComboBoxSettingCard, FluentIcon as FIF, LineEdit,
    OptionsConfigItem, OptionsValidator, PushButton, SettingCard, SettingCardGroup,
    SpinBox, SwitchSettingCard, CaptionLabel, TitleLabel, TransparentToolButton, ScrollArea,
)
from ..components.custom_widgets import make_card
from ..theme import SettingSurface, style_controls
from core.background import BackgroundJobs
from core.api_client import ApiClient
from core.image_uploader import ImageUploader
from core.model_parameters import MODELS
from ..components.model_options import apply_model_options
import copy


class JsonComboBoxSettingCard(SettingSurface, ComboBoxSettingCard):
    """Use Fluent's combo card without its unrelated config/config.json writes."""
    def _onCurrentIndexChanged(self, index):
        self.configItem.value = self.comboBox.itemData(index)

    def setValue(self, value):
        if value in self.optionToText:
            self.comboBox.setCurrentText(self.optionToText[value])


class StudioSwitchSettingCard(SettingSurface, SwitchSettingCard):
    pass


class CustomSettingCard(SettingSurface, SettingCard):
    # 1.11.3 has no LineEditSettingCard or SpinBoxSettingCard: compose its
    # SettingCard with its LineEdit/SpinBox to retain the Fluent design.
    def __init__(self, title, control, icon=FIF.SETTING, parent=None):
        super().__init__(icon, title, parent=parent)
        self.hBoxLayout.addWidget(control, 1, Qt.AlignVCenter)
        self.hBoxLayout.addSpacing(20)
        self.setMinimumHeight(64)


class SettingsPage(QWidget):
    debug_mode_changed = pyqtSignal(bool)
    def __init__(self, config_manager, log_callback, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.config_manager = config_manager
        self.log_callback = log_callback
        self.jobs = BackgroundJobs(self)
        self.jobs.log_message.connect(self.log_callback)
        self.model_rows = []
        self.groups = []
        page = QWidget()
        page.setObjectName("settingsContent")
        self.root = QVBoxLayout(page)
        self.root.setContentsMargins(24, 22, 24, 30)
        self.root.setSpacing(16)
        self.root.addWidget(TitleLabel("设置"))
        self.root.addWidget(CaptionLabel("连接配置、模型池与任务偏好 · 修改后自动保存"))
        self._build_api()
        self._build_pool()
        self._build_task()
        self._build_defaults()
        self._build_appearance()
        self.root.addStretch(1)
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setWidget(page)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)

    def _group(self, title):
        group = SettingCardGroup(title)
        group.cardLayout.setSpacing(16)
        self.root.addWidget(group)
        self.groups.append(group)
        return group

    def _value(self, path):
        return self.config_manager.config[path[0]][path[1]]

    def _line(self, group, title, path, secret=False):
        edit = LineEdit()
        edit.setText(self._value(path))
        edit.setMinimumWidth(240)
        edit.textChanged.connect(lambda v: self.config_manager.update(path, v))
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        if secret:
            edit.setEchoMode(LineEdit.Password)
            reveal = TransparentToolButton(FIF.VIEW)
            reveal.setToolTip("显示或隐藏密钥")
            reveal.clicked.connect(lambda: self._toggle_secret(edit, reveal))
            row.addWidget(reveal)
        if path == ("api", "api_key"):
            self.test_button = PushButton(FIF.CONNECT, "测试连接")
            self.test_button.setToolTip('分别检查视频 API 和上传 API；上传一张微型测试图片，不创建视频任务')
            self.test_button.clicked.connect(self.test_connection)
            row.addWidget(self.test_button)
        group.addSettingCard(CustomSettingCard(title, host, FIF.LINK))
        return edit

    def _combo(self, group, title, path, choices, texts=None):
        item = OptionsConfigItem(path[0], path[1], self._value(path), OptionsValidator(choices))
        card = JsonComboBoxSettingCard(item, FIF.SETTING, title, texts=texts or choices)
        card.comboBox.setMinimumWidth(200)
        # Model-dependent choices are rebuilt without signals. Persist the actual
        # selected item even when the setting card's cached ConfigItem is unchanged.
        card.comboBox.currentIndexChanged.connect(
            lambda index: self.config_manager.update(path, card.comboBox.itemData(index)) if index >= 0 else None)
        group.addSettingCard(card)
        return card.comboBox

    def _spin(self, group, title, path, low, high):
        control = SpinBox()
        control.setRange(low, high)
        control.setValue(self._value(path))
        control.setFixedWidth(200)
        control.valueChanged.connect(lambda v: self.config_manager.update(path, v))
        group.addSettingCard(CustomSettingCard(title, control))
        return control

    def _switch(self, group, title, path, icon=FIF.SETTING):
        card = StudioSwitchSettingCard(icon, title)
        card.setChecked(self._value(path))
        card.checkedChanged.connect(lambda v: self.config_manager.update(path, bool(v)))
        group.addSettingCard(card)
        return card

    def _build_api(self):
        group = self._group("API 配置")
        self.base_url = self._line(group, "API Base URL", ("api", "base_url"))
        self.api_key = self._line(group, "API Key", ("api", "api_key"), True)
        self.upload_url = self._line(group, "图床上传 URL", ("api", "upload_url"))
        self.upload_key = self._line(group, '图床 API Key（上传 Token）', ('api', 'upload_api_key'), True)
        self.upload_key.setPlaceholderText('服务商签发的素材上传 Token')
        self.upload_key.setToolTip('默认图床使用 X-Upload-Token 和 Bearer 头；视频 API Key 不保证具有上传权限')
        self.auto_upload = self._switch(group, "自动上传缺失图片", ("api", "auto_upload_missing"), FIF.CLOUD)
        self.debug_mode = self._switch(group, '调试模式', ('diagnostics', 'debug_mode'), FIF.INFO)
        self.debug_mode.setToolTip('记录参考图、提示词和脱敏请求体；额外下载上传后的图片检查尺寸和格式')
        self.debug_mode.checkedChanged.connect(self.debug_mode_changed)

    def _build_pool(self):
        group = self._group("模型池")
        self.strategy = self._combo(group, "调用策略", ("model_pool", "strategy"), ["轮询", "随机", "优先级"])
        self.failover = self._switch(group, "自动故障转移", ("model_pool", "auto_failover"), FIF.SYNC)
        self.cooldown = self._spin(group, "冷却时间（秒）", ("model_pool", "cooldown"), 10, 300)
        card = make_card()
        self.models_card = card
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        layout.addWidget(CaptionLabel("模型列表"))
        self.model_layout = QVBoxLayout()
        self.model_layout.setSpacing(8)
        layout.addLayout(self.model_layout)
        for model in self._value(("model_pool", "models")):
            self._add_model_row(model["name"], model["enabled"], model["status"], persist=False)
        self.add_model = PushButton(FIF.ADD, "添加模型")
        self.add_model.clicked.connect(lambda: self._add_model_row("video-v3", True, "健康"))
        layout.addWidget(self.add_model, 0, Qt.AlignLeft)
        card.setFixedHeight(layout.sizeHint().height())
        group.addSettingCard(card)

    def _build_task(self):
        group = self._group("任务策略")
        self.auto_retry = self._switch(group, "失败自动重试", ("task_strategy", "auto_retry"), FIF.SYNC)
        self.max_retry = self._spin(group, "最大重试次数", ("task_strategy", "max_retries"), 1, 20)
        self.retry_interval = self._spin(group, "重试间隔（秒）", ("task_strategy", "retry_interval"), 1, 60)
        self.fail_threshold = self._spin(group, "连续失败阈值", ("task_strategy", "failure_skip_threshold"), 1, 100)
        self.unmatched = self._combo(group, "未匹配提示词处理", ("task_strategy", "unmatched_prompt"), ["跳过并警告", "仍提交文生视频", "暂停任务"])
        self.naming = self._line(group, "下载命名规则", ("task_strategy", "naming_rule"))
        self.open_folder = self._switch(group, "下载完成自动打开", ("task_strategy", "open_folder_after_download"), FIF.FOLDER)

    def _build_defaults(self):
        group = self._group("默认参数")
        self.default_model = self._combo(group, "默认模型", ("defaults", "model"), MODELS)
        self.default_ratio = self._combo(group, "默认比例", ("defaults", "aspect_ratio"), ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "2:3", "3:2"])
        self.default_resolution = self._combo(group, "默认分辨率", ("defaults", "resolution"), ["480p", "720p", "768p", "1080p", "2K", "4K"])
        self.default_duration = self._spin(group, "默认时长（秒）", ("defaults", "duration"), 1, 30)
        self.default_poll = self._spin(group, "默认轮询间隔（秒）", ("defaults", "poll_interval"), 3, 60)
        self.default_model.currentTextChanged.connect(self._refresh_default_options)
        self.default_duration.valueChanged.connect(self._refresh_default_options)
        self._refresh_default_options()

    def _refresh_default_options(self, *_):
        values = apply_model_options(self.default_model.currentText(), self.default_ratio, self.default_resolution, self.default_duration)
        for key, value in values.items():
            if self.config_manager.config['defaults'][key] != value:
                self.config_manager.update(('defaults', key), value)

    def _build_appearance(self):
        group = self._group("外观")
        self.theme = self._combo(group, "主题", ("appearance", "theme"), ["dark", "light", "system"], ["深色", "浅色", "跟随系统"])
        self.language = self._combo(group, "语言", ("appearance", "language"), ["简体中文", "English"])
        self.root.addWidget(CaptionLabel('StoryboardVideoStudio v2.0A · 模型池和自动重试将在阶段2B启用'))

    def _add_model_row(self, name, enabled, status, persist=True):
        row = make_card()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(20, 10, 20, 10)
        check = CheckBox("启用")
        check.setChecked(enabled)
        combo = ComboBox()
        combo.addItems(MODELS)
        combo.setCurrentText(name)
        state = CaptionLabel(f"● {status}")
        color = "#22c55e" if status == "健康" else "#f59e0b"
        state.setTextColor(color, color)
        remove = TransparentToolButton(FIF.DELETE)
        remove.setToolTip("删除模型")
        for widget in (check, combo, state, remove):
            layout.addWidget(widget, 1 if widget is combo else 0)
        self.model_layout.addWidget(row)
        self.model_rows.append((row, check, combo, status))
        check.stateChanged.connect(self._persist_models)
        combo.currentTextChanged.connect(self._persist_models)
        remove.clicked.connect(lambda: self._remove_model_row(row))
        if persist:
            style_controls(row)
            self._persist_models()
            self.models_card.setFixedHeight(self.models_card.layout().sizeHint().height())

    def _remove_model_row(self, row):
        # Drop Python references before deleteLater; future edits must not touch deleted Qt objects.
        self.model_rows = [entry for entry in self.model_rows if entry[0] is not row]
        self.model_layout.removeWidget(row)
        row.deleteLater()
        self._persist_models()
        self.models_card.setFixedHeight(self.models_card.layout().sizeHint().height())

    def _persist_models(self, *_):
        self.config_manager.update(("model_pool", "models"), [
            {"name": combo.currentText(), "enabled": check.isChecked(), "status": status}
            for _, check, combo, status in self.model_rows
        ])

    def test_connection(self):
        config = copy.deepcopy(self.config_manager.config)
        self.test_button.setEnabled(False)
        self.test_button.setText('正在测试...')
        def check():
            client = ApiClient(config['api']['base_url'], config['api']['api_key'], log=self.jobs.log_message.emit)
            uploader = ImageUploader.from_config(config, log=self.jobs.log_message.emit)
            try:
                return client.test_connection(uploader)
            finally:
                client.close()
                uploader.close()
        def finish(result):
            self.test_button.setEnabled(True)
            self.test_button.setText('测试连接')
            self.last_connection_result = result
            messages = []
            for name, title in (('video', '视频 API'), ('upload', '上传 API')):
                item = result[name]
                message = f"{title}：{'成功' if item['ok'] else '失败'} · {item['message']}"
                self.log_callback(message, 'success' if item['ok'] else 'error')
                messages.append(message)
            self._notice('\n'.join(messages), result['ok'])
        def failed(message):
            result = dict(ok=False, video=dict(ok=False, message=message), upload=dict(ok=False, message='测试未完成'))
            finish(result)
        self.jobs.start(check, finish, failed)

    def _notice(self, message, success=True):
        from qfluentwidgets import InfoBar
        self.log_callback(message, 'success' if success else 'error')
        (InfoBar.success if success else InfoBar.error)('测试连接', message, parent=self, duration=4500)

    @staticmethod
    def _toggle_secret(field, button):
        visible = field.echoMode() == LineEdit.Password
        field.setEchoMode(LineEdit.Normal if visible else LineEdit.Password)
        button.setIcon(FIF.HIDE if visible else FIF.VIEW)
