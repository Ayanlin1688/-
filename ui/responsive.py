"""Width-aware layouts and bounded, coalesced geometry updates."""
from types import MethodType
import weakref

from PyQt5.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer
from PyQt5.QtWidgets import QLayout, QSizePolicy, QSpacerItem, QScrollArea, QVBoxLayout, QWidget

from .tokens import px


def scrollable_body(page):
    shell = QVBoxLayout(page)
    shell.setContentsMargins(0, 0, 0, 0)
    area = QScrollArea(page)
    area.setFrameShape(QScrollArea.NoFrame)
    area.setWidgetResizable(True)
    area.setStyleSheet('QScrollArea{background:transparent;border:0;}')
    content = QWidget()
    content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    area.setWidget(content)
    shell.addWidget(area)
    page.body_scroll = area
    page.body_content = content
    return QVBoxLayout(content)


class CoalescedCall(QObject):
    def __init__(self, parent, callback, interval=32):
        super().__init__(parent)
        if isinstance(callback, MethodType):
            reference = weakref.WeakMethod(callback)

            def invoke():
                current = reference()
                if current is not None:
                    current()

            self.callback = invoke
        else:
            self.callback = callback
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(interval)
        self.timer.timeout.connect(self.flush)

    def schedule(self):
        if not self.timer.isActive():
            self.timer.start()

    def flush(self):
        self.timer.stop()
        self.callback()


class ContentHeight(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.update = CoalescedCall(self, self.fit, 0)
        widget.installEventFilter(self)
        self.update.schedule()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.LayoutRequest, QEvent.Show):
            self.update.schedule()
        elif event.type() == QEvent.Resize and event.size().width() != event.oldSize().width():
            self.update.schedule()
        return False

    def fit(self):
        widget = self.parent()
        layout = widget.layout()
        if layout is not None:
            height = layout.totalHeightForWidth(widget.width()) if layout.hasHeightForWidth() else layout.sizeHint().height()
            height = max(height, layout.minimumSize().height())
            if widget.minimumHeight() != height or widget.maximumHeight() != height:
                widget.setFixedHeight(height)


class WrapLayout(QLayout):
    def __init__(self, parent=None, spacing=10, card_width=None, max_columns=4):
        super().__init__(parent)
        self.items = []
        self.card_width = card_width
        self.max_columns = max_columns
        self.columns = 0
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def __del__(self):
        while self.items:
            self.takeAt(0)

    def addItem(self, item):
        self.items.append(item)
        self.invalidate()

    def addWidget(self, widget, stretch=0, alignment=Qt.Alignment()):
        super().addWidget(widget)
        self.items[-1].setAlignment(alignment)

    def addLayout(self, layout, stretch=0):
        self.addChildLayout(layout)
        self.addItem(layout)

    def insertLayout(self, index, layout, stretch=0):
        self.addChildLayout(layout)
        self.items.insert(index, layout)
        self.invalidate()

    def addStretch(self, stretch=1):
        self.addItem(QSpacerItem(0, 0, QSizePolicy.Expanding, QSizePolicy.Minimum))

    def addSpacing(self, size):
        self.addItem(QSpacerItem(size, 0, QSizePolicy.Fixed, QSizePolicy.Minimum))

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return Qt.Horizontal

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), False)

    def minimumSize(self):
        size = QSize()
        for item in self._visible_items():
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def sizeHint(self):
        return self.minimumSize()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _visible_items(self):
        return [item for item in self.items if item.widget() is None or not item.widget().isHidden()]

    def _arrange(self, rect, apply):
        margins = self.contentsMargins()
        width = max(1, rect.width() - margins.left() - margins.right())
        spacing = max(0, self.spacing())
        items = self._visible_items()
        rows = []
        if self.card_width is not None:
            columns = min(self.max_columns, max(1, (width + spacing) // (px(self.card_width) + spacing)))
            if len(items) == 4 and columns == 3:
                columns = 2
            if apply:
                self.columns = columns
            rows = [items[index:index + columns] for index in range(0, len(items), columns)]
        else:
            row, used = [], 0
            for item in items:
                wanted = self._item_width(item, width)
                if row and used + spacing + wanted > width:
                    rows.append(row)
                    row, used = [], 0
                used += (spacing if row else 0) + wanted
                row.append(item)
            if row:
                rows.append(row)
        top = rect.y() + margins.top()
        for row in rows:
            widths = [self._item_width(item, width) for item in row]
            remaining = max(0, width - sum(widths) - spacing * (len(row) - 1))
            flexible = list(range(len(row))) if self.card_width else [index for index, item in enumerate(row)
                         if item.expandingDirections() & Qt.Horizontal or
                         (item.widget() is not None and item.widget().sizePolicy().horizontalPolicy() == QSizePolicy.Ignored)]
            if self.card_width:
                widths = [(width - spacing * (len(row) - 1)) // len(row)] * len(row)
                remaining = width - sum(widths) - spacing * (len(row) - 1)
            if flexible:
                for offset, index in enumerate(flexible):
                    widths[index] += remaining // len(flexible) + int(offset < remaining % len(flexible))
            heights = [item.heightForWidth(item_width) if item.hasHeightForWidth() else item.sizeHint().height()
                       for item, item_width in zip(row, widths)]
            height = max([0] + heights)
            if self.card_width:
                heights = [height] * len(row)
            left = rect.x() + margins.left()
            for item, item_width, item_height in zip(row, widths, heights):
                if apply:
                    item.setGeometry(QRect(left, top + (height - item_height) // 2, item_width, item_height))
                left += item_width + spacing
            top += height + spacing
        return top - rect.y() - (spacing if rows else 0) + margins.bottom()

    @staticmethod
    def _item_width(item, available):
        widget = item.widget()
        if widget is not None and widget.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored:
            return min(available, item.minimumSize().width())
        return min(available, max(item.minimumSize().width(), item.sizeHint().width()))
