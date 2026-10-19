"""Ecommerce form and queued Qt presentation of the existing Python pipeline."""
from pathlib import Path
import threading

from PyQt5.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import QFileDialog, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    CardWidget, CaptionLabel, CheckBox, ComboBox, InfoBar, LineEdit, ListWidget,
    PlainTextEdit, PrimaryPushButton, PushButton, SpinBox, StrongBodyLabel,
    TextEdit, TitleLabel, FluentIcon as FIF,
)

from core.ecommerce.platforms import (
    domestic, overseas, LANGUAGE_TEXT, RATIOS, MAX_PLATFORMS, detect_conflicts,
)
from core.ecommerce.image_client import TIER_SHORT_EDGE
from core.ecommerce.manager import EcommerceImageManager
from core.i18n import tr
from core.log_redaction import redact_structure, redact_text
from .. import materials
from ..components.ecommerce_result_panel import EcommerceResultPanel
from ..components.ecommerce_history_panel import EcommerceHistoryPanel, RESUMABLE_STATUSES
from ..components.studio_dialog import StudioDialog, safe_delete_dialog
from ..file_actions import open_local
from ..responsive import WrapLayout, scrollable_body
from ..theme import style_controls, style_page
from ..tokens import px


# Parentless QThreads survive even if a containing widget is deleted externally.
# Release only on Qt's native finished signal, after run() has actually returned.
_LIVE_WORKERS = set()
def _image_filter():
    return tr('图片 (*.png *.jpg *.jpeg *.webp *.bmp)')


def _stage_text(status):
    return {
        'analyzing': tr('AI分析中'), 'generating': tr('图片生成中'), 'succeeded': tr('已完成'),
        'partial': tr('部分成功'), 'failed': tr('失败'), 'cancelled': tr('已取消'),
    }.get(status, status)


def _item_text(status):
    return {
        'pending': tr('等待'), 'running': tr('生成中'), 'succeeded': tr('成功'),
        'failed': tr('失败'), 'skipped': tr('跳过'),
    }.get(status, status)


class EcommerceWorker(QThread):
    group_changed = pyqtSignal(object)
    item_changed = pyqtSignal(object)
    log_message = pyqtSignal(str, str)
    finished = pyqtSignal(str, str)

    def __init__(self, manager_factory, arguments, cancel_event, secrets=(), *, operation='start'):
        super().__init__()
        self.manager_factory = manager_factory
        self.arguments = arguments
        self.cancel_event = cancel_event
        self.secrets = tuple(secrets)
        self.operation = operation

    def _clean(self, value):
        return redact_text(value, self.secrets)

    def run(self):
        try:
            manager = self.manager_factory()
            self.secrets += (manager.api_key,)
            manager.on_group = lambda rec: self.group_changed.emit(redact_structure(rec, self._clean))
            manager.on_item = lambda rec: self.item_changed.emit(redact_structure(rec, self._clean))
            manager.on_log = lambda msg, level: self.log_message.emit(self._clean(msg), level)
            runners = {
                'start': manager.start_group,
                'regenerate': manager.regenerate_items,
                'resume': manager.resume_group,
            }
            group_id = runners[self.operation](**self.arguments, cancel_event=self.cancel_event)
            self.finished.emit(group_id, '')
        except Exception as error:
            self.finished.emit('', self._clean(error))
        finally:
            self.manager_factory = None


class EcommercePage(QWidget):
    idle = pyqtSignal()

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.setObjectName('ecommercePage')
        self.config_manager = config_manager
        self._worker = None
        self.cancel_event = threading.Event()
        self._secrets = ()
        self._group = None
        self._items = {}
        self.item_labels = {}
        self._logs = []
        self._close_when_idle = False
        self.product_image_paths = []
        self.style_image_paths = []
        self._form_cards = []
        defaults = config_manager.config.get('ecommerce', {})

        root = scrollable_body(self)
        root.setContentsMargins(px(26), px(16), px(26), px(20))
        root.setSpacing(px(16))
        root.addWidget(TitleLabel(tr('电商图制作')))
        hint = CaptionLabel(tr('选择平台与产品资料 · AI分析 → 图片生成 → 完成后打开输出目录'))
        hint.setWordWrap(True)
        root.addWidget(hint)

        platform_layout = self._card(root, tr('选择平台（最多 {count} 个）').format(count=MAX_PLATFORMS))
        self.platform_checkboxes = {}
        self.platform_groups = {}
        for label, records in ((tr('国内'), domestic()), (tr('海外'), overseas())):
            section = QWidget()
            layout = QVBoxLayout(section)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(StrongBodyLabel(tr(label)))
            choices = WrapLayout(spacing=px(12))
            for record in records:
                checkbox = CheckBox(record['name'])
                checkbox.setObjectName('platform_' + record['id'])
                self.platform_checkboxes[record['id']] = checkbox
                checkbox.toggled.connect(lambda checked, key=record['id']: self._platform_changed(key, checked))
                choices.addWidget(checkbox)
            layout.addLayout(choices)
            self.platform_groups[label] = section
            platform_layout.addWidget(section)
        self.background_conflict = self._text_label()
        self.ratio_conflict = self._text_label()
        platform_layout.addWidget(self.background_conflict)
        platform_layout.addWidget(self.ratio_conflict)

        product_layout = self._card(root, tr('产品资料'))
        self.product_name = LineEdit()
        self.product_name.setPlaceholderText(tr('产品名称（必填）'))
        self.product_info = PlainTextEdit()
        self.product_info.setPlaceholderText(tr('产品信息（必填）：材质、尺寸、功能、卖点、适用场景等'))
        self.product_info.setMinimumHeight(px(112))
        product_layout.addWidget(self.product_name)
        product_layout.addWidget(self.product_info)

        options = self._card(root, tr('图片配置'))
        row = WrapLayout(spacing=px(16))
        self.language = ComboBox()
        for key, value in LANGUAGE_TEXT.items():
            self.language.addItem(tr(value), userData=key)
        self._select_data(self.language, defaults.get('language', 'zh'))
        row.addLayout(self._field(tr('图片文字语言'), self.language))
        self.tier = ComboBox()
        self.tier.addItems(list(TIER_SHORT_EDGE))
        self.tier.setCurrentText(defaults.get('default_tier', next(iter(TIER_SHORT_EDGE))))
        row.addLayout(self._field(tr('分辨率档位'), self.tier))
        options.addLayout(row)
        counts = WrapLayout(spacing=px(16))
        self.carousel_count = SpinBox()
        self.carousel_count.setRange(1, 12)
        self.carousel_count.setValue(defaults.get('default_carousel_count', 3))
        self.detail_count = SpinBox()
        self.detail_count.setRange(0, 12)
        self.detail_count.setValue(defaults.get('default_detail_count', 4))
        self.carousel_ratio, self.detail_ratio = ComboBox(), ComboBox()
        for combo, key in ((self.carousel_ratio, 'default_carousel_ratio'),
                           (self.detail_ratio, 'default_detail_ratio')):
            combo.addItems(RATIOS)
            combo.setCurrentText(defaults.get(key, RATIOS[0]))
        for title, widget in ((tr('轮播图张数'), self.carousel_count), (tr('轮播图比例'), self.carousel_ratio),
                              (tr('详情图张数'), self.detail_count), (tr('详情图比例'), self.detail_ratio)):
            counts.addLayout(self._field(title, widget))
        options.addLayout(counts)

        references = self._card(root, tr('参考图片（可选）'))
        reference_hint = CaptionLabel(tr('产品图最多 6 张，风格图最多 1 张；按产品图在前、风格图最后提交。'))
        reference_hint.setWordWrap(True)
        references.addWidget(reference_hint)
        self.product_list = ListWidget()
        self.product_list.setFixedHeight(px(120))
        references.addWidget(self.product_list)
        products = WrapLayout()
        self.add_products_button = PushButton(tr('添加产品图'))
        self.remove_product_button = PushButton(tr('移除所选产品图'))
        self.clear_products_button = PushButton(tr('清空产品图'))
        self.add_products_button.clicked.connect(self._choose_products)
        self.remove_product_button.clicked.connect(self._remove_product)
        self.clear_products_button.clicked.connect(self._clear_products)
        for button in (self.add_products_button, self.remove_product_button, self.clear_products_button):
            products.addWidget(button)
        references.addLayout(products)
        self.style_label = self._text_label(tr('未选择风格图'))
        references.addWidget(self.style_label)
        style = WrapLayout()
        self.choose_style_button = PushButton(tr('选择 / 替换风格图'))
        self.clear_style_button = PushButton(tr('清除风格图'))
        self.choose_style_button.clicked.connect(self._choose_style)
        self.clear_style_button.clicked.connect(self._clear_style)
        style.addWidget(self.choose_style_button)
        style.addWidget(self.clear_style_button)
        references.addLayout(style)

        actions = WrapLayout()
        self.start_button = PrimaryPushButton(tr('开始生成'))
        self.cancel_button = PushButton(tr('取消'))
        self.cancel_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_generation)
        self.cancel_button.clicked.connect(self.cancel_generation)
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)
        root.addLayout(actions)
        progress = self._card(root, tr('生成进度'), form=False)
        self.stage_label = self._text_label(tr('等待开始'))
        self.summary_label = self._text_label()
        progress.addWidget(self.stage_label)
        progress.addWidget(self.summary_label)
        self.items_layout = QVBoxLayout()
        progress.addLayout(self.items_layout)
        self.open_output_button = PushButton(FIF.FOLDER, tr('打开输出文件夹'))
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output)
        progress.addWidget(self.open_output_button)
        self.log_box = TextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(px(160))
        self.log_box.document().setMaximumBlockCount(1000)
        progress.addWidget(self.log_box)
        self.result_panel = EcommerceResultPanel(self)
        self.result_panel.regenerate_requested.connect(lambda key: self._run_regenerate([key]))
        self.result_panel.regenerate_failed_requested.connect(self._run_regenerate)
        self.result_panel.log_message.connect(self._append_log)
        self.regenerate_failed_button = self.result_panel.regenerate_failed_button
        root.addWidget(self.result_panel)
        self.history_panel = EcommerceHistoryPanel(_stage_text, self)
        self.history_panel.refresh_requested.connect(self._refresh_history)
        self.history_panel.view_requested.connect(self._view_history_group)
        self.history_panel.resume_requested.connect(self._run_resume)
        self.history_panel.open_requested.connect(self._open_history_folder)
        self.history_panel.delete_requested.connect(self._delete_history_group)
        root.addWidget(self.history_panel)
        root.addStretch(1)
        style_controls(self)
        style_page(self)
        materials.register_theme_callback(self._refresh_colors)
        self._refresh_colors()
        self._refresh_conflicts()
        self._refresh_history()

    @property
    def busy(self):
        return self._worker is not None

    def _card(self, root, title, form=True):
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(px(18), px(16), px(18), px(16))
        layout.setSpacing(px(12))
        layout.addWidget(StrongBodyLabel(tr(title)))
        root.addWidget(card)
        if form:
            self._form_cards.append(card)
        return layout

    @staticmethod
    def _field(title, widget):
        layout = QVBoxLayout()
        layout.addWidget(CaptionLabel(tr(title)))
        layout.addWidget(widget)
        return layout

    @staticmethod
    def _text_label(text=''):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _select_data(combo, value):
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    def selected_platforms(self):
        return [key for key, checkbox in self.platform_checkboxes.items() if checkbox.isChecked()]

    def _notice(self, message, warning=False):
        method = InfoBar.warning if warning else InfoBar.error
        method(tr('电商图'), self._clean(message), parent=self, duration=5000)

    def _platform_changed(self, key, checked):
        if checked and len(self.selected_platforms()) > MAX_PLATFORMS:
            checkbox = self.platform_checkboxes[key]
            checkbox.blockSignals(True)
            checkbox.setChecked(False)
            checkbox.blockSignals(False)
            self._notice(tr('最多选择 {count} 个平台').format(count=MAX_PLATFORMS), warning=True)
        self._refresh_conflicts()

    def _refresh_conflicts(self):
        conflicts = detect_conflicts(self.selected_platforms())
        self.background_conflict.setText('\n'.join(c['tip'] for c in conflicts if c['type'] == 'background'))
        self.ratio_conflict.setText('\n'.join(c['tip'] for c in conflicts if c['type'].startswith('ratio_')))
        self.background_conflict.setVisible(bool(self.background_conflict.text()))
        self.ratio_conflict.setVisible(bool(self.ratio_conflict.text()))
        self.start_button.setEnabled(not self.busy and not self.background_conflict.text())

    def _refresh_colors(self):
        colors = materials.palette()
        self.background_conflict.setStyleSheet('color: %s;' % colors['error'])
        self.ratio_conflict.setStyleSheet('color: %s;' % colors['warning'])
        for label in (self.stage_label, self.summary_label, self.style_label, *self.item_labels.values()):
            label.setStyleSheet('color: %s;' % colors['text1'])
        self.result_panel.refresh_colors()
        self.history_panel.refresh_colors()
        self.log_box.clear()
        for message, level in self._logs:
            self._insert_log(message, level)

    def _clean(self, value):
        current_key = self.config_manager.config.get('api', {}).get('api_key', '')
        return redact_text(value, self._secrets + (current_key,))

    def _choose_products(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr('选择产品图'), '', _image_filter())
        paths = [str(Path(path).resolve()) for path in paths]
        merged = list(dict.fromkeys(self.product_image_paths + paths))
        if len(merged) > 6:
            self._notice(tr('产品图最多 6 张，请移除后再添加'))
            return
        self.product_image_paths = merged
        self._refresh_references()

    def _remove_product(self):
        index = self.product_list.currentRow()
        if 0 <= index < len(self.product_image_paths):
            self.product_image_paths.pop(index)
        self._refresh_references()

    def _clear_products(self):
        self.product_image_paths.clear()
        self._refresh_references()

    def _choose_style(self):
        path, _ = QFileDialog.getOpenFileName(self, tr('选择风格图'), '', _image_filter())
        if path:
            self.style_image_paths = [str(Path(path).resolve())]
            self._refresh_references()

    def _clear_style(self):
        self.style_image_paths.clear()
        self._refresh_references()

    def _refresh_references(self):
        self.product_list.clear()
        for path in self.product_image_paths:
            self.product_list.addItem(self._clean(Path(path).name))
            self.product_list.item(self.product_list.count() - 1).setToolTip(self._clean(path))
        self.style_label.setText(self._clean(Path(self.style_image_paths[0]).name)
                                 if self.style_image_paths else tr('未选择风格图'))

    def _arguments(self):
        selected = self.selected_platforms()
        if not 1 <= len(selected) <= MAX_PLATFORMS:
            raise ValueError(tr('请选择 1 至 {count} 个平台').format(count=MAX_PLATFORMS))
        for conflict in detect_conflicts(selected):
            if conflict['type'] == 'background':
                raise ValueError(conflict['tip'])
        name, info = self.product_name.text().strip(), self.product_info.toPlainText().strip()
        if not name or not info:
            raise ValueError(tr('产品名称与产品信息均为必填'))
        d, g = self.carousel_count.value(), self.detail_count.value()
        if d + g < 1:
            raise ValueError(tr('至少生成 1 张图'))
        if len(self.product_image_paths) > 6 or len(self.style_image_paths) > 1:
            raise ValueError(tr('产品图最多 6 张，风格图最多 1 张'))
        api = self.config_manager.config.get('api', {})
        if not str(api.get('base_url') or '').strip() or not str(api.get('api_key') or '').strip():
            raise ValueError(tr('请先到“设置”页填写 API 地址与密钥；AI 分析需要含文本模型权限的密钥'))
        return dict(platforms=selected, product_name=name, product_info=info,
                    language=self.language.currentData(), carousel_count=d, detail_count=g,
                    carousel_ratio=self.carousel_ratio.currentText(), detail_ratio=self.detail_ratio.currentText(),
                    tier=self.tier.currentText(),
                    product_image_paths=[str(Path(p).resolve()) for p in self.product_image_paths],
                    style_image_path=str(Path(self.style_image_paths[0]).resolve()) if self.style_image_paths else None)

    def _make_manager(self):
        # Called in the worker; this factory reads configuration, never QWidget state.
        return EcommerceImageManager.build_from_config(self.config_manager)

    def start_generation(self):
        if self.busy:
            return
        try:
            arguments = self._arguments()
        except ValueError as error:
            self._notice(str(error))
            return
        self._secrets = (self.config_manager.config['api']['api_key'],)
        self._clear_results()
        self._logs.clear()
        self.log_box.clear()
        self.stage_label.setText(tr('AI分析中'))
        self._append_log(tr('AI 分析需要含文本模型权限的密钥；图片按所选配置生成。'), 'info')
        self._launch_worker(arguments)

    def _clear_results(self):
        self._group, self._items = None, {}
        for label in self.item_labels.values():
            self.items_layout.removeWidget(label)
            label.deleteLater()
        self.item_labels.clear()
        self.result_panel.clear()
        self.summary_label.clear()
        self.open_output_button.setEnabled(False)
        self.stage_label.setText(tr('等待开始'))

    def _refresh_history(self):
        store = self.config_manager.ecommerce_store()
        groups = store.list_groups(limit=50)
        for group in groups:
            if group['status'] in ('analyzing', 'generating'):
                counts = store.item_counts(group['group_id'])
                group['totals'] = dict(counts, total=sum(counts.values()))
        self.history_panel.update_groups(redact_structure(groups, self._clean), store.persistence_available)

    def _view_history_group(self, group_id):
        if self.busy:
            return
        if not self.config_manager.ecommerce_store().get_group(group_id):
            self._notice(tr('当前生成组不存在'))
            self._refresh_history()
            return
        self._clear_results()
        self._refresh_results(group_id)

    def _run_resume(self, group_id):
        if self.busy:
            return
        group = self.config_manager.ecommerce_store().get_group(group_id)
        if not group:
            self._notice(tr('当前生成组不存在'))
            return
        if group['status'] not in RESUMABLE_STATUSES:
            self._notice(tr('当前组无需断点恢复'))
            return
        self._view_history_group(group_id)
        self._secrets = (self.config_manager.config.get('api', {}).get('api_key', ''),)
        self.summary_label.clear()
        self.stage_label.setText(tr('图片生成中'))
        self._append_log(tr('断点恢复'), 'info')
        self._launch_worker(dict(group_id=group_id), operation='resume')

    def _open_folder(self, output_dir):
        try:
            exists = bool(output_dir) and Path(output_dir).is_dir()
        except (OSError, ValueError):
            exists = False
        if not exists:
            self._notice(tr('输出文件夹不存在'))
            return
        open_local(output_dir, self._append_log, folder=True)

    def _open_history_folder(self, group_id):
        group = self.config_manager.ecommerce_store().get_group(group_id)
        if group:
            self._open_folder(group.get('output_dir'))
        else:
            self._notice(tr('当前生成组不存在'))

    def _confirm_delete(self, group):
        dialog = StudioDialog(self, tr('删除记录'))
        dialog.resize(px(440), px(220))
        dialog.body_layout.addWidget(self._text_label(
            self._clean(group.get('product_name') or group['group_id'][:8])))
        dialog.body_layout.addWidget(self._text_label(tr('仅删除记录，不删除已生成文件')))
        actions = WrapLayout()
        yes, no = PrimaryPushButton(tr('是')), PushButton(tr('否'))
        yes.setObjectName('confirmDeleteRecord')
        no.setObjectName('keepRecord')
        yes.clicked.connect(dialog.accept)
        no.clicked.connect(dialog.reject)
        actions.addWidget(yes)
        actions.addWidget(no)
        dialog.body_layout.addLayout(actions)
        style_controls(dialog)
        try:
            return dialog.exec_() == dialog.Accepted
        finally:
            safe_delete_dialog(dialog)

    def _delete_history_group(self, group_id):
        if self.busy:
            return
        store = self.config_manager.ecommerce_store()
        group = store.get_group(group_id)
        if group and self._confirm_delete(group) and not self.busy:
            store.delete_group(group_id)
            if self._group and self._group['group_id'] == group_id:
                self._clear_results()
        self._refresh_history()

    def _run_regenerate(self, item_keys=None):
        if self.busy:
            return
        try:
            store = self.config_manager.ecommerce_store()
            group = store.get_group(self._group['group_id']) if self._group else None
            if not group:
                raise ValueError(tr('当前生成组不存在'))
            items = store.list_items(group['group_id'])
            by_id = {item['item_id']: item for item in items}
            if item_keys is None:
                keys = [item['item_id'] for item in items if item['status'] == 'failed']
                if not keys:
                    raise ValueError(tr('没有需要重生成的失败项'))
            else:
                keys = []
                for key in item_keys:
                    if isinstance(key, tuple) and len(key) == 2:
                        kind, seq = key
                        if kind not in ('carousel', 'detail') or type(seq) is not int:
                            raise ValueError(tr('请选择当前组中的图片'))
                        key = f'{group["group_id"]}:{kind}:{seq}'
                    if not isinstance(key, str) or key not in by_id:
                        raise ValueError(tr('请选择当前组中的图片'))
                    if key not in keys:
                        keys.append(key)
                if not keys:
                    raise ValueError(tr('请选择当前组中的图片'))
        except (ValueError, TypeError) as error:
            self._notice(str(error))
            return
        self._secrets = (self.config_manager.config.get('api', {}).get('api_key', ''),)
        self.summary_label.clear()
        self.stage_label.setText(tr('图片生成中'))
        self._launch_worker(dict(group_id=group['group_id'], item_keys=keys), operation='regenerate')

    def _launch_worker(self, arguments, *, operation='start'):
        worker = EcommerceWorker(self._make_manager, arguments, self.cancel_event, self._secrets,
                                 operation=operation)
        self._worker = worker
        _LIVE_WORKERS.add(worker)
        worker.group_changed.connect(self._on_group, Qt.QueuedConnection)
        worker.item_changed.connect(self._on_item, Qt.QueuedConnection)
        worker.log_message.connect(self._append_log, Qt.QueuedConnection)
        worker.finished.connect(self._on_result, Qt.QueuedConnection)
        native_finished = QThread.finished.__get__(worker, QThread)
        native_finished.connect(self._thread_stopped, Qt.QueuedConnection)
        native_finished.connect(lambda: _LIVE_WORKERS.discard(worker))
        native_finished.connect(worker.deleteLater)
        for card in self._form_cards:
            card.setEnabled(False)
        self.start_button.setEnabled(False)
        self.result_panel.set_busy(True)
        self.history_panel.set_busy(True)
        self.cancel_button.setEnabled(True)
        worker.start()

    def cancel_generation(self):
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self._append_log(tr('已请求取消；未开始的图片将跳过，等待已发出的请求结束。'), 'warning')

    @pyqtSlot(object)
    def _on_group(self, record):
        self._group = redact_structure(record, self._clean)
        status = self._group['status']
        self.stage_label.setText(_stage_text(status))
        if status not in ('analyzing', 'generating'):
            totals = self._group.get('totals', {})
            self.summary_label.setText(tr('成功 {success} / 失败 {failed} / 跳过 {skipped}').format(
                success=totals.get('succeeded', 0), failed=totals.get('failed', 0), skipped=totals.get('skipped', 0)))
            self.open_output_button.setEnabled(bool(self._group.get('output_dir')))
            for item in self.config_manager.ecommerce_store().list_items(self._group['group_id']):
                self._on_item(item)
        if self._group.get('error'):
            self._append_log(self._group['error'], 'error')
        self._refresh_history()

    @pyqtSlot(object)
    def _on_item(self, record):
        record = redact_structure(record, self._clean)
        record['output_dir'] = (self._group or {}).get('output_dir', '')
        key = (record['kind'], record['seq'])
        self._items[key] = record
        if key not in self.item_labels:
            label = self._text_label()
            self.item_labels[key] = label
            self.items_layout.insertWidget(sorted(self.item_labels).index(key), label)
            label.setStyleSheet('color: %s;' % materials.palette()['text1'])
        name = tr('轮') if record['kind'] == 'carousel' else tr('详')
        parts = [f'{name}{record["seq"]}：{_item_text(record["status"])}']
        if record.get('size'):
            parts.append(record['size'])
        parts.extend(record.get('warnings', []))
        if record.get('error'):
            parts.append(record['error'])
        self.item_labels[key].setText(' · '.join(parts))
        self.result_panel.update_item(record)
        self._refresh_history()

    def _refresh_results(self, group_id=None):
        group_id = group_id or (self._group or {}).get('group_id')
        if group_id:
            store = self.config_manager.ecommerce_store()
            group = store.get_group(group_id)
            if group:
                self._on_group(group)
                if group['status'] in ('analyzing', 'generating'):
                    for item in store.list_items(group_id):
                        self._on_item(item)

    @pyqtSlot(str, str)
    def _append_log(self, message, level='info'):
        message = self._clean(message)
        level = level if level in ('info', 'warning', 'error') else 'info'
        self._logs.append((message, level))
        self._logs = self._logs[-1000:]
        self._insert_log(message, level)

    def _insert_log(self, message, level):
        cursor = self.log_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        format_ = QTextCharFormat()
        format_.setForeground(QColor(materials.palette()[{'info': 'text1', 'warning': 'warning', 'error': 'error'}[level]]))
        cursor.insertText(f'[{level}] {message}\n', format_)
        self.log_box.setTextCursor(cursor)
        self.log_box.ensureCursorVisible()

    @pyqtSlot(str, str)
    def _on_result(self, group_id, error):
        self._refresh_results(group_id)
        self._refresh_history()
        if error:
            self.stage_label.setText(tr('失败'))
            self._append_log(error, 'error')
            self._notice(error)
        elif self._group:
            self._append_log(self.stage_label.text() + ' · ' + self.summary_label.text(), 'info')

    @pyqtSlot()
    def _thread_stopped(self):
        self._worker = None
        self.cancel_event = threading.Event()
        for card in self._form_cards:
            card.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.result_panel.set_busy(False)
        self.history_panel.set_busy(False)
        self._refresh_conflicts()
        self.idle.emit()
        if self._close_when_idle:
            self.close()

    def open_output(self):
        if self._group and self._group.get('output_dir'):
            self._open_folder(self._group['output_dir'])

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_history()

    def closeEvent(self, event):
        if self.busy:
            self._close_when_idle = True
            self.cancel_generation()
            event.ignore()
            return
        super().closeEvent(event)
