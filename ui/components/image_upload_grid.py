"""图片上传区：方形缩略图、序号、删除钮和虚线添加格。

产品图与风格图可使用统一四列布局；点击预览、删除与添加由页面处理。
旧调用方仍可指定列数与固定尺寸样式。
"""
from pathlib import Path

from PyQt5.QtCore import Qt, QRectF, QSize, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPixmap, QPen
from PyQt5.QtWidgets import QLabel, QSizePolicy, QWidget

from core.i18n import tr
from .. import materials
from ..tokens import px

PRODUCT_COLUMNS = 6
PRODUCT_MAX = 6
STYLE_CELL = 80
GRID_PADDING = 8
GRID_GAP = 8


class _CircleButton(QWidget):
    """红色圆形删除钮（自绘，避免被全局 Fluent 按钮样式覆盖；hover 放大 1.15×）。"""

    clicked = pyqtSignal()

    def __init__(self, parent=None, size=24):
        super().__init__(parent)
        self._diameter = max(px(18), int(size))
        self._hover = False
        self.setFixedSize(self._diameter + px(4), self._diameter + px(4))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tr('删除'))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        diameter = self._diameter + px(3) if self._hover else self._diameter
        offset = (self.width() - diameter) / 2
        rect = QRectF(offset, offset, diameter, diameter)
        fill = QColor(materials.ECOMMERCE['danger_hover'] if self._hover
                      else materials.ECOMMERCE['danger'])
        painter.setPen(QPen(QColor(materials.ECOMMERCE['white']),
                            max(1.0, diameter / 12)))
        painter.setBrush(fill)
        painter.drawEllipse(rect)
        pen = QPen(QColor(materials.ECOMMERCE['white']), max(1.2, diameter / 12))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        inset = diameter * 0.3
        painter.drawLine(QRectF(offset + inset, offset + inset, 0, 0).topLeft(),
                         QRectF(offset + diameter - inset, offset + diameter - inset,
                                0, 0).topLeft())
        painter.drawLine(QRectF(offset + diameter - inset, offset + inset, 0, 0).topLeft(),
                         QRectF(offset + inset, offset + diameter - inset, 0, 0).topLeft())
        painter.end()
        super().paintEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _ImageTile(QWidget):
    """单张图片格：圆角 cover 缩略图 + 右下序号 + 右上删除钮。"""

    clicked = pyqtSignal(int)
    removed = pyqtSignal(int)

    def __init__(self, path, index, cell, parent=None):
        super().__init__(parent)
        self.path = str(path)
        self.index = int(index)
        self._cell = max(1, int(cell))
        self._hover = False
        self._pixmap = QPixmap()
        self._load()
        self.setFixedSize(self._cell, self._cell)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(self.path)
        self.remove_button = _CircleButton(self, size=px(24))
        self.remove_button.clicked.connect(lambda: self.removed.emit(self.index))
        self.number_label = QLabel(str(self.index + 1), self)
        self.number_label.setAlignment(Qt.AlignCenter)
        self.number_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        metrics = QFontMetrics(self.font())
        badge_width = metrics.horizontalAdvance('99') + px(12)
        badge_height = metrics.height() + px(4)
        self.number_label.setFixedSize(max(px(16), badge_width), max(px(14), badge_height))
        self.number_label.setStyleSheet(
            'background: %s; color: %s; border-radius: %dpx;'
            ' font-size: %dpx; font-weight: 600;'
            % (materials.rgba(materials.ECOMMERCE['badge_bg']),
               materials.ECOMMERCE['badge_text'], px(8), px(9)))
        self._place_overlays()

    def set_path(self, path):
        self.path = str(path)
        self.setToolTip(self.path)
        self._load()
        self.update()

    def _load(self):
        image = QPixmap()
        try:
            if Path(self.path).is_file():
                image.loadFromData(Path(self.path).read_bytes())
        except (OSError, ValueError):
            image = QPixmap()
        self._pixmap = image

    def _place_overlays(self):
        margin = max(px(3), self._cell // 30)
        offset = max(px(3), self._cell // 40)
        button_size = self.remove_button.size()
        self.remove_button.move(
            max(0, self.width() - button_size.width() - margin), margin)
        self.number_label.move(
            offset if getattr(self.parentWidget(), 'uniform', False) else max(0, self.width() - self.number_label.width() - offset),
            max(0, self.height() - self.number_label.height() - offset))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        radius = px(6)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1),
                            radius, radius)
        colors = materials.palette()
        painter.setClipPath(clip)
        if self._pixmap.isNull():
            painter.fillPath(clip, QColor(colors['bg2']))
            painter.setPen(QColor(colors['text3']))
            painter.drawText(self.rect(), Qt.AlignCenter, tr('无法预览'))
        else:
            size = self.size()
            scaled = self._pixmap.scaled(size, Qt.KeepAspectRatio if getattr(self.parentWidget(), 'uniform', False) else Qt.KeepAspectRatioByExpanding,
                                         Qt.SmoothTransformation)
            left = max(0, (scaled.width() - size.width()) // 2)
            top = max(0, (scaled.height() - size.height()) // 2)
            if getattr(self.parentWidget(), 'uniform', False):
                painter.fillPath(clip, QColor(colors['bg2']))
                painter.drawPixmap((size.width() - scaled.width()) // 2,
                                   (size.height() - scaled.height()) // 2, scaled)
            else:
                painter.drawPixmap(0, 0, scaled, left, top, size.width(), size.height())
        painter.setClipping(False)
        pen = QPen(QColor(materials.ECOMMERCE['brand_blue'])
                   if self._hover else QColor(*colors['border']))
        pen.setWidthF(2.0 if self._hover else 1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2),
                                radius, radius)
        painter.end()
        super().paintEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_overlays()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.LeftButton and self.rect().contains(event.pos())
                and not self.remove_button.geometry().contains(event.pos())):
            self.clicked.emit(self.index)
        super().mouseReleaseEvent(event)


class _AddTile(QWidget):
    """虚线添加格：产品图显示「继续添加 n/6」，风格图显示「+ 上传参考图」。"""

    clicked = pyqtSignal()

    def __init__(self, label, *, cell=STYLE_CELL, fixed=False, parent=None):
        super().__init__(parent)
        self._label = str(label)
        self._hover = False
        self._fixed = bool(fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(self._label)
        if fixed:
            self.setFixedSize(px(cell), px(cell))
        self.plus_label = QLabel('+', self)
        self.plus_label.setAlignment(Qt.AlignCenter)
        self.plus_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.text_label = QLabel(self._label, self)
        self.text_label.setAlignment(Qt.AlignCenter)
        self.text_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.text_label.setWordWrap(True)
        self._style_labels()

    def _style_labels(self):
        self.plus_label.setStyleSheet('background: transparent; border: 0;')
        self.text_label.setStyleSheet('background: transparent; border: 0;')
        font = self.plus_label.font()
        font.setPixelSize(px(20))
        self.plus_label.setFont(font)
        font = self.text_label.font()
        font.setPixelSize(px(11))
        self.text_label.setFont(font)

    def set_label(self, label):
        self._label = str(label)
        self.text_label.setText(self._label)
        self.setToolTip(self._label)

    def _place_labels(self):
        width = max(1, self.width())
        plus_height = min(self.height(), px(26))
        self.plus_label.setGeometry(0, max(0, self.height() // 2 - plus_height - px(2)),
                                    width, plus_height)
        text_height = max(px(16), self.height() // 3)
        self.text_label.setGeometry(
            max(0, width // 10), self.height() // 2 + px(2),
            max(1, width - width // 5), text_height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_labels()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        colors = materials.palette()
        if self._hover:
            painter.fillRect(self.rect(), QColor(*materials.ECOMMERCE['upload_tint']))
        pen = QPen(QColor(materials.ECOMMERCE['brand_blue']) if self._hover
                   else QColor(*colors['border']))
        pen.setWidthF(1.5)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2),
                                px(8), px(8))
        painter.end()
        super().paintEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ImageUploadGrid(QWidget):
    """上传区网格容器。

    uniform=True：产品图与风格图使用相同的四列正方形格。
    其他调用保持原有六列产品图/80px 风格图布局。
    signals:
        changed(list[str])          当前路径顺序变化
        preview_requested(int)      点击缩略图（参数为序号）
        add_requested()             点击虚线添加格
    """

    changed = pyqtSignal(object)
    preview_requested = pyqtSignal(int)
    add_requested = pyqtSignal()

    def __init__(self, variant='product', max_count=PRODUCT_MAX, parent=None,
                 add_label_provider=None, columns=PRODUCT_COLUMNS, uniform=False):
        super().__init__(parent)
        self.variant = 'style' if variant == 'style' else 'product'
        self.max_count = max(1, int(max_count))
        self.columns = max(1, int(columns))
        self.uniform = bool(uniform)
        # Optional page-owned label generation.  Existing callers keep the
        # built-in product/style labels when this is omitted.
        self.add_label_provider = add_label_provider
        self._paths = []
        self._tiles = []
        self._add_tile = None
        self._cell = px(STYLE_CELL)
        self.setObjectName('imageUploadGrid')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._resizing = False
        self._apply_style()

    # —— 公共 API ——
    def paths(self):
        return list(self._paths)

    def set_paths(self, paths, *, emit=True):
        paths = [str(path) for path in (paths or []) if str(path or '').strip()]
        self._paths = paths[:self.max_count]
        self._rebuild(emit=emit)
        return list(self._paths)

    def clear(self):
        self.set_paths([])

    def count(self):
        return len(self._paths)

    def tile_at(self, index):
        for tile in self._tiles:
            if getattr(tile, 'index', None) == index:
                return tile
        return None

    def add_tile(self):
        return self._add_tile

    def refresh_interface_scale(self):
        self._apply_style()
        self._rebuild(emit=False)

    def refresh_colors(self):
        self._apply_style()
        for tile in self._tiles:
            tile.update()
        if self._add_tile is not None:
            self._add_tile.update()
        self.update()

    def retranslate_ui(self):
        """语言切换：添加格/删除钮提示等动态文案按新语言重建（幂等）。"""
        self._rebuild(emit=False)

    # —— 内部 ——
    def _apply_style(self):
        colors = materials.palette()
        if self.variant == 'product' and not self.uniform:
            self.setStyleSheet(
                'QWidget#imageUploadGrid {background: transparent;'
                ' border: 1.5px dashed %s; border-radius: %dpx;}'
                % (materials.rgba(colors['border']), px(10)))
        else:
            self.setStyleSheet('QWidget#imageUploadGrid {background: transparent; border: 0;}')

    def _clear_widgets(self):
        for widget in list(self._tiles) + ([self._add_tile] if self._add_tile else []):
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()

    def _rebuild(self, emit=True):
        self._clear_widgets()
        self._tiles = []
        self._add_tile = None
        show_add = len(self._paths) < self.max_count
        if self.variant == 'style' and not self.uniform:
            for index, path in enumerate(self._paths):
                tile = _ImageTile(path, index, px(STYLE_CELL) - px(2), self)
                tile.clicked.connect(self.preview_requested)
                tile.removed.connect(self._remove_at)
                tile.show()
                self._tiles.append(tile)
            if show_add:
                self._add_tile = _AddTile(tr('+ 上传参考图'), fixed=True, parent=self)
                self._add_tile.clicked.connect(self.add_requested)
                self._add_tile.show()
        else:
            for index, path in enumerate(self._paths):
                tile = _ImageTile(path, index, self._product_cell(), self)
                tile.clicked.connect(self.preview_requested)
                tile.removed.connect(self._remove_at)
                tile.show()
                self._tiles.append(tile)
            if show_add:
                if callable(self.add_label_provider):
                    try:
                        label = str(self.add_label_provider(
                            len(self._paths), self.max_count))
                    except TypeError:
                        # Permit the convenient one-argument provider form.
                        label = str(self.add_label_provider(len(self._paths)))
                else:
                    label = (tr('点击上传产品图') if not self._paths
                             else tr('继续添加 {count}/{max}').format(
                                 count=len(self._paths), max=self.max_count))
                self._add_tile = _AddTile(label, parent=self)
                self._add_tile.clicked.connect(self.add_requested)
                self._add_tile.show()
        self._relayout()
        if emit:
            self.changed.emit(list(self._paths))

    def _product_cell(self):
        width = self.width()
        if width <= 1:
            width = px(900)
        available = max(1, width - px(GRID_PADDING) * 2)
        cell = max(px(56), (available - px(10 if self.uniform else GRID_GAP) * (self.columns - 1)) // self.columns)
        return min(px(96), cell) if self.uniform else cell

    def _relayout(self):
        if self.variant == 'style' and not self.uniform:
            x = GRID_PADDING
            widgets = list(self._tiles) + ([self._add_tile] if self._add_tile else [])
            for widget in widgets:
                widget.setFixedSize(px(STYLE_CELL), px(STYLE_CELL))
                widget.move(x, GRID_PADDING)
                x += px(STYLE_CELL) + GRID_GAP
            height = GRID_PADDING * 2 + px(STYLE_CELL) if widgets else 0
        else:
            cell = self._product_cell()
            gap = px(10 if self.uniform else GRID_GAP)
            widgets = list(self._tiles) + ([self._add_tile] if self._add_tile else [])
            for index, widget in enumerate(widgets):
                widget.setFixedSize(cell, cell)
                row, column = divmod(index, self.columns)
                widget.move(px(GRID_PADDING) + column * (cell + gap),
                            px(GRID_PADDING) + row * (cell + gap))
            rows = ((len(widgets) + self.columns - 1) // self.columns
                    if widgets else 0)
            height = (px(GRID_PADDING) * 2 + rows * cell + max(0, rows - 1) * gap
                      if widgets else 0)
        if self.minimumHeight() != height:
            self.setMinimumHeight(height)
        if self.width() > 1 and self.height() != height:
            self._resizing = True
            try:
                self.resize(self.width(), height)
            finally:
                self._resizing = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, '_resizing', False):
            return
        self._resizing = True
        try:
            self._relayout()
        finally:
            self._resizing = False

    def sizeHint(self):
        return QSize(max(px(260), self.width()), max(0, self.minimumHeight()))

    def _remove_at(self, index):
        if 0 <= int(index) < len(self._paths):
            self._paths.pop(int(index))
            self._rebuild()
