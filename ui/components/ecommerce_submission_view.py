"""电商图生成中的提交格与进度，全部缩略图在后台解码。"""
from PyQt5.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PyQt5.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout
from qfluentwidgets import FluentIcon as FIF, PushButton

from core.i18n import register_retranslate, tr
from .. import materials
from ..tokens import px
from . import image_async


def _label(size=12, bold=False, parent=None):
    label = QLabel(parent)
    label.setTextFormat(Qt.PlainText)
    font = label.font()
    font.setFamily(materials.ui_font_family())
    # 全局缩放器捕获 100% 字号；显式缩放钩子再提交当前尺寸。
    font.setPixelSize(size)
    font.setBold(bold)
    label.setFont(font)
    label.setMinimumWidth(0)
    return label


def _row_key(record):
    return str(record.get('kind') or 'carousel'), int(record.get('seq') or 0)


def _sort_key(key):
    return (0 if key[0] == 'carousel' else 1, key[1])


class _SubmissionThumbnail(QLabel):
    """缓存 QImage 的轻量 cover 绘制；resize 不读盘、不重新解码。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = ''
        self.has_image = False
        self._key = None
        self._image = QImage()
        self._missing = False
        self.setTextFormat(Qt.PlainText)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(0, px(72))
        # A QLabel pixmap otherwise becomes its next minimum height.  Repainting
        # at the allocated size must not feed a larger size back into the grid.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._resize_render = QTimer(self)
        self._resize_render.setSingleShot(True)
        self._resize_render.timeout.connect(self._render)
        font = self.font()
        font.setFamily(materials.ui_font_family())
        font.setPixelSize(12)
        self.setFont(font)

    def set_source(self, path):
        path = str(path or '')
        key = image_async.image_cache_key(path) if path else None
        if path == self.path and key == self._key:
            if not path:
                self._missing = True
            self._render()
            return
        self.path, self._key = path, key
        self._image = QImage()
        self.has_image = False
        self._missing = not bool(path)
        self.setToolTip(path)
        image = image_async.cached_image(key) if key else None
        if image is not None:
            self._decoded(key, path, image)
        else:
            self._render()
            if path:
                image_async.load_image_async(path, (240, 240), self._decoded)

    def _decoded(self, key, path, image):
        if path != self.path:
            return
        current_key = image_async.image_cache_key(path)
        if current_key != key:
            if current_key != self._key:
                self.set_source(path)
            return
        self._key = key
        self._image = image
        self.has_image = not image.isNull()
        self._missing = image.isNull()
        self._render()

    def _render(self):
        self.setProperty('thumbLoaded', self.has_image)
        style = 'color:%s; background:transparent;' % materials.text_color(3)
        if self.styleSheet() != style:
            self.setStyleSheet(style)
        if self._missing:
            self.setPixmap(QPixmap())
            self.setText(tr('缺失'))
            return
        self.setText('')
        width, height = max(1, self.width()), max(1, self.height())
        ratio = max(1.0, float(self.devicePixelRatioF()))
        canvas = QPixmap(round(width * ratio), round(height * ratio))
        canvas.setDevicePixelRatio(ratio)
        canvas.fill(Qt.transparent)
        painter = QPainter(canvas)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(0, 0, width, height), px(8), px(8))
            painter.setClipPath(clip)
            painter.fillRect(QRectF(0, 0, width, height),
                             QColor(materials.ecommerce_color('thumb_bg')))
            if not self._image.isNull():
                target_width, target_height = round(width * ratio), round(height * ratio)
                image = self._image.scaled(target_width, target_height,
                                           Qt.KeepAspectRatioByExpanding,
                                           Qt.SmoothTransformation)
                source = QRectF((image.width() - target_width) / 2,
                                (image.height() - target_height) / 2,
                                target_width, target_height)
                painter.drawImage(QRectF(0, 0, width, height), image, source)
                size = px(16)
                mark = QRectF(width - size - px(4), height - size - px(4), size, size)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(materials.ecommerce_color('submit_success')))
                painter.drawEllipse(mark)
                painter.setPen(QColor(materials.ecommerce_color('submit_success_text')))
                font = self.font()
                font.setPixelSize(px(11))
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(mark, Qt.AlignCenter, '✓')
            else:
                size = px(20)
                painter.setOpacity(0.45)
                painter.drawPixmap((width - size) // 2, (height - size) // 2,
                                   FIF.PHOTO.icon().pixmap(size, size))
        finally:
            painter.end()
        self.setPixmap(canvas)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_render.start(0)


class _SubmissionCell(QFrame):
    retry_requested = pyqtSignal(object)

    def __init__(self, record, index, parent=None):
        super().__init__(parent)
        self.setObjectName('ecommerceSubmitCell')
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.record = {}
        self.index = index
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(px(8), px(8), px(8), px(8))
        self._layout.setSpacing(px(4))
        self.number_label = _label(parent=self)
        self.number_label.setAlignment(Qt.AlignCenter)
        self._layout.addWidget(self.number_label)
        self.thumbnail = _SubmissionThumbnail(self)
        self._layout.addWidget(self.thumbnail, 1)
        self.status_label = _label(parent=self)
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setMinimumHeight(px(58))
        self._layout.addWidget(self.status_label, 1)
        self.retry_button = PushButton(self)
        self.retry_button.setFixedHeight(px(24))
        self.retry_button.clicked.connect(self._retry)
        self._layout.addWidget(self.retry_button, 0, Qt.AlignCenter)
        self.badge_label = _label(11, parent=self)
        self.badge_label.setFixedHeight(px(16))
        self.badge_label.setAlignment(Qt.AlignCenter)
        self._layout.addWidget(self.badge_label)
        self.update_record(record, index)

    def update_record(self, record, index=None):
        record = dict(record)
        if record == self.record and (index is None or index == self.index):
            return
        self.record = record
        self.key = _row_key(self.record)
        if index is not None:
            self.index = index
        status = str(self.record.get('status') or 'pending')
        status = status if status in ('running', 'succeeded', 'failed', 'cancelled', 'skipped', 'retried') else 'pending'
        self.setProperty('cellState', status)
        self.number_label.setText(str(self.index))
        self.thumbnail.setVisible(status == 'succeeded')
        self.status_label.setVisible(status != 'succeeded')
        self.retry_button.setVisible(status == 'failed')
        self.setCursor(Qt.PointingHandCursor if status == 'failed' else Qt.ArrowCursor)
        self.setToolTip(str(self.record.get('error') or ''))
        if status == 'succeeded':
            self.thumbnail.set_source(self.record.get('file') or self.record.get('file_path'))
        else:
            self.thumbnail.set_source('')
        self.retranslate_ui()
        self.refresh_colors()

    def _retry(self):
        if self.property('cellState') == 'failed':
            self.retry_requested.emit(self.key)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self._retry()
        super().mouseReleaseEvent(event)

    def retranslate_ui(self):
        self.badge_label.setText((tr('轮{seq}') if self.key[0] == 'carousel'
                                 else tr('详{seq}')).format(seq=self.key[1]))
        self.status_label.setText({'pending': tr('排队中'), 'running': tr('生成中'),
                                   'failed': tr('失败'), 'cancelled': tr('取消'),
                                   'skipped': tr('跳过'),
                                   'retried': tr('已重试')}.get(self.property('cellState'), ''))
        self.retry_button.setText(tr('重试'))
        self.thumbnail._render()

    def refresh_colors(self):
        state = self.property('cellState')
        background_key = {'running': 'submit_running_bg', 'failed': 'submit_failed_bg'}.get(
            state, 'submit_pending_bg')
        color = (materials.ecommerce_color('submit_failure') if state == 'failed'
                 else materials.accent_color() if state == 'running' else materials.text_color(3))
        self.setStyleSheet('QFrame#ecommerceSubmitCell { background:%s; border:1px solid %s; '
                          'border-radius:%dpx; }' % (materials.ecommerce_color(background_key),
                                                    materials.ecommerce_color('submit_border'), px(8)))
        self.number_label.setStyleSheet('color:%s; background:transparent;' % materials.text_color(3))
        self.status_label.setStyleSheet('color:%s; background:transparent;' % color)
        kind = 'carousel' if self.key[0] == 'carousel' else 'detail'
        self.badge_label.setStyleSheet('background:%s; color:%s; border-radius:%dpx;' % (
            materials.ecommerce_color('submit_%s_bg' % kind),
            materials.ecommerce_color('submit_%s_text' % kind), px(4)))
        self.thumbnail._render()

    def refresh_interface_scale(self):
        self._layout.setContentsMargins(px(8), px(8), px(8), px(8))
        self._layout.setSpacing(px(4))
        self.status_label.setMinimumHeight(px(58))
        self.thumbnail.setMinimumHeight(px(72))
        self.retry_button.setFixedHeight(px(24))
        self.badge_label.setFixedHeight(px(16))
        for label, size in ((self.number_label, 12), (self.status_label, 12),
                            (self.thumbnail, 12), (self.badge_label, 11)):
            font = label.font()
            font.setPixelSize(px(size))
            label.setFont(font)
        self.refresh_colors()


class EcommerceSubmissionView(QFrame):
    cancel_requested = pyqtSignal()
    retry_requested = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('ecommerceSubmissionView')
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.cells = {}
        self._items = {}
        self.rounds = {}
        self._round_sections = []
        self._total = self._done = self._failed = 0
        self._phase = 'generating'
        self._submitted = 0
        self.column_count = 3
        self._arranged_keys = ()
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(px(20), px(20), px(20), px(20))
        self._layout.setSpacing(px(16))
        header = QHBoxLayout()
        header.setSpacing(px(8))
        self.title_label = _label(13, True, self)
        self.progress_label = _label(parent=self)
        self.progress_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.cancel_button = PushButton(self)
        self.cancel_button.setFixedHeight(px(40))
        self.cancel_button.setStyleSheet('border-radius:%dpx;' % px(8))
        self.cancel_button.clicked.connect(self.cancel_requested)
        header.addWidget(self.title_label)
        header.addWidget(self.progress_label, 1)
        header.addWidget(self.cancel_button)
        self._layout.addLayout(header)
        self.grid_layout = QGridLayout()
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setSpacing(px(10))
        self._layout.addLayout(self.grid_layout)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self._arrange_all)
        self.retranslate_ui()
        self.refresh_colors()
        register_retranslate(self)
        materials.register_theme_callback(self.refresh_colors)

    def set_items(self, items):
        records = {_row_key(record): dict(record) for record in items if isinstance(record, dict)}
        structure_changed = set(records) != set(self.cells)
        if not structure_changed and records == self._items:
            return
        self._items = records
        self.setUpdatesEnabled(False)
        try:
            for key in tuple(self.cells):
                if key not in records:
                    cell = self.cells.pop(key)
                    self.grid_layout.removeWidget(cell)
                    cell.hide()
                    cell.deleteLater()
            for index, key in enumerate(sorted(records, key=_sort_key), 1):
                if key in self.cells:
                    cell = self.cells[key]
                    if cell.record != records[key] or cell.index != index:
                        cell.update_record(records[key], index)
                else:
                    cell = _SubmissionCell(records[key], index, self)
                    cell.retry_requested.connect(self.retry_requested)
                    self.cells[key] = cell
            if structure_changed:
                self._arrange_cells()
        finally:
            self.setUpdatesEnabled(True)
            self.update()

    def _create_round_section(self, round_no, keys, total):
        section = QFrame(self)
        section.setObjectName('ecommerceRetryRound')
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(px(8))
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = _label(13, True, section)
        progress = _label(parent=section)
        progress.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        header.addWidget(title)
        header.addWidget(progress, 1)
        section_layout.addLayout(header)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(px(10))
        section_layout.addLayout(grid)
        self._layout.addWidget(section)
        cells = {}
        records = []
        for key in sorted(keys, key=_sort_key):
            source = dict(self._items.get(key) or {'kind': key[0], 'seq': key[1]})
            source['status'] = 'pending'
            records.append(source)
        for index, key in enumerate(sorted(keys, key=_sort_key), 1):
            cell = _SubmissionCell(records[index - 1], index, section)
            cell.retry_requested.connect(self.retry_requested)
            cells[key] = cell
        data = dict(number=round_no, section=section, title=title, progress=progress,
                    grid=grid, cells=cells, keys=tuple(sorted(keys, key=_sort_key)),
                    total=max(int(total or 0), len(keys)), done=0, failed=0)
        self.rounds[round_no] = data
        self._round_sections.append(data)
        title.setText(tr('第 {n} 次重新生成').format(n=round_no))
        self._set_round_progress(data)
        self._arrange_round(data)
        self._style_round(data)
        return data

    def begin_retry_round(self, keys, total=None):
        normalized = []
        for key in keys or ():
            if isinstance(key, list):
                key = tuple(key)
            if isinstance(key, tuple) and len(key) == 2:
                key = (str(key[0]), int(key[1]))
                if key[0] in ('carousel', 'detail') and key not in normalized:
                    normalized.append(key)
        if not normalized:
            return 0
        round_no = max(self.rounds.keys(), default=0) + 1
        self._create_round_section(round_no, normalized, total or len(normalized))
        self.updateGeometry()
        return round_no

    def update_round_item(self, round_no, record):
        data = self.rounds.get(int(round_no))
        if not data or not isinstance(record, dict):
            return
        key = _row_key(record)
        cell = data['cells'].get(key)
        if cell is None:
            return
        cell.update_record(record)
        status = str(record.get('status') or '')
        if status == 'succeeded':
            self._supersede_previous(key, keep_round=int(round_no))
        total, succeeded, failed, done = self.round_item_counts(round_no)
        data['done'] = done
        data['failed'] = failed
        self._set_round_progress(data)

    def set_round_progress(self, round_no, done, failed):
        data = self.rounds.get(int(round_no))
        if not data:
            return
        data['done'] = max(0, int(done or 0))
        data['failed'] = max(0, int(failed or 0))
        self._set_round_progress(data)

    def round_item_counts(self, round_no):
        data = self.rounds.get(int(round_no))
        if not data:
            return 0, 0, 0, 0
        records = [cell.record for cell in data['cells'].values()]
        done = sum(record.get('status') in ('succeeded', 'failed', 'cancelled', 'skipped')
                   for record in records)
        succeeded = sum(record.get('status') == 'succeeded' for record in records)
        failed = sum(record.get('status') == 'failed' for record in records)
        return len(records), succeeded, failed, done

    def _set_round_progress(self, data):
        data['progress'].setText(tr('完成 {done}/{total}（失败 {failed}）').format(
            done=data['done'], total=data['total'], failed=data['failed']))

    def _supersede_previous(self, key, keep_round=None):
        cell = self.cells.get(key)
        if cell is not None:
            cell.setProperty('superseded', True)
            cell.hide()
        for number, data in self.rounds.items():
            if keep_round is not None and number == keep_round:
                continue
            previous = data['cells'].get(key)
            if previous is not None:
                previous.setProperty('superseded', True)
                previous.hide()
        self._arrange_all()

    def _arrange_round(self, data):
        grid = data['grid']
        while grid.count():
            grid.takeAt(0)
        section = data['section']
        available = max(0, section.width() - section.layout().contentsMargins().left() -
                        section.layout().contentsMargins().right())
        spacing = px(10)
        columns = min(6, max(3, (available + spacing) // max(1, px(96) + spacing)))
        for column in range(6):
            grid.setColumnStretch(column, 1 if column < columns else 0)
            grid.setColumnMinimumWidth(column, 0)
        visible_keys = [key for key in data['keys']
                        if not data['cells'][key].property('superseded')]
        for index, key in enumerate(visible_keys):
            cell = data['cells'][key]
            grid.addWidget(cell, index // columns, index % columns)
            cell.show()
        for key in data['keys']:
            if key not in visible_keys:
                data['cells'][key].hide()
        grid.invalidate()

    def _style_round(self, data):
        data['title'].setStyleSheet('color:%s; background:transparent;' % materials.text_color(1))
        data['progress'].setStyleSheet('color:%s; background:transparent;' % materials.text_color(2))
        for cell in data['cells'].values():
            cell.refresh_colors()

    def update_item(self, record):
        if not isinstance(record, dict):
            return
        key = _row_key(record)
        self._items[key] = dict(record)
        if key in self.cells:
            self.cells[key].update_record(record)
        else:
            self.set_items(list(self._items.values()))

    def set_progress(self, total, done, failed):
        self._total = max(0, int(total or 0))
        self._done = max(0, int(done or 0))
        self._failed = max(0, int(failed or 0))
        self.progress_label.setText(tr('已完成 {done}/{total}（失败 {failed}）').format(
            done=self._done, total=self._total, failed=self._failed))
        self._phase = 'complete'

    def set_submission_progress(self, submitted, total):
        """Show prompt submission progress while keeping all plan cells visible."""
        self._phase = 'submitting'
        self._submitted = max(0, int(submitted or 0))
        self._total = max(0, int(total or 0))
        self.title_label.setText(tr('提交中'))
        self.progress_label.setText(tr('已提交 {done}/{total}').format(
            done=self._submitted, total=self._total))

    def set_generation_phase(self, total, done, failed):
        """Update the first grid header without replacing planned cells."""
        self._phase = 'generating'
        self._total = max(0, int(total or 0))
        self._done = max(0, int(done or 0))
        self._failed = max(0, int(failed or 0))
        self.title_label.setText(tr('生成中'))
        self.progress_label.setText(tr('成功 {success} / 失败 {failed} / 共 {total}').format(
            success=self._done, failed=self._failed, total=self._total))

    def clear(self):
        self._resize_timer.stop()
        self.set_items([])
        for data in self._round_sections:
            data['section'].hide()
            data['section'].deleteLater()
        self.rounds.clear()
        self._round_sections.clear()
        self._phase = 'generating'
        self._submitted = 0
        self.set_progress(0, 0, 0)

    def _arrange_cells(self):
        self._resize_timer.stop()
        margins = self._layout.contentsMargins()
        available = max(0, self.width() - margins.left() - margins.right())
        spacing = px(10)
        columns = min(6, max(3, (available + spacing) // max(1, px(96) + spacing)))
        ordered = tuple(sorted((key for key, cell in self.cells.items()
                                if not cell.property('superseded')), key=_sort_key))
        if columns == self.column_count and ordered == self._arranged_keys:
            return
        self.column_count = columns
        self._arranged_keys = ordered
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)
        for column in range(6):
            self.grid_layout.setColumnStretch(column, 1 if column < columns else 0)
            self.grid_layout.setColumnMinimumWidth(column, 0)
        for index, key in enumerate(ordered):
            self.grid_layout.addWidget(self.cells[key], index // columns, index % columns)
            self.cells[key].show()
        for key, cell in self.cells.items():
            if key not in ordered:
                cell.hide()
        self.grid_layout.invalidate()
        self.updateGeometry()

    def _arrange_all(self):
        self._arrange_cells()
        for data in self._round_sections:
            self._arrange_round(data)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_timer.start()

    def closeEvent(self, event):
        self._resize_timer.stop()
        super().closeEvent(event)

    def retranslate_ui(self):
        if self._phase == 'submitting':
            self.title_label.setText(tr('提交中'))
            self.progress_label.setText(tr('已提交 {done}/{total}').format(
                done=self._submitted, total=self._total))
        elif self._phase == 'generating':
            self.title_label.setText(tr('生成中'))
            self.progress_label.setText(tr('成功 {success} / 失败 {failed} / 共 {total}').format(
                success=self._done, failed=self._failed, total=self._total))
        else:
            self.title_label.setText(tr('生成中'))
        self.cancel_button.setText(tr('取消'))
        if self._phase not in ('submitting', 'generating'):
            self.set_progress(self._total, self._done, self._failed)
        for cell in self.cells.values():
            cell.retranslate_ui()
        for data in self._round_sections:
            data['title'].setText(tr('第 {n} 次重新生成').format(n=data['number']))
            self._set_round_progress(data)
            for cell in data['cells'].values():
                cell.retranslate_ui()

    def refresh_colors(self):
        self.setStyleSheet('QFrame#ecommerceSubmissionView { background:%s; '
                          'border:1px solid %s; border-radius:%dpx; }' % (
                              materials.ecommerce_color('single_card'),
                              materials.ecommerce_color('single_card_border'), px(12)))
        self.title_label.setStyleSheet('color:%s; background:transparent;' % materials.text_color(1))
        self.progress_label.setStyleSheet('color:%s; background:transparent;' % materials.text_color(2))
        for cell in self.cells.values():
            cell.refresh_colors()
        for data in self._round_sections:
            self._style_round(data)

    def refresh_interface_scale(self):
        self._layout.setContentsMargins(px(20), px(20), px(20), px(20))
        self._layout.setSpacing(px(16))
        self.grid_layout.setSpacing(px(10))
        self.cancel_button.setFixedHeight(px(40))
        self.cancel_button.setStyleSheet('border-radius:%dpx;' % px(8))
        for label, size in ((self.title_label, 13), (self.progress_label, 12)):
            font = label.font()
            font.setPixelSize(px(size))
            label.setFont(font)
        for cell in self.cells.values():
            cell.refresh_interface_scale()
        for data in self._round_sections:
            data['section'].layout().setSpacing(px(8))
            self._arrange_round(data)
            for cell in data['cells'].values():
                cell.refresh_interface_scale()
            self._style_round(data)
        self.refresh_colors()
        self._arrange_cells()
