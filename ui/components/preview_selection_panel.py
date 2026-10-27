"""平台预览中的本地勾选与排序；缩略图后台解码，不写任务数据。"""
import json

from PyQt5.QtCore import QEvent, QMimeData, QPoint, QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QDrag, QImage, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (
    QApplication, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from core.i18n import register_retranslate, tr
from .. import materials
from ..tokens import px
from . import image_async


PREVIEW_IMAGE_MIME = 'application/x-yanlin-preview-image'
_KINDS = ('carousel', 'detail')


def _color(key):
    value = materials.ecommerce_color(key)
    if value.startswith('rgba('):
        red, green, blue, alpha = value[5:-1].split(',')
        return QColor(int(red), int(green), int(blue), round(float(alpha) * 255))
    return QColor(value)


def _label(size, parent, *, bold=False, wrap=False):
    label = QLabel(parent)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(wrap)
    label.setMinimumWidth(0)
    font = label.font()
    font.setFamily(materials.ui_font_family())
    # 全局缩放器从构造期的 100% 度量采样；此处不可预乘当前倍率。
    font.setPixelSize(size)
    font.setBold(bold)
    label.setFont(font)
    return label


class _SelectionTile(QWidget):
    """单图的勾选状态与同一分组内的拖放交互。"""

    def __init__(self, panel, kind, record, index):
        super().__init__(panel)
        self.panel, self.kind, self.record, self.index = panel, kind, record, index
        self.path, self.seq = record['path'], record['seq']
        self.checked = True
        self.has_image = False
        self._image = QImage()
        self._missing = not bool(self.path)
        self._press_point = None
        self._insert_line = False
        self._side = px(72)
        self.setObjectName('previewSelectionTile')
        self.setProperty('imageSelected', True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(self.path)
        self.badge_label = _label(10, self)
        self.badge_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.badge_label.installEventFilter(self)
        self.missing_label = _label(11, self)
        self.missing_label.setAlignment(Qt.AlignCenter)
        self.missing_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.retranslate_ui()
        self.refresh_colors()
        if self.path:
            image_async.load_image_async(self.path, (240, 240), self._decoded)

    def _decoded(self, key, path, image):
        if path != self.path:
            return
        self._image = image
        self.has_image = not image.isNull()
        self._missing = image.isNull()
        self.setProperty('thumbLoaded', self.has_image)
        self.missing_label.setVisible(self._missing)
        self.update()

    def set_checked(self, checked):
        self.checked = bool(checked)
        self.setProperty('imageSelected', self.checked)
        self.update()

    def set_side(self, side):
        if self._side != side:
            self._side = side
            self.updateGeometry()

    def sizeHint(self):
        # 随可用宽度计算的尺寸提示不进入缩放器的固定尺寸基线。
        return QSize(self._side, self._side)

    def retranslate_ui(self):
        self.badge_label.setText((tr('轮{seq}') if self.kind == 'carousel'
                                  else tr('详{seq}')).format(seq=self.seq))
        self.missing_label.setText(tr('缺失'))
        self.missing_label.setVisible(self._missing)
        self._place_labels()

    def refresh_colors(self):
        self.badge_label.setStyleSheet('background:%s; color:%s; border-radius:%dpx; '
                                       'padding:%dpx %dpx;' % (
                                           materials.ecommerce_color('preview_badge_bg'),
                                           materials.ecommerce_color('preview_white'),
                                           3, 2, 3))
        self.missing_label.setStyleSheet('background:transparent; color:%s;' %
                                         materials.ecommerce_color('preview_muted'))
        self.update()

    def refresh_interface_scale(self):
        for label, size in ((self.badge_label, 10), (self.missing_label, 11)):
            font = label.font()
            font.setPixelSize(px(size))
            label.setFont(font)
        self.refresh_colors()
        self._place_labels()

    def _place_labels(self):
        self.badge_label.adjustSize()
        self.badge_label.move(px(3), px(3))
        self.missing_label.setGeometry(self.rect())
        self.badge_label.raise_()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            rect = QRectF(self.rect())
            clip = QPainterPath()
            clip.addRoundedRect(rect, px(6), px(6))
            painter.setClipPath(clip)
            painter.fillRect(rect, _color('preview_input'))
            if self.has_image:
                side = min(self._image.width(), self._image.height())
                source = QRectF((self._image.width() - side) / 2,
                                (self._image.height() - side) / 2, side, side)
                painter.drawImage(rect, self._image, source)
            size = px(18)
            check = QRectF(self.width() - size - px(4),
                           self.height() - size - px(4), size, size)
            painter.setBrush(_color('preview_check_bg' if self.checked else 'preview_unchecked_bg'))
            painter.setPen(Qt.NoPen if self.checked else QPen(
                QColor(materials.ecommerce_color('preview_white')), max(1, px(1))))
            painter.drawEllipse(check)
            if self.checked:
                font = self.font()
                font.setFamily(materials.ui_font_family())
                font.setPixelSize(px(12))
                painter.setFont(font)
                painter.setPen(QColor(materials.ecommerce_color('preview_white')))
                painter.drawText(check, Qt.AlignCenter, '✓')
            if self._insert_line:
                painter.setPen(QPen(QColor(materials.ecommerce_color('preview_check_bg')),
                                    max(2, px(2))))
                painter.drawLine(px(1), 0, px(1), self.height())
        finally:
            painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_labels()

    def eventFilter(self, watched, event):
        if (watched is self.badge_label
                and event.type() in (QEvent.FontChange, QEvent.StyleChange)):
            self._place_labels()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_point = QPoint(event.pos())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._press_point is not None and event.buttons() & Qt.LeftButton
                and (event.pos() - self._press_point).manhattanLength()
                >= QApplication.startDragDistance()):
            mime = QMimeData()
            mime.setData(PREVIEW_IMAGE_MIME,
                         json.dumps({'kind': self.kind, 'index': self.index}).encode('utf-8'))
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(event.pos())
            self._press_point = None
            drag.exec_(Qt.MoveAction)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        click = (event.button() == Qt.LeftButton and self._press_point is not None
                 and self.rect().contains(event.pos()))
        self._press_point = None
        if click:
            self.panel.toggle_image(self.kind, self.index)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.panel.toggle_image(self.kind, self.index)
            event.accept()
        else:
            super().keyPressEvent(event)

    def _drag_record(self, event):
        source = event.source()
        if (not isinstance(source, _SelectionTile) or source.panel is not self.panel
                or source.kind != self.kind or not event.mimeData().hasFormat(PREVIEW_IMAGE_MIME)):
            return None
        try:
            data = bytes(event.mimeData().data(PREVIEW_IMAGE_MIME))
            if len(data) > 256:
                return None
            payload = json.loads(data.decode('utf-8'))
            if not isinstance(payload, dict):
                return None
            index = payload.get('index')
            if (payload.get('kind') != self.kind or type(index) is not int
                    or not 0 <= index < len(self.panel.tiles[self.kind])
                    or self.panel.tiles[self.kind][index] is not source):
                return None
            return index
        except (ValueError, TypeError, UnicodeError):
            return None

    def dragEnterEvent(self, event):
        if self._drag_record(event) is not None:
            self._insert_line = True
            self.update()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._drag_record(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._insert_line = False
        self.update()
        event.accept()

    def dropEvent(self, event):
        index = self._drag_record(event)
        self._insert_line = False
        self.update()
        if index is None:
            event.ignore()
            return
        self.panel.move_image(self.kind, index, self.index)
        event.acceptProposedAction()


class PreviewSelectionPanel(QWidget):
    """预览会话独立的选中项；路径重复也按各自原始位置独立选择。"""

    selection_changed = pyqtSignal()

    def __init__(self, carousel, detail, parent=None):
        super().__init__(parent)
        self.setObjectName('previewSelectionPanel')
        self.setProperty('studioManagedScale', True)
        self.setMinimumWidth(0)
        self.columns = 3
        self.tiles, self.groups = {}, {}
        self.title_labels, self.clear_buttons, self.empty_labels = {}, {}, {}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setMinimumWidth(0)
        self.scroll_area.viewport().installEventFilter(self)
        self._content = QWidget(self.scroll_area)
        self._content.setMinimumWidth(0)
        self._content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(px(16), px(16), px(16), px(8))
        self._content_layout.setSpacing(px(20))
        for kind, images in zip(_KINDS, (carousel, detail)):
            self._add_group(kind, images)
        self._content_layout.addStretch(1)
        self.scroll_area.setWidget(self._content)
        self._layout.addWidget(self.scroll_area, 1)
        self.hint_label = _label(11, self)
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setFixedHeight(36)
        self._layout.addWidget(self.hint_label)
        self.retranslate_ui()
        self.refresh_colors()
        for child in self.findChildren(QWidget):
            child.setProperty('studioManagedScale', True)
        self.refresh_interface_scale()
        register_retranslate(self)

    def _add_group(self, kind, images):
        section = QWidget(self._content)
        section.setMinimumWidth(0)
        vertical = QVBoxLayout(section)
        vertical.setContentsMargins(0, 0, 0, 0)
        vertical.setSpacing(px(12))
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        title = _label(13, section, bold=True)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        clear_button = QPushButton(section)
        clear_button.setFixedHeight(px(26))
        font = clear_button.font()
        font.setFamily(materials.ui_font_family())
        font.setPixelSize(11)
        clear_button.setFont(font)
        clear_button.setCursor(Qt.PointingHandCursor)
        clear_button.clicked.connect(lambda checked=False, value=kind: self.clear_selection(value))
        header.addWidget(title, 1)
        header.addWidget(clear_button)
        vertical.addLayout(header)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(px(8))
        grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        vertical.addLayout(grid)
        empty = _label(12, section, wrap=True)
        empty.setAlignment(Qt.AlignCenter)
        empty.setMinimumHeight(px(48))
        vertical.addWidget(empty)
        tiles = []
        for index, value in enumerate(images or ()):
            value = value if isinstance(value, dict) else {'path': value}
            seq = value.get('seq')
            seq = seq if type(seq) is int and seq > 0 else index + 1
            record = {'path': str(value.get('file') or value.get('path') or ''),
                      'seq': seq}
            tile = _SelectionTile(self, kind, record, index)
            tiles.append(tile)
            grid.addWidget(tile, index // self.columns, index % self.columns)
        self.tiles[kind] = tiles
        self.title_labels[kind] = title
        self.clear_buttons[kind] = clear_button
        self.empty_labels[kind] = empty
        self.groups[kind] = {'widget': section, 'layout': vertical, 'grid': grid,
                             'tiles': tiles, 'title_label': title,
                             'clear_button': clear_button, 'empty_label': empty}
        empty.setVisible(not tiles)
        clear_button.setEnabled(bool(tiles))
        self._content_layout.addWidget(section)

    def selected_images(self, kind):
        return [tile.path for tile in self.tiles.get(kind, ()) if tile.checked]

    def selected_count(self, kind):
        return sum(tile.checked for tile in self.tiles.get(kind, ()))

    def toggle_image(self, kind, index):
        tiles = self.tiles.get(kind, ())
        if type(index) is not int or not 0 <= index < len(tiles):
            return
        tile = tiles[index]
        tile.set_checked(not tile.checked)
        self.selection_changed.emit()

    def clear_selection(self, kind):
        changed = False
        for tile in self.tiles.get(kind, ()):
            changed = changed or tile.checked
            tile.set_checked(False)
        if changed:
            self.selection_changed.emit()

    def move_image(self, kind, from_index, to_index):
        tiles = self.tiles.get(kind, ())
        if (type(from_index) is not int or type(to_index) is not int
                or not 0 <= from_index < len(tiles) or not 0 <= to_index < len(tiles)
                or from_index == to_index):
            return
        tiles.insert(to_index, tiles.pop(from_index))
        self._reflow()
        self.selection_changed.emit()

    def set_columns(self, columns):
        columns = 2 if int(columns) == 2 else 3
        if columns != self.columns:
            self.columns = columns
            self._reflow()
        else:
            self._fit_tiles()

    def _reflow(self):
        for kind, group in self.groups.items():
            grid = group['grid']
            while grid.count():
                grid.takeAt(0)
            for index, tile in enumerate(self.tiles[kind]):
                tile.index = index
                grid.addWidget(tile, index // self.columns, index % self.columns)
        self._fit_tiles()

    def _fit_tiles(self):
        available = max(0, self.scroll_area.viewport().width() - 2 * px(16))
        side = max(1, min(px(72), (available - (self.columns - 1) * px(8)) // self.columns))
        for tiles in self.tiles.values():
            for tile in tiles:
                tile.set_side(side)

    def eventFilter(self, watched, event):
        if watched is self.scroll_area.viewport() and event.type() == QEvent.Resize:
            self._fit_tiles()
        return super().eventFilter(watched, event)

    def retranslate_ui(self):
        self.title_labels['carousel'].setText(tr('轮播图'))
        self.title_labels['detail'].setText(tr('详情图'))
        self.hint_label.setText(tr('点击勾选 · 拖拽排序'))
        for kind in _KINDS:
            self.clear_buttons[kind].setText(tr('取消全选'))
            self.empty_labels[kind].setText(tr('暂无生成图片'))
            for tile in self.tiles[kind]:
                tile.retranslate_ui()

    def refresh_colors(self):
        background = materials.ecommerce_color('preview_sidebar_bg')
        self.setStyleSheet('QWidget#previewSelectionPanel {background:%s;}' % background)
        self._content.setStyleSheet('background:%s;' % background)
        self.scroll_area.setStyleSheet('QScrollArea {background:%s; border:none;}' % background)
        self.hint_label.setStyleSheet('background:%s; color:%s;' % (
            background, materials.ecommerce_color('preview_hint')))
        for kind in _KINDS:
            self.title_labels[kind].setStyleSheet('background:transparent; color:%s;' %
                                                 materials.ecommerce_color('preview_sidebar_text'))
            self.empty_labels[kind].setStyleSheet('background:transparent; color:%s;' %
                                                 materials.ecommerce_color('preview_hint'))
            button = self.clear_buttons[kind]
            button.setStyleSheet('QPushButton {color:%s; background:%s; border:1px solid %s; '
                                 'border-radius:%dpx; padding:0 %dpx;} '
                                 'QPushButton:hover {background:%s;}' % (
                                     materials.ecommerce_color('preview_sidebar_button_text'),
                                     background, materials.ecommerce_color('preview_sidebar_border'),
                                     6, 7, materials.ecommerce_color('preview_input')))
            for tile in self.tiles[kind]:
                tile.refresh_colors()

    def refresh_interface_scale(self):
        self._content_layout.setContentsMargins(px(16), px(16), px(16), px(8))
        self._content_layout.setSpacing(px(20))
        self.hint_label.setFixedHeight(px(36))
        font = self.hint_label.font()
        font.setPixelSize(px(11))
        self.hint_label.setFont(font)
        for kind, group in self.groups.items():
            group['layout'].setSpacing(px(12))
            group['grid'].setSpacing(px(8))
            self.clear_buttons[kind].setFixedHeight(px(26))
            font = self.clear_buttons[kind].font()
            font.setPixelSize(px(11))
            self.clear_buttons[kind].setFont(font)
            for label, size in ((self.title_labels[kind], 13), (self.empty_labels[kind], 12)):
                font = label.font()
                font.setPixelSize(px(size))
                label.setFont(font)
            self.empty_labels[kind].setMinimumHeight(px(48))
            for tile in self.tiles[kind]:
                tile.refresh_interface_scale()
        self._fit_tiles()
        self.refresh_colors()
