"""Incremental, width-aware presentation of the current ecommerce image group."""
from pathlib import Path

from PyQt5.QtCore import Qt, QRectF, QSignalBlocker, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PyQt5.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CardWidget, CheckBox, PushButton, StrongBodyLabel

from core.i18n import tr
from .. import materials
from ..file_actions import open_local
from ..responsive import WrapLayout
from ..theme import style_controls
from ..tokens import px


def _status_text(status):
    return {
        'pending': tr('等待'), 'running': tr('生成中'), 'succeeded': tr('成功'),
        'failed': tr('失败'), 'skipped': tr('跳过'),
    }.get(status, status)


class _Thumbnail(QLabel):
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class EcommerceResultCard(CardWidget):
    regenerate_requested = pyqtSignal(object)
    selection_changed = pyqtSignal()
    log_message = pyqtSignal(str, str)

    def __init__(self, record, parent=None):
        super().__init__(parent)
        self.record = {}
        self._busy = False
        self._has_image = False
        self._feedback_positive = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(14), px(14), px(14), px(14))
        layout.setSpacing(px(8))
        self.badge_label = StrongBodyLabel()
        self.status_label = self._label()
        self.select_checkbox = CheckBox(tr('选择'))
        self.select_checkbox.setToolTip(tr('选择此失败图进行重生'))
        self.select_checkbox.toggled.connect(self.selection_changed)
        layout.addWidget(self.badge_label)
        layout.addWidget(self.select_checkbox)
        layout.addWidget(self.status_label)
        self.thumbnail = _Thumbnail()
        self.thumbnail.setAlignment(Qt.AlignCenter)
        self.thumbnail.setTextFormat(Qt.PlainText)
        self.thumbnail.setFixedHeight(px(180))
        self.thumbnail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.thumbnail.clicked.connect(self._open_image)
        layout.addWidget(self.thumbnail)
        self.size_label = self._label()
        self.error_label = self._label()
        self.warning_label = self._label()
        layout.addWidget(self.size_label)
        layout.addWidget(self.error_label)
        layout.addWidget(self.warning_label)
        self.regenerate_button = PushButton(tr('重生成此图'))
        self.folder_button = PushButton(tr('打开所在文件夹'))
        self.regenerate_button.clicked.connect(
            lambda: self.regenerate_requested.emit(self.record['item_id']))
        self.folder_button.clicked.connect(self._open_folder)
        layout.addWidget(self.regenerate_button)
        layout.addWidget(self.folder_button)
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
        failed = record.get('status') == 'failed'
        self.select_checkbox.setVisible(failed)
        if not failed:
            self.select_checkbox.setChecked(False)
        self.size_label.setText(str(record.get('size') or ''))
        self.error_label.setText(str(record.get('error') or ''))
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
        self.updateGeometry()

    def _render_thumbnail(self):
        self._has_image = False
        self.thumbnail.clear()
        status = self.record.get('status', 'pending')
        if status == 'succeeded':
            path = self.record.get('file_path')
            image = QPixmap()
            try:
                if path and Path(path).is_file():
                    # Regeneration replaces the same filename; bypass Qt's file
                    # cache so rapid replacements always show the latest image.
                    image.loadFromData(Path(path).read_bytes())
            except (OSError, ValueError):
                pass
            if not image.isNull():
                ratio = self.devicePixelRatioF()
                width, height = px(220), px(180)
                canvas = QPixmap(round(width * ratio), round(height * ratio))
                canvas.setDevicePixelRatio(ratio)
                canvas.fill(Qt.transparent)
                painter = QPainter(canvas)
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setRenderHint(QPainter.SmoothPixmapTransform)
                clip = QPainterPath()
                clip.addRoundedRect(QRectF(0, 0, width, height), px(8), px(8))
                painter.setClipPath(clip)
                scaled = image.scaled(round(width * ratio), round(height * ratio),
                                      Qt.KeepAspectRatio, Qt.SmoothTransformation)
                target_width, target_height = scaled.width() / ratio, scaled.height() / ratio
                painter.drawPixmap(QRectF((width - target_width) / 2, (height - target_height) / 2,
                                          target_width, target_height), scaled,
                                   QRectF(scaled.rect()))
                painter.end()
                self.thumbnail.setPixmap(canvas)
                self._has_image = True
            else:
                self.thumbnail.setText(tr('文件缺失'))
        else:
            self.thumbnail.setText(_status_text(status))
        self.thumbnail.setCursor(Qt.PointingHandCursor if self._has_image else Qt.ArrowCursor)

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
            colors['error'] if status == 'failed' else colors['text2'])
        self.badge_label.setStyleSheet('color: %s;' % colors['text1'])
        self.status_label.setStyleSheet('color: %s;' % status_color)
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

    def __init__(self, parent=None):
        super().__init__(parent)
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
        actions = WrapLayout(spacing=px(12))
        actions.addWidget(self.select_all_checkbox)
        actions.addWidget(self.regenerate_selected_button)
        actions.addWidget(self.regenerate_failed_button)
        layout.addLayout(actions)
        self.grid = WrapLayout(spacing=px(12), card_width=260, max_columns=4)
        layout.addLayout(self.grid)
        style_controls(self)
        self._refresh_selection()
        self.refresh_colors()

    def ordered_cards(self):
        return sorted(self.cards.values(), key=lambda card: (card.record['kind'], card.record['seq']))

    def update_item(self, record):
        item_id = record['item_id']
        if item_id in self.cards:
            self.cards[item_id].update_record(record)
        else:
            card = EcommerceResultCard(record, self)
            card.regenerate_requested.connect(self.regenerate_requested)
            card.selection_changed.connect(self._refresh_selection)
            card.log_message.connect(self.log_message)
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
