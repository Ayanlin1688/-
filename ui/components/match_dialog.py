"""Frameless Fluent binding editor with real-image loading and local ordering."""

from pathlib import Path
from PyQt5.QtCore import Qt, QSize, pyqtSignal
import copy
from PyQt5.QtGui import QColor, QPainter, QPixmap
from PyQt5.QtWidgets import QListWidgetItem, QSplitter, QVBoxLayout, QHBoxLayout, QWidget, QFileDialog
from .studio_dialog import StudioDialog
from qfluentwidgets import (
    CaptionLabel, ImageLabel, ListWidget, PrimaryPushButton, PushButton,
    StrongBodyLabel, TransparentToolButton, FluentIcon as FIF, Theme,
    PlainTextEdit, SegmentedWidget,
)
from ..theme import style_controls, style_page
from .model_selector import ModelComboBox, catalog_snapshot
from core.prompt_detector import annotate_tasks
from core.prompt_converter import convert_for_model
from core.task_state import parameters_for_model


class MatchDialog(StudioDialog):
    saved = pyqtSignal()

    def __init__(self, parent=None, log_callback=None, config_manager=None, matches=None, automatic_matches=None):
        super().__init__(parent, '图片与提示词匹配详情')
        self.log_callback = log_callback or (lambda *args: None)
        self.config_manager = config_manager
        self.matches = copy.deepcopy(matches or [])
        self.bindings = {t['prompt_path']: list(t['images']) for t in self.matches}
        self.automatic = {t['prompt_path']: list(t['images']) for t in automatic_matches or self.matches}
        self.dirty = set()
        self.model_overrides = dict(config_manager.config.get('model_overrides', {})) if config_manager else {}
        self.models_dirty = False
        if config_manager:
            annotate_tasks(self.matches, config_manager.config)
        self.setObjectName("matchDialog")
        self.setWindowTitle("图片-提示词匹配详情")
        self.resize(1000, 680)
        root = self.body_layout
        root.addWidget(CaptionLabel('拖动图片行调整 @参考图1/2/3 的顺序；修改后点击保存调整'))
        splitter = QSplitter(Qt.Horizontal)
        self.prompt_list = ListWidget()
        for task in self.matches:
            filename = Path(task['prompt_path']).name
            label = f'{task["product"]} / {filename}' if task.get('product') else filename
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, task['prompt_path'])
            item.setToolTip(task['prompt_path'])
            item.setIcon(FIF.DOCUMENT.icon())
            self.prompt_list.addItem(item)
        splitter.addWidget(self.prompt_list)
        detail = QWidget()
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(StrongBodyLabel("图片绑定详情"))
        self.detected_model_label = CaptionLabel('自动识别模型：未识别')
        self.detected_model_label.setWordWrap(True)
        layout.addWidget(self.detected_model_label)
        self.model_combo = ModelComboBox()
        layout.addWidget(self.model_combo)
        self.binding_summary = CaptionLabel('已绑定0张参考图')
        layout.addWidget(self.binding_summary)
        self.picture_list = ListWidget()
        self.picture_list.setDragDropMode(ListWidget.InternalMove)
        self.picture_list.setDefaultDropAction(Qt.MoveAction)
        layout.addWidget(self.picture_list, 1)
        actions = QHBoxLayout()
        add = PushButton(FIF.ADD, "添加图片")
        remove = PushButton(FIF.REMOVE, "移除")
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        actions.addWidget(add)
        actions.addWidget(remove)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addWidget(StrongBodyLabel('提示词预览'))
        self.preview_tabs = SegmentedWidget()
        self.preview_tabs.addItem('original', '原始格式')
        self.preview_tabs.addItem('converted', '转换后格式')
        layout.addWidget(self.preview_tabs)
        self.preview_status = CaptionLabel('选择提示词后显示预览')
        self.preview_status.setWordWrap(True)
        layout.addWidget(self.preview_status)
        self.prompt_preview = PlainTextEdit()
        self.prompt_preview.setReadOnly(True)
        self.prompt_preview.setMinimumHeight(130)
        layout.addWidget(self.prompt_preview, 1)
        self.preview_tabs.currentItemChanged.connect(self._show_preview_mode)
        self.preview_tabs.setCurrentItem('original')
        splitter.addWidget(detail)
        splitter.setSizes([290, 550])
        root.addWidget(splitter, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        rematch = PushButton(FIF.SYNC, "重新自动匹配")
        self.reset_models_button = PushButton(FIF.SYNC, '全部重置为自动识别')
        self.reset_models_button.clicked.connect(self.reset_models)
        save = PrimaryPushButton(FIF.SAVE, "保存调整")
        close = PushButton("关闭")
        rematch.clicked.connect(self._rematch)
        save.clicked.connect(self._save)
        close.clicked.connect(self.accept)
        for button in (self.reset_models_button, rematch, save, close):
            footer.addWidget(button)
        root.addLayout(footer)
        self.prompt_list.currentRowChanged.connect(self._show_details)
        self.picture_list.model().rowsMoved.connect(self._remember)
        self.model_combo.currentTextChanged.connect(self._model_changed)
        self.prompt_list.setCurrentRow(0)
        style_page(self, opaque_window=False)
        style_controls(self)

    def _show_details(self, row):
        self.picture_list.clear()
        self.binding_summary.setText('已绑定0张参考图')
        if row < 0:
            return
        key = self.matches[row]['prompt_path']
        task = self.matches[row]
        detected = task.get('detected_model') or '未识别（使用默认模型）'
        self.detected_model_label.setText('自动识别模型：' + detected)
        self.model_combo.set_models(catalog_snapshot(self.config_manager), self.model_overrides.get(key, ''),
                                    automatic=True, keep_missing=True)
        self.model_combo.setEnabled(bool(self.config_manager and self.config_manager.config.get('prompt_detection', {}).get('enabled', False)))
        paths = self.bindings.get(key, [])
        for path in paths:
            self._append_picture(path)
        self._refresh_picture_labels()
        self._refresh_prompt_preview()

    def _append_picture(self, path):
        item = QListWidgetItem(self.picture_list)
        item.setData(Qt.UserRole, path)
        item.setSizeHint(QSize(360, 100))
        host = QWidget()
        # All row children are display-only. Route mouse drags to the Fluent list.
        host.setAttribute(Qt.WA_TransparentForMouseEvents)
        row = QHBoxLayout(host)
        row.setContentsMargins(8, 10, 8, 10)
        pixmap = QPixmap(path)
        missing = pixmap.isNull()
        item.setData(Qt.UserRole + 1, missing)
        if pixmap.isNull():
            pixmap = QPixmap(80, 80)
            pixmap.fill(QColor("#343958"))
            painter = QPainter(pixmap)
            painter.drawPixmap(24, 24, FIF.PHOTO.icon(Theme.DARK).pixmap(32, 32))
            painter.end()
        else:
            pixmap = pixmap.scaled(80, 80, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            pixmap = pixmap.copy((pixmap.width()-80)//2, (pixmap.height()-80)//2, 80, 80)
        thumb = ImageLabel(pixmap)
        thumb.setBorderRadius(8, 8, 8, 8)
        row.addWidget(thumb)
        label = CaptionLabel('')
        label.setObjectName('pictureCaption')
        row.addWidget(label, 1)
        handle = TransparentToolButton(FIF.MOVE)
        handle.setToolTip("拖动图片行调整顺序")
        handle.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(handle)
        self.picture_list.setItemWidget(item, host)

    def _refresh_picture_labels(self):
        count = self.picture_list.count()
        self.binding_summary.setText(f'已绑定{count}张参考图 · 从上到下对应 Picture 1–{count}' if count else '已绑定0张参考图')
        for index in range(count):
            item = self.picture_list.item(index)
            host = self.picture_list.itemWidget(item)
            if host is not None:
                name = Path(item.data(Qt.UserRole)).name
                status = '文件缺失或图片不可读' if item.data(Qt.UserRole + 1) else '本地图片'
                host.findChild(CaptionLabel, 'pictureCaption').setText(f'Picture {index+1} · {name}\n{status}')

    def _remember(self, *_):
        self._refresh_picture_labels()
        item = self.prompt_list.currentItem()
        if item:
            key = item.data(Qt.UserRole)
            self.bindings[key] = [self.picture_list.item(i).data(Qt.UserRole) for i in range(self.picture_list.count())]
            self.dirty.add(key)
            self._refresh_prompt_preview()

    def _add(self):
        if self.prompt_list.currentRow() < 0:
            self._notice('请先选择目录并扫描提示词')
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "选择参考图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        for path in paths:
            self._append_picture(str(Path(path).resolve()))
        self._remember()

    def _remove(self):
        row = self.picture_list.currentRow()
        if row >= 0:
            self.picture_list.takeItem(row)
            self._remember()

    def _save(self):
        if self.config_manager:
            overrides = dict(self.config_manager.config.get('match_overrides', {}))
            for key in self.dirty:
                overrides[key] = self.bindings[key]
            try:
                if not self.config_manager.update(('match_overrides',), overrides):
                    self._notice('保存失败，请查看执行日志；本次修改仍保留在内存')
                    return
                if self.models_dirty and not self.config_manager.update(('model_overrides',), dict(self.model_overrides)):
                    self._notice('模型选择保存失败，请查看执行日志')
                    return
            except Exception as error:
                self._notice(f'保存失败：{error}')
                return
        self.saved.emit()
        self._notice("匹配调整已保存")

    def _model_changed(self, model):
        row = self.prompt_list.currentRow()
        if row < 0:
            return
        key = self.matches[row]['prompt_path']
        if model:
            self.model_overrides[key] = model
        else:
            self.model_overrides.pop(key, None)
        self.models_dirty = True
        self._refresh_prompt_preview()

    def _effective_preview_model(self, task):
        if self.config_manager:
            config = dict(self.config_manager.config, model_overrides=dict(self.model_overrides))
            current = copy.deepcopy(task)
            annotate_tasks([current], config)
            return current['requested_model']
        selected = self.model_combo.currentText()
        return selected or task.get('requested_model') or task.get('model') or ''

    def _refresh_prompt_preview(self):
        row = self.prompt_list.currentRow()
        if row < 0:
            self._preview_original = ''
            self._preview_converted = ''
            self.preview_status.setText('选择提示词后显示预览')
            self._show_preview_mode(self.preview_tabs.currentRouteKey() or 'original')
            return
        task = self.matches[row]
        try:
            original = Path(task['prompt_path']).read_text(encoding='utf-8-sig')
            model = self._effective_preview_model(task)
            catalog = catalog_snapshot(self.config_manager)
            values = self.config_manager.config.get('workspace', {}) if self.config_manager else {}
            task_specific = bool(self.model_combo.currentText()) or task.get('model_source') in {'auto', 'manual', 'fallback'}
            pooled = bool(self.config_manager and self.config_manager.config.get('model_pool', {}).get('enabled'))
            params = parameters_for_model(model, values, pooled or task_specific,
                                          len(self.bindings.get(task['prompt_path'], [])), catalog)
            enabled = True
            if self.config_manager:
                enabled = self.config_manager.config.get('prompt_conversion', {}).get('enabled', True)
            result = convert_for_model(original, model, duration=params.get('duration'),
                                       enabled=enabled, catalog=catalog)
            self._preview_original = original
            self._preview_converted = result.text
            if result.converted:
                status = f'{result.source_format} → {result.target_format}'
            elif not enabled:
                status = '自动转换已关闭'
            else:
                status = f'{result.source_format} → {result.target_format} · 无需转换'
            if result.warnings:
                status += ' · ' + '；'.join(result.warnings)
            self.preview_status.setText(status)
        except Exception as error:
            try:
                self._preview_original = Path(task['prompt_path']).read_text(encoding='utf-8-sig')
            except Exception:
                self._preview_original = ''
            self._preview_converted = f'转换失败：{error}'
            self.preview_status.setText('转换失败 · 请检查提示词结构')
        self._show_preview_mode(self.preview_tabs.currentRouteKey() or 'original')

    def _show_preview_mode(self, route_key):
        if route_key == 'converted':
            self.prompt_preview.setPlainText(getattr(self, '_preview_converted', ''))
        else:
            self.prompt_preview.setPlainText(getattr(self, '_preview_original', ''))

    def reset_models(self):
        self.model_overrides.clear()
        self.models_dirty = True
        self._show_details(self.prompt_list.currentRow())

    def _rematch(self):
        self.bindings = copy.deepcopy(self.automatic)
        self.dirty.update(self.bindings)
        self._show_details(self.prompt_list.currentRow())
        self._notice('已恢复本次扫描的自动匹配结果，点击保存调整生效')

    def _notice(self, text):
        from qfluentwidgets import InfoBar
        self.log_callback(text, "info")
        InfoBar.info("匹配详情", text, parent=self, duration=2200)
