"""Fluent setting cards backed by the application's JSON config."""

from PyQt5.QtCore import Qt, pyqtSignal, QTime, QSignalBlocker, QTimer
from PyQt5.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget
from qfluentwidgets import (
    CheckBox, ComboBox, ComboBoxSettingCard, FluentIcon as FIF, LineEdit,
    OptionsConfigItem, OptionsValidator, PushButton, SettingCard, SettingCardGroup,
    SpinBox, SwitchSettingCard, CaptionLabel, TitleLabel, TransparentToolButton, ScrollArea, TimePicker,
)
from ..components.custom_widgets import make_card
from ..theme import SettingSurface, style_controls
from core.background import BackgroundJobs
from core.api_client import ApiClient
from core.image_uploader import ImageUploader
from core.model_parameters import MODELS
from core.repository_sync import RepositorySync, GITHUB_REPOSITORY
from ..components.model_options import apply_model_options
from ..components.model_selector import ModelComboBox, catalog_snapshot, usable
from datetime import datetime
import copy
import math
import time


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
    schedule_changed = pyqtSignal()
    task_settings_changed = pyqtSignal()
    detection_changed = pyqtSignal()
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
        self._build_schedule()
        self._build_sync()
        self._build_appearance()
        self.root.addStretch(1)
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setWidget(page)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)
        self.pool_timer = QTimer(self)
        self.pool_timer.setInterval(1000)
        self.pool_timer.timeout.connect(self.refresh_pool_state)
        self.pool_timer.start()
        self.refresh_pool_state()

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

    def _model_combo(self, group, title, path, automatic=False):
        combo = ModelComboBox()
        combo.setMinimumWidth(240)
        combo.setMaximumWidth(560)
        combo.set_models(catalog_snapshot(self.config_manager), self._value(path), automatic=automatic)
        combo.currentTextChanged.connect(lambda model: self.config_manager.update(path, model))
        group.addSettingCard(CustomSettingCard(title, combo))
        return combo

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
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        self.last_model_sync = CaptionLabel('尚未同步')
        self.sync_models_button = PushButton(FIF.SYNC, '同步上游模型')
        row.addWidget(self.last_model_sync, 1)
        row.addWidget(self.sync_models_button)
        group.addSettingCard(CustomSettingCard('上游模型目录', host, FIF.SYNC))

    def _build_pool(self):
        group = self._group("模型池")
        self.pool_group = group
        self.pool_enabled = self._switch(group, '启用模型池', ('model_pool', 'enabled'), FIF.SYNC)
        self.pool_enabled.setToolTip('关闭时使用工作台所选单模型。运行期间修改设置将在下一批任务生效。')
        self.strategy = self._combo(group, "调用策略", ("model_pool", "strategy"), ["轮询", "随机", "优先级"])
        self.failover = self._switch(group, "自动故障转移", ("model_pool", "auto_failover"), FIF.SYNC)
        self.cooldown = self._spin(group, "冷却时间（秒）", ("model_pool", "cooldown"), 1, 3600)
        card = make_card()
        self.models_card = card
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        hint = CaptionLabel('模型列表 · 列表顺序即优先级；修改在下一批生效')
        hint.setWordWrap(True)
        layout.addWidget(hint)
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
        self.task_group = group
        self.auto_detect = self._switch(group, '自动识别模型', ('prompt_detection', 'enabled'))
        self.fallback_model = self._model_combo(group, '无法识别时使用', ('prompt_detection', 'fallback_model'), automatic=True)
        self.auto_detect.checkedChanged.connect(lambda *_: self.detection_changed.emit())
        self.fallback_model.currentIndexChanged.connect(lambda *_: self.detection_changed.emit())
        self.prevent_duplicates = self._switch(group, '防重复提交', ('task_strategy', 'prevent_duplicates'), FIF.SETTING)
        self.auto_convert_prompt = self._switch(group, '自动转换提示词格式', ('prompt_conversion', 'enabled'), FIF.SYNC)
        self.preserve_original_prompt = self._switch(group, '转换后保留原文', ('prompt_conversion', 'preserve_original'), FIF.DOCUMENT)
        self.preserve_original_prompt.setToolTip('保留转换前的额外原文快照；关闭后仍保存实际提交文本，便于核对任务。原始 TXT 文件不改动。')
        self.prefer_same_format = self._switch(group, '故障转移优先同格式模型', ('prompt_conversion', 'prefer_same_format'), FIF.SYNC)
        self.max_concurrency = self._spin(group, '最大并发数', ('task_strategy', 'max_concurrency'), 1, 5)
        self.max_concurrency.setToolTip('同一产品内最多同时处理的任务数；当前产品结束后切换下一产品')
        self.auto_retry = self._switch(group, "失败自动重试", ("task_strategy", "auto_retry"), FIF.SYNC)
        self.max_retry = self._spin(group, "最大重试次数", ("task_strategy", "max_retries"), 0, 20)
        self.max_retry.setToolTip('首次尝试之外的重试次数；5 次重试最多执行 6 次')
        self.retry_interval = self._spin(group, "重试间隔（秒）", ("task_strategy", "retry_interval"), 1, 60)
        self.fail_threshold = self._spin(group, "连续失败阈值", ("task_strategy", "failure_skip_threshold"), 1, 100)
        self.max_retry.valueChanged.connect(lambda *_: self.task_settings_changed.emit())
        self.fail_threshold.valueChanged.connect(lambda *_: self.task_settings_changed.emit())
        self.unmatched = self._combo(group, "未匹配参考图时", ("task_strategy", "unmatched_prompt"),
                                     ["跳过并警告", "仍提交文生视频", "暂停任务"],
                                     ["跳过该任务", "仍提交为文生视频", "暂停任务"])
        self.naming = self._line(group, "下载命名规则", ("task_strategy", "naming_rule"))
        self.open_folder = self._switch(group, "下载完成自动打开", ("task_strategy", "open_folder_after_download"), FIF.FOLDER)

    def _build_defaults(self):
        group = self._group("默认参数")
        self.default_model = self._model_combo(group, "默认模型", ("defaults", "model"))
        self.default_ratio = self._combo(group, "默认比例", ("defaults", "aspect_ratio"), ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "2:3", "3:2"])
        self.default_resolution = self._combo(group, "默认分辨率", ("defaults", "resolution"), ["480p", "720p", "768p", "1080p", "2K", "4K"])
        self.default_duration = self._spin(group, "默认时长（秒）", ("defaults", "duration"), 1, 30)
        self.default_poll = self._spin(group, "默认轮询间隔（秒）", ("defaults", "poll_interval"), 3, 60)
        self.default_model.currentTextChanged.connect(self._refresh_default_options)
        self.default_duration.valueChanged.connect(self._refresh_default_options)
        self._refresh_default_options()

    def _refresh_default_options(self, *_):
        values = apply_model_options(self.default_model.currentText(), self.default_ratio, self.default_resolution, self.default_duration,
                                     catalog=catalog_snapshot(self.config_manager))
        for key, value in values.items():
            if self.config_manager.config['defaults'][key] != value:
                self.config_manager.update(('defaults', key), value)

    def _build_appearance(self):
        group = self._group("外观")
        self.theme = self._combo(group, "主题", ("appearance", "theme"), ["dark", "light", "system"], ["深色", "浅色", "跟随系统"])
        self.language = self._combo(group, "语言", ("appearance", "language"), ["简体中文", "English"])
        self.root.addWidget(CaptionLabel('Yanlin Smart-Creation Matrix v3.1 · 多模型并发、产品批处理、定时执行与GitHub同步'))

    def _build_schedule(self):
        group = self._group('定时执行')
        self.schedule_enabled = self._switch(group, '启用定时执行', ('schedule', 'enabled'), FIF.PLAY)
        self.schedule_time = TimePicker(showSeconds=False)
        self.schedule_time.setTime(QTime.fromString(self._value(('schedule', 'time')), 'HH:mm'))
        group.addSettingCard(CustomSettingCard('定时开始时间', self.schedule_time, FIF.HISTORY))
        self.schedule_mode = self._combo(group, '执行模式', ('schedule', 'mode'), ['once', 'daily'], ['仅一次', '每天重复'])
        self.schedule_after = self._combo(group, '定时队列全部结束后', ('schedule', 'after_finish'), ['keep', 'close'], ['保持运行', '自动关闭软件'])
        self.current_time = CaptionLabel('当前时间：正在读取')
        self.current_time.setWordWrap(True)
        group.addSettingCard(CustomSettingCard('当前时间 / 网络校时', self.current_time, FIF.HISTORY))
        self.schedule_enabled.checkedChanged.connect(lambda *_: self.schedule_changed.emit())
        self.schedule_time.timeChanged.connect(self._schedule_time_changed)
        self.schedule_mode.currentIndexChanged.connect(lambda *_: self.schedule_changed.emit())
        # Completion behavior does not re-arm an already consumed occurrence.

    def _schedule_time_changed(self, value):
        self.config_manager.update(('schedule', 'time'), value.toString('HH:mm'))
        self.schedule_changed.emit()

    def update_schedule_state(self, now, clock_status):
        self.current_time.setText(now.strftime('%Y-%m-%d %H:%M:%S') + '\n' + clock_status)
        enabled = bool(self.config_manager.config['schedule']['enabled'])
        if self.schedule_enabled.isChecked() != enabled:
            blocker = QSignalBlocker(self.schedule_enabled)
            self.schedule_enabled.setChecked(enabled)
            del blocker

    def _build_sync(self):
        group = self._group('GitHub代码同步')
        self.sync_button = PushButton(FIF.SYNC, '同步到GitHub')
        self.sync_button.setToolTip('提交已修改的代码并推送main；配置、密钥、视频及日志不会上传')
        self.sync_button.clicked.connect(self.sync_github)
        group.addSettingCard(CustomSettingCard('GitHub 私人仓库同步', self.sync_button, FIF.SYNC))

    def sync_github(self):
        self.sync_button.setEnabled(False)
        self.sync_button.setText('正在同步...')
        config = copy.deepcopy(self.config_manager.config)
        def work():
            secrets = [config['api'].get(key, '') for key in ('api_key', 'upload_api_key')]
            return RepositorySync(secrets=secrets, log=self.jobs.log_message.emit).sync()
        def reset():
            self.sync_button.setEnabled(True); self.sync_button.setText('同步到GitHub')
        def done(result):
            reset()
            from qfluentwidgets import InfoBar
            InfoBar.success('GitHub同步成功', '已同步到GitHub，最新commit: ' + result['commit'][:12], parent=self, duration=5000)
        def failed(message):
            reset()
            self.log_callback(message, 'error')
            from qfluentwidgets import InfoBar
            InfoBar.error('GitHub同步失败', message, parent=self, duration=7000)
        self.jobs.start(work, done, failed)

    def _add_model_row(self, name, enabled, status, persist=True):
        row = make_card()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(20, 10, 20, 10)
        check = CheckBox("启用")
        check.setChecked(enabled)
        combo = ModelComboBox()
        combo.set_models(catalog_snapshot(self.config_manager), name, keep_missing=True)
        state = CaptionLabel(f"● {status}")
        color = "#67c23a" if status == "健康" else "#e6a23c"
        state.setTextColor(color, color)
        remove = TransparentToolButton(FIF.DELETE)
        remove.setToolTip("删除模型")
        for widget in (check, combo, state, remove):
            layout.addWidget(widget, 1 if widget is combo else 0)
        self.model_layout.addWidget(row)
        self.model_rows.append((row, check, combo, state))
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
        previous = {model['name']: model for model in self._value(('model_pool', 'models'))}
        self.config_manager.update(("model_pool", "models"), [
            dict(previous.get(combo.currentText(), {'status': '健康'}), name=combo.currentText(), enabled=check.isChecked())
            for _, check, combo, _ in self.model_rows
        ])
        self.refresh_pool_state()

    def update_pool_state(self, snapshot):
        """Merge runtime health only; user model selection belongs to the next batch."""
        updates = {item['name']: item for item in snapshot}
        models = copy.deepcopy(self._value(('model_pool', 'models')))
        changed = False
        for model in models:
            state = updates.get(model['name'])
            if state is None:
                continue
            for key in ('status', 'cooldown_until', 'consecutive_failures'):
                if key in state and model.get(key) != state[key]:
                    model[key] = state[key]; changed = True
        if changed:
            self.config_manager.update(('model_pool', 'models'), models)
        self.refresh_pool_state()

    def refresh_pool_state(self):
        now = time.time()
        models = copy.deepcopy(self._value(('model_pool', 'models')))
        changed = False
        for model in models:
            if model.get('status') == '冷却中':
                if not model.get('cooldown_until'):
                    model['cooldown_until'] = now + self._value(('model_pool', 'cooldown'))
                    changed = True
                elif model['cooldown_until'] <= now:
                    model.update(status='健康', cooldown_until=0, consecutive_failures=0)
                    changed = True
        if changed:
            self.config_manager.update(('model_pool', 'models'), models)
        states = {model['name']: model for model in models}
        catalog = catalog_snapshot(self.config_manager)
        for _, check, combo, label in self.model_rows:
            model = states.get(combo.currentText(), {})
            record = catalog.get(combo.currentText())
            if record is None or not usable(record):
                status = '已下架' if record is None else '非视频模型' if record.get('kind') != 'video' else '协议待确认'
                label.setText('● ' + status)
                label.setTextColor('#92929b', '#92929b')
                check.setEnabled(False)
                continue
            check.setEnabled(True)
            cooling = model.get('status') == '冷却中'
            remaining = max(0, math.ceil(model.get('cooldown_until', 0)-now))
            label.setText(f'● 冷却中 {remaining}秒' if cooling else '● 健康')
            color = '#e6a23c' if cooling else '#67c23a'
            label.setTextColor(color, color)

    def refresh_catalog(self, *_):
        catalog = catalog_snapshot(self.config_manager)
        self.default_model.set_models(catalog, self._value(('defaults', 'model')))
        if self.default_model.currentText():
            self.config_manager.update(('defaults', 'model'), self.default_model.currentText())
        self.fallback_model.set_models(catalog, self._value(('prompt_detection', 'fallback_model')), automatic=True)
        self.config_manager.update(('prompt_detection', 'fallback_model'), self.fallback_model.currentText())
        renamed = False
        for _, _, combo, _ in self.model_rows:
            old_name = combo.currentText()
            combo.set_models(catalog, combo.currentText(), keep_missing=True)
            renamed = renamed or old_name != combo.currentText()
        if renamed:
            self._persist_models()
        self._refresh_default_options()
        self.refresh_pool_state()
        self.refresh_sync_state()

    def refresh_sync_state(self):
        controller = getattr(self.config_manager, 'model_catalog_controller', None)
        if controller is None:
            return
        self.sync_models_button.setEnabled(not controller.syncing)
        self.sync_models_button.setText('正在同步...' if controller.syncing else '同步上游模型')
        fetched = controller.catalog.fetched_at
        text = datetime.fromtimestamp(fetched).strftime('%Y-%m-%d %H:%M:%S') if fetched else '尚未同步'
        self.last_model_sync.setText('上次同步：' + text)

    def model_sync_finished(self, success, count, message):
        from qfluentwidgets import InfoBar
        (InfoBar.success if success else InfoBar.warning)('模型同步', message, parent=self, duration=4500)
        self.refresh_sync_state()

    def refresh_task_settings(self, *_):
        for widget, key in ((self.max_retry, 'max_retries'), (self.fail_threshold, 'failure_skip_threshold')):
            blocker = QSignalBlocker(widget)
            widget.setValue(self._value(('task_strategy', key)))
            del blocker

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
