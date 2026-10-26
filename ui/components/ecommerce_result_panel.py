"""Incremental, width-aware presentation of the current ecommerce image group."""
from pathlib import Path
from collections import OrderedDict
import json
import re

from PyQt5.QtCore import Qt, QRectF, QSize, QSignalBlocker, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget, CheckBox, PushButton, StrongBodyLabel, SwitchButton

from core.api_errors import classify_connection_error, classify_http_error
from core.i18n import tr
from .. import materials
from ..file_actions import open_local
from ..responsive import WrapLayout
from ..theme import style_controls
from ..tokens import px


_THUMBNAIL_CACHE = OrderedDict()
_THUMBNAIL_CACHE_LIMIT = 24


def _load_thumbnail_image(value):
    """Cache bounded display images, invalidating regenerated files by mtime."""
    if not value:
        return QImage()
    try:
        path = Path(value).resolve()
        key = (str(path), path.stat().st_mtime_ns)
        image = _THUMBNAIL_CACHE.get(key)
        if image is not None:
            _THUMBNAIL_CACHE.move_to_end(key)
            return image
        image = QImage.fromData(path.read_bytes())
        if image.isNull():
            return image
        if max(image.width(), image.height()) > 1600:
            image = image.scaled(1600, 1600, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # A same-name regeneration should replace its old resident image too.
        for previous in tuple(_THUMBNAIL_CACHE):
            if previous[0] == key[0]:
                del _THUMBNAIL_CACHE[previous]
        _THUMBNAIL_CACHE[key] = image
        while len(_THUMBNAIL_CACHE) > _THUMBNAIL_CACHE_LIMIT:
            _THUMBNAIL_CACHE.popitem(last=False)
        return image
    except (OSError, ValueError, TypeError):
        return QImage()


def _status_text(status):
    return {
        'pending': tr('等待'), 'running': tr('生成中'), 'succeeded': tr('成功'),
        'failed': tr('失败'), 'skipped': tr('跳过'),
    }.get(status, status)


class _Thumbnail(QLabel):
    clicked = pyqtSignal()
    render_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._adaptive = True
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self.render_requested)
        self.apply_adaptive(True)

    def apply_adaptive(self, adaptive):
        self._adaptive = bool(adaptive)
        policy = QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return max(1, width)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self._resize_timer.stop()
        self.render_requested.emit()

    def closeEvent(self, event):
        self._resize_timer.stop()
        super().closeEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class EcommerceResultCard(CardWidget):
    regenerate_requested = pyqtSignal(object)
    selection_changed = pyqtSignal()
    log_message = pyqtSignal(str, str)
    preview_requested = pyqtSignal(str)

    def __init__(self, record, parent=None, *, adaptive=True):
        super().__init__(parent)
        self.record = {}
        self._busy = False
        self._has_image = False
        self._feedback_positive = False
        self._adaptive = bool(adaptive)
        policy = QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumHeight(px(90))
        self.setMinimumWidth(0)
        self._hover = False
        self.badge_label = StrongBodyLabel()
        self.status_label = self._label()
        self.select_checkbox = CheckBox()
        self.select_checkbox.setAccessibleName(tr('选择'))
        self.select_checkbox.setToolTip(tr('选择此失败图进行重生'))
        self.select_checkbox.toggled.connect(self.selection_changed)
        self.badge_label.setParent(self)
        self.select_checkbox.setParent(self)
        self.status_label.setParent(self)
        self.thumbnail = _Thumbnail()
        self.thumbnail.setAlignment(Qt.AlignCenter)
        self.thumbnail.setTextFormat(Qt.PlainText)
        self.thumbnail.apply_adaptive(self._adaptive)
        self.thumbnail.clicked.connect(self._request_preview)
        self.thumbnail.render_requested.connect(self._render_thumbnail)
        self.thumbnail.setParent(self)
        self.size_label = self._label()
        self.error_label = self._label()
        self.warning_label = self._label()
        self.size_label.setParent(self)
        self.error_label.setParent(self)
        self.warning_label.setParent(self)
        self.regenerate_button = PushButton('↻')
        self.regenerate_button.setToolTip(tr('重生成此图'))
        self.folder_button = PushButton('↗')
        self.folder_button.setToolTip(tr('打开所在文件夹'))
        self.regenerate_button.clicked.connect(
            lambda: self.regenerate_requested.emit(self.record['item_id']))
        self.folder_button.clicked.connect(self._open_folder)
        self.regenerate_button.setParent(self)
        self.folder_button.setParent(self)
        style_controls(self)
        self.update_record(record)

    @staticmethod
    def _label():
        label = QLabel()
        label.setWordWrap(True)
        label.setTextFormat(Qt.PlainText)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        return label

    def update_record(self, record):
        self.record = dict(record)
        self.record['warnings'] = list(record.get('warnings') or [])
        kind = tr('轮') if record['kind'] == 'carousel' else tr('详')
        self.badge_label.setText(f'{kind}{record["seq"]}')
        self.status_label.setText(_status_text(record.get('status', 'pending')))
        self.status_label.setToolTip(tr('模型：{model}').format(model=record.get('model') or ''))
        failed = record.get('status') == 'failed'
        self.select_checkbox.setVisible(failed)
        if not failed:
            self.select_checkbox.setChecked(False)
        self.size_label.setText(str(record.get('size') or ''))
        error_text, error_tooltip = self._display_error(record.get('error') or '', record)
        self.error_label.setText(error_text)
        self.error_label.setToolTip(error_tooltip)
        self.error_label.setVisible(bool(self.error_label.text()))
        references = list(record.get('reference_paths') or [])
        warnings = [warning for warning in self.record['warnings']
                    if references or '参考图可能未被模型采用' not in str(warning)]
        image_tokens = record.get('image_tokens')
        self._feedback_positive = False
        if references and isinstance(image_tokens, int) and image_tokens > 0:
            warnings.append(tr('已参考 {count} 张图（image_tokens={tokens}）').format(
                count=len(references), tokens=image_tokens))
            self._feedback_positive = True
        elif references and image_tokens == 0:
            note = tr('参考图可能未被模型采用，请核对')
            if note not in warnings:
                warnings.append(note)
        elif image_tokens is None:
            warnings = [warning for warning in warnings
                        if '参考图可能未被模型采用' not in str(warning)]
        self.warning_label.setText('\n'.join(str(warning) for warning in warnings))
        self.warning_label.setVisible(bool(warnings))
        self._render_thumbnail()
        self.set_busy(self._busy)
        self.refresh_colors()
        self.setToolTip('\n'.join(filter(None, (
            self.badge_label.text(), self.size_label.text(), self.status_label.toolTip(),
            self.error_label.text(), self.warning_label.text()))))
        self._place_content()
        self.updateGeometry()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return max(px(90), width)

    def sizeHint(self):
        return QSize(px(110), px(110))

    def _place_content(self):
        width, height = self.width(), self.height()
        bar = px(22)
        side = max(1, min(width, height) - 4)
        self.thumbnail.setGeometry((width - side) // 2, (height - side) // 2, side, side)
        self.badge_label.setGeometry(px(4), px(3), px(34), px(22))
        self.status_label.setGeometry(1, max(0, height - bar - 1), max(1, width - 2), bar)
        self.status_label.setAlignment(Qt.AlignCenter)
        self.size_label.setGeometry(px(4), max(0, height - bar * 2), max(1, width - px(8)), bar)
        self.size_label.setVisible(self._hover and bool(self.size_label.text()))
        self.select_checkbox.setGeometry(max(0, width - px(30)), px(3), px(26), px(26))
        for index, button in enumerate((self.regenerate_button, self.folder_button)):
            button.setFixedSize(px(26), px(24))
            button.move(max(0, width - px(58) + index * px(28)), px(28))
            button.setVisible(self._hover)
        for index, label in enumerate((self.error_label, self.warning_label)):
            label.setGeometry(px(4), px(30 + index * 25), max(1, width - px(8)), px(24))
            label.setVisible(bool(label.text()))
        self.thumbnail.lower()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_content()

    def enterEvent(self, event):
        self._hover = True
        self._place_content()
        self._render_thumbnail()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._place_content()
        self._render_thumbnail()
        super().leaveEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        colors = materials.palette()
        status = self.record.get('status')
        color = colors['success'] if status == 'succeeded' else (
            colors['error'] if status == 'failed' else colors['accent'])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(color), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), px(8), px(8))

    def _render_thumbnail(self):
        self.thumbnail._resize_timer.stop()
        self._has_image = False
        self.thumbnail.clear()
        status = self.record.get('status', 'pending')
        if status == 'succeeded':
            path = self.record.get('file_path')
            image = _load_thumbnail_image(path)
            if not image.isNull():
                ratio = self.devicePixelRatioF()
                width, height = max(1, self.thumbnail.width()), max(1, self.thumbnail.height())
                canvas = QPixmap(round(width * ratio), round(height * ratio))
                canvas.setDevicePixelRatio(ratio)
                canvas.fill(Qt.transparent)
                painter = QPainter(canvas)
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setRenderHint(QPainter.SmoothPixmapTransform)
                clip = QPainterPath()
                clip.addRoundedRect(QRectF(0, 0, width, height), px(8), px(8))
                painter.setClipPath(clip)
                painter.fillRect(QRectF(0, 0, width, height), QColor(materials.palette()['bg2']))
                physical_width, physical_height = round(width * ratio), round(height * ratio)
                zoom = 1.04 if self._hover and self._adaptive else 1.0
                scaled = image.scaled(round(physical_width * zoom), round(physical_height * zoom),
                                      Qt.KeepAspectRatioByExpanding if self._adaptive else Qt.KeepAspectRatio,
                                      Qt.SmoothTransformation)
                if self._adaptive:
                    source = QRectF((scaled.width() - physical_width) / 2,
                                    (scaled.height() - physical_height) / 2,
                                    physical_width, physical_height)
                    painter.drawImage(QRectF(0, 0, width, height), scaled, source)
                else:
                    rendered_width, rendered_height = scaled.width() / ratio, scaled.height() / ratio
                    target = QRectF((width - rendered_width) / 2, (height - rendered_height) / 2,
                                    rendered_width, rendered_height)
                    painter.drawImage(target, scaled, QRectF(scaled.rect()))
                painter.end()
                self.thumbnail.setPixmap(canvas)
                self._has_image = True
            else:
                self.thumbnail.setText(tr('文件缺失'))
        else:
            self.thumbnail.setText(_status_text(status))
        self.thumbnail.setCursor(Qt.PointingHandCursor if self._has_image else Qt.ArrowCursor)

    @staticmethod
    def _display_error(value, record):
        raw = str(value or '')
        safe = re.sub(r'(?i)\b(?:sk-[\w-]+|vup_[\w-]+)', '[REDACTED]', raw)
        safe = re.sub(r'(?i)(bearer\s+)\S+', r'\1[REDACTED]', safe)
        safe = re.sub(r'(?i)((?:api[_ -]?key|token|authorization)\s*[=:]\s*)\S+',
                      r'\1[REDACTED]', safe)
        lowered = raw.casefold()
        if not raw:
            return '', ''
        if any(marker in lowered for marker in (
                'httpsconnectionpool', 'connectionpool', 'proxyerror', 'connectionerror',
                'sslerror', 'unexpected_eof', 'ssl_c_syscall', 'handshake', 'max retries',
                'readtimeout', 'connecttimeout', 'timed out', 'timeout')):
            return tr(classify_connection_error(raw)['message']), safe

        status = record.get('status_code') if isinstance(record, dict) else None
        if isinstance(status, str) and status.isdecimal():
            status = int(status)
        if not isinstance(status, int) or isinstance(status, bool):
            status = None
        if status is None:
            match = re.search(r'\bHTTP\s*(\d{3})\b', raw, flags=re.IGNORECASE)
            status = int(match.group(1)) if match else None
        embedded_error = re.search(r'\{\s*"error"\s*:', raw)
        payload_text = raw[embedded_error.start():] if embedded_error else raw.lstrip()
        if status is None and payload_text.startswith(('{', '[')):
            try:
                payload, _end = json.JSONDecoder().raw_decode(payload_text)
                error = payload.get('error', payload) if isinstance(payload, dict) else {}
                candidate = error.get('code') if isinstance(error, dict) else None
                if isinstance(candidate, str) and candidate.isdecimal():
                    candidate = int(candidate)
                status = candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else None
            except (TypeError, ValueError):
                status = None
            if status is None:
                status = 400
        if status is not None:
            classified = classify_http_error(status, raw, kind='image')
            if classified['category'] == 'http':
                message = tr('HTTP {code}：{message}').format(
                    code=status, message=tr('请求失败，请检查线路与参数'))
            else:
                message = tr(classified['message'])
            return message, safe
        return safe, safe

    def apply_adaptive(self, adaptive, *, update_geometry=True):
        self._adaptive = bool(adaptive)
        self.thumbnail.apply_adaptive(self._adaptive)
        self._render_thumbnail()
        if update_geometry:
            self.updateGeometry()

    def refresh_interface_scale(self):
        self.setMinimumHeight(px(90))
        self.thumbnail.apply_adaptive(self._adaptive)
        self._place_content()
        self.thumbnail._resize_timer.start()
        self.updateGeometry()

    def closeEvent(self, event):
        self.thumbnail._resize_timer.stop()
        super().closeEvent(event)

    def _request_preview(self):
        """结果图点击进灯箱预览（需求：生成结果图也要能点开看大图）。"""
        if self._has_image:
            self.preview_requested.emit(self.record.get('item_id', ''))

    def _open_image(self):
        if self._has_image:
            open_local(self.record['file_path'], self.log_message.emit, folder=False)

    def _open_folder(self):
        path = self._folder_path()
        if path:
            open_local(path, self.log_message.emit, folder=True)

    def _folder_path(self):
        path = self.record.get('file_path')
        return str(Path(path).parent) if path else self.record.get('output_dir', '')

    def set_busy(self, busy):
        self._busy = bool(busy)
        if self._busy:
            self.select_checkbox.setChecked(False)
        self.select_checkbox.setEnabled(not self._busy and self.record.get('status') == 'failed')
        self.regenerate_button.setEnabled(not self._busy)
        try:
            path = self._folder_path()
            folder_exists = bool(path and Path(path).is_dir())
        except (OSError, ValueError):
            folder_exists = False
        self.folder_button.setEnabled(folder_exists)

    def refresh_colors(self):
        colors = materials.palette()
        status = self.record.get('status')
        status_color = colors['success'] if status == 'succeeded' else (
            colors['error'] if status == 'failed' else colors['accent'])
        self.badge_label.setStyleSheet('color: %s;' % colors['text1'])
        self.status_label.setStyleSheet('color:%s;background:%s;border-radius:%dpx;' % (status_color, materials.ecommerce_color('single_card'), px(4)))
        self.setStyleSheet('EcommerceResultCard {border:2px solid %s;border-radius:%dpx;}' % (status_color, px(8)))
        self.size_label.setStyleSheet('color: %s;' % colors['text2'])
        self.error_label.setStyleSheet('color: %s;' % colors['error'])
        self.warning_label.setStyleSheet('color: %s;' % (
            colors['success'] if self._feedback_positive else colors['warning']))
        background = QColor(colors['bg2']).name()
        self.thumbnail.setStyleSheet(
            'color: %s; background: %s; border-radius: %dpx;' %
            (colors['text2'], background, px(8)))


class EcommerceResultPanel(QWidget):
    regenerate_requested = pyqtSignal(object)
    regenerate_failed_requested = pyqtSignal()
    regenerate_selected_requested = pyqtSignal(list)
    log_message = pyqtSignal(str, str)
    preview_requested = pyqtSignal(object, int)

    def __init__(self, parent=None, *, config_manager=None):
        super().__init__(parent)
        self.config_manager = config_manager
        config = getattr(config_manager, 'config', {})
        self._adaptive = bool(config.get('ecommerce', {}).get('thumbnail_adaptive', True))
        self.cards = {}
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(12))
        self.title_label = StrongBodyLabel(tr('结果'))
        layout.addWidget(self.title_label)
        self.regenerate_failed_button = PushButton(tr('重生成失败项'))
        self.regenerate_failed_button.setEnabled(False)
        self.regenerate_failed_button.clicked.connect(self.regenerate_failed_requested)
        self.regenerate_selected_button = PushButton()
        self.regenerate_selected_button.clicked.connect(
            lambda: self.regenerate_selected_requested.emit(self.selected_keys()))
        self.select_all_checkbox = CheckBox(tr('全选失败'))
        self.select_all_checkbox.toggled.connect(self._select_all_failed)
        self.adaptive_label = CaptionLabel(tr('图片自适应'))
        self.adaptive_switch = SwitchButton()
        self.adaptive_switch.setOnText('')
        self.adaptive_switch.setOffText('')
        self.adaptive_switch.setChecked(self._adaptive)
        self.adaptive_switch.checkedChanged.connect(self.set_adaptive)
        actions = WrapLayout(spacing=px(12))
        actions.addWidget(self.adaptive_label)
        actions.addWidget(self.adaptive_switch)
        actions.addWidget(self.select_all_checkbox)
        actions.addWidget(self.regenerate_selected_button)
        actions.addWidget(self.regenerate_failed_button)
        layout.addLayout(actions)
        self.grid = WrapLayout(spacing=px(12), card_width=90, max_columns=12)
        layout.addLayout(self.grid)
        style_controls(self)
        self._refresh_selection()
        self.refresh_colors()

    def _on_card_preview(self, item_id):
        records = [card.record for card in self.ordered_cards()
                   if card.record.get('status') == 'succeeded'
                   and card.record.get('file_path')]
        paths = [str(record['file_path']) for record in records]
        if not paths:
            return
        index = next((position for position, record in enumerate(records)
                      if record.get('item_id') == item_id), 0)
        self.preview_requested.emit(paths, min(index, len(paths) - 1))

    def ordered_cards(self):
        return sorted(self.cards.values(), key=lambda card: (card.record['kind'], card.record['seq']))

    def update_item(self, record):
        item_id = record['item_id']
        if item_id in self.cards:
            self.cards[item_id].update_record(record)
        else:
            card = EcommerceResultCard(record, self, adaptive=self._adaptive)
            card.regenerate_requested.connect(self.regenerate_requested)
            card.selection_changed.connect(self._refresh_selection)
            card.log_message.connect(self.log_message)
            card.preview_requested.connect(self._on_card_preview)
            self.cards[item_id] = card
            # Only the layout order changes when an item is first seen. Existing
            # cards retain their identity, pixmap and signal connections.
            while self.grid.count():
                self.grid.takeAt(0)
            for current in self.ordered_cards():
                self.grid.addWidget(current)
        self.set_busy(self._busy)
        self.grid.invalidate()
        self.updateGeometry()

    def clear(self):
        for card in self.cards.values():
            card.thumbnail._resize_timer.stop()
            card.select_checkbox.setChecked(False)
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        self.cards.clear()
        self._refresh_selection()
        self.grid.invalidate()
        self.updateGeometry()

    def set_adaptive(self, adaptive):
        adaptive = bool(adaptive)
        changed = adaptive != self._adaptive
        self._adaptive = adaptive
        blocker = QSignalBlocker(self.adaptive_switch)
        self.adaptive_switch.setChecked(adaptive)
        del blocker
        if changed and self.config_manager is not None:
            self.config_manager.update(('ecommerce', 'thumbnail_adaptive'), adaptive)
        self.grid.setEnabled(False)
        try:
            for card in self.cards.values():
                card.apply_adaptive(adaptive, update_geometry=False)
        finally:
            self.grid.setEnabled(True)
        self.grid.invalidate()
        self.updateGeometry()

    def set_busy(self, busy):
        self._busy = bool(busy)
        for card in self.cards.values():
            card.set_busy(self._busy)
        self._refresh_selection()

    def selected_keys(self):
        return [card.record['item_id'] for card in self.ordered_cards()
                if card.record.get('status') == 'failed' and card.select_checkbox.isChecked()]

    def _select_all_failed(self, checked):
        for card in self.cards.values():
            card.select_checkbox.setChecked(
                checked and not self._busy and card.record.get('status') == 'failed')
        self._refresh_selection()

    def _refresh_selection(self):
        failed = [card for card in self.cards.values() if card.record.get('status') == 'failed']
        count = len(self.selected_keys())
        blocker = QSignalBlocker(self.select_all_checkbox)
        self.select_all_checkbox.setChecked(bool(failed) and count == len(failed))
        del blocker
        self.select_all_checkbox.setEnabled(not self._busy and bool(failed))
        self.regenerate_selected_button.setText(tr('重生成选中（{count}）').format(count=count))
        self.regenerate_selected_button.setEnabled(not self._busy and count > 0)
        self.regenerate_failed_button.setEnabled(
            not self._busy and bool(failed))

    def refresh_colors(self):
        self.title_label.setStyleSheet('color: %s;' % materials.palette()['text1'])
        for card in self.cards.values():
            card.refresh_colors()

    def retranslate_ui(self):
        """语言切换：重翻标题/工具栏并按当前语言重绘卡片文案（幂等）。"""
        self.title_label.setText(tr('结果'))
        self.regenerate_failed_button.setText(tr('重生成失败项'))
        self.select_all_checkbox.setText(tr('全选失败'))
        self.adaptive_label.setText(tr('图片自适应'))
        for card in self.cards.values():
            try:
                card.update_record(card.record)
            except Exception:
                continue
        try:
            from ..i18n_helpers import retranslate_tree
            retranslate_tree(self)
        except Exception:
            pass
        self._refresh_selection()
