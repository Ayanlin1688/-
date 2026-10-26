"""Reusable, persistent platform picker for the single and batch forms."""
from PyQt5.QtCore import QEvent, Qt, QPoint, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics
from PyQt5.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from core.ecommerce.platforms import all_platforms, domestic, overseas
from core.i18n import register_retranslate, tr
from .. import materials
from ..responsive import WrapLayout
from ..tokens import px


class _Popup(QFrame):
    def showEvent(self, event):
        QApplication.instance().installEventFilter(self)
        super().showEvent(event)

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.MouseButtonPress and isinstance(watched, QWidget)
                and watched is not self and not self.isAncestorOf(watched)):
            self.hide()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            self.parentWidget().button.setFocus()
            event.accept()
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        self.parentWidget().arrow.setText('⌄')
        super().hideEvent(event)


class PlatformMultiSelect(QWidget):
    changed = pyqtSignal(object)
    limit_reached = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('platformMultiSelect')
        self._maximum = 5
        self._selected = []
        self._recent = []
        self.records = {p['id']: p for p in all_platforms()}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.button = QPushButton(self)
        self.button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.button.setObjectName('platformSelectButton')
        self.button.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self.button)
        row.setContentsMargins(px(10), 0, px(10), 0)
        row.setSpacing(px(5))
        self.placeholder = QLabel(self.button)
        self.placeholder.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(self.placeholder)
        self.selected_chips = []
        for _ in range(5):
            chip = QLabel(self.button)
            chip.setAttribute(Qt.WA_TransparentForMouseEvents)
            chip.setMinimumWidth(0)
            chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            row.addWidget(chip)
            self.selected_chips.append(chip)
        row.addStretch(1)
        self.arrow = QLabel('⌄', self.button)
        self.arrow.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(self.arrow)
        root.addWidget(self.button)
        self.popup = _Popup(self, Qt.Popup | Qt.FramelessWindowHint)
        self.popup.setObjectName('platformSelectPopup')
        self.shadow = QGraphicsDropShadowEffect(self.popup)
        self.popup.setGraphicsEffect(self.shadow)
        layout = QVBoxLayout(self.popup)
        layout.setContentsMargins(px(16), px(14), px(16), px(14))
        self.hint = QLabel(self.popup)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        scroll = QScrollArea(self.popup)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        sections = QVBoxLayout(content)
        sections.setContentsMargins(0, 0, 0, 0)
        self.group_labels = {}
        self.chips = {}
        self.recent_chips = []
        for group, records in (('recent', []), ('domestic', domestic()), ('overseas', overseas())):
            label = QLabel(content)
            self.group_labels[group] = label
            sections.addWidget(label)
            choices = WrapLayout(spacing=px(8))
            sections.addLayout(choices)
            for record in records:
                chip = self._make_chip(record['id'], content)
                self.chips[record['id']] = chip
                choices.addWidget(chip)
            if group == 'recent':
                for _ in range(5):
                    chip = self._make_chip('', content)
                    choices.addWidget(chip)
                    self.recent_chips.append(chip)
        sections.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        footer = QHBoxLayout()
        self.clear_button = QPushButton(self.popup)
        self.clear_button.clicked.connect(lambda: self.set_platforms([]))
        footer.addWidget(self.clear_button)
        footer.addStretch(1)
        self.count_label = QLabel(self.popup)
        footer.addWidget(self.count_label)
        layout.addLayout(footer)
        self.button.clicked.connect(self.toggle_popup)
        self.refresh_interface_scale()
        self.retranslate_ui()
        register_retranslate(self)
        materials.register_theme_callback(self.refresh_colors)

    def _make_chip(self, platform_id, parent):
        chip = QPushButton(parent)
        chip.setCheckable(True)
        chip.setProperty('platformId', platform_id)
        chip.toggled.connect(lambda checked, chip=chip: self._toggle(chip.property('platformId'), checked))
        return chip

    def selected_platforms(self):
        return list(self._selected)

    def setMaximumCount(self, count):
        self._maximum = max(1, min(5, int(count)))
        self.set_platforms(self._selected)

    def set_platforms(self, platforms):
        wanted = list(dict.fromkeys(p for p in platforms or [] if p in self.records))
        if len(wanted) > self._maximum:
            self.limit_reached.emit(self._maximum)
        wanted = wanted[:self._maximum]
        changed = wanted != self._selected
        self._selected = wanted
        self._recent = list(dict.fromkeys(wanted + self._recent))[:5]
        self._sync()
        if changed:
            self.changed.emit(self.selected_platforms())

    def _toggle(self, platform_id, checked):
        wanted = self.selected_platforms()
        if checked and platform_id not in wanted:
            if len(wanted) >= self._maximum:
                self.limit_reached.emit(self._maximum)
                self._sync()
                return
            wanted.append(platform_id)
        elif not checked and platform_id in wanted:
            wanted.remove(platform_id)
        self.set_platforms(wanted)

    def _sync(self):
        self.placeholder.setVisible(not self._selected)
        for index, chip in enumerate(self.selected_chips):
            chip.setVisible(index < len(self._selected))
            chip.setToolTip(self.records[self._selected[index]]['name'] if index < len(self._selected) else '')
        self._elide_selected()
        for chip in self.chips.values():
            chip.setText(self.records[chip.property('platformId')]['name'])
            checked = chip.property('platformId') in self._selected
            chip.blockSignals(True)
            chip.setChecked(checked)
            chip.blockSignals(False)
            chip.setProperty('checked', checked)
        self.group_labels['recent'].setVisible(bool(self._recent))
        for index, chip in enumerate(self.recent_chips):
            chip.setVisible(index < len(self._recent))
            if index < len(self._recent):
                pid = self._recent[index]
                chip.setProperty('platformId', pid)
                chip.setText(self.records[pid]['name'])
                chip.blockSignals(True)
                chip.setChecked(pid in self._selected)
                chip.blockSignals(False)
                chip.setProperty('checked', pid in self._selected)
        self.count_label.setText(tr('已选 {count}/{max} 个').format(count=len(self._selected), max=self._maximum))
        self.button.setAccessibleName(tr('电商平台') + ': ' + ', '.join(self.records[p]['name'] for p in self._selected))
        self.refresh_colors()

    def _elide_selected(self):
        available = max(1, self.width() - px(50) - px(5) * max(0, len(self._selected) - 1))
        width = available // max(1, len(self._selected))
        for chip in self.selected_chips:
            metrics = QFontMetrics(chip.font())
            text = metrics.elidedText(chip.toolTip(), Qt.ElideRight, max(1, width - px(6)))
            chip.setText(text)
            chip.setFixedWidth(min(width, metrics.horizontalAdvance(text) + px(12)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide_selected()

    def toggle_popup(self):
        if self.popup.isVisible():
            self.popup.hide()
            return
        available = self.screen().availableGeometry()
        width = min(max(self.width(), px(420)), available.width())
        height = min(px(520), available.height())
        self.popup.resize(width, height)
        point = self.mapToGlobal(QPoint(0, self.height()))
        point.setX(min(max(available.left(), point.x()), available.right() - width + 1))
        point.setY(min(max(available.top(), point.y()), available.bottom() - height + 1))
        self.popup.move(point)
        self.popup.show()
        self.arrow.setText('⌃')
        self.clear_button.setFocus()

    def retranslate_ui(self):
        self.placeholder.setText(tr('请选择平台'))
        self.hint.setText(tr('建议只选一个平台，多平台可能存在规则冲突（最多5个）'))
        self.group_labels['recent'].setText(tr('最近使用'))
        self.group_labels['domestic'].setText(tr('国内'))
        self.group_labels['overseas'].setText(tr('海外'))
        self.clear_button.setText(tr('清空'))
        self._sync()

    def refresh_interface_scale(self):
        self.button.setFixedHeight(px(40))
        self.button.layout().setContentsMargins(px(10), 0, px(10), 0)
        self.button.layout().setSpacing(px(5))
        self.popup.layout().setContentsMargins(px(16), px(14), px(16), px(14))
        self.shadow.setBlurRadius(px(20))
        self.shadow.setOffset(0, px(4))
        for chip in list(self.chips.values()) + self.recent_chips:
            chip.setFixedHeight(px(30))
        self.refresh_colors()

    def refresh_colors(self):
        c = materials.ecommerce_color
        self.shadow.setColor(QColor(c('selector_shadow')))
        button_rules = ('QPushButton#platformSelectButton {background:%s;color:%s;border:1px solid %s;border-radius:%dpx;}'
                        % (c('single_card'), materials.palette()['text1'], c('single_button_border'), px(8)))
        if self.styleSheet() != button_rules:
            self.setStyleSheet(button_rules)
        popup_rules = (
            'QFrame#platformSelectPopup {background:%s;border:1px solid %s;border-radius:%dpx;}'
            'QLabel {color:%s;background:transparent;border:0;}'
            'QScrollArea, QScrollArea > QWidget > QWidget {background:%s;border:0;}'
            'QPushButton {background:%s;color:%s;border:1px solid %s;border-radius:%dpx;padding:3px 9px;}'
            'QPushButton:checked {background:%s;color:%s;border-color:%s;}'
            % (c('single_card'), c('single_button_border'), px(12), materials.palette()['text1'],
               c('single_card'), c('single_card'), materials.palette()['text2'], c('single_button_border'), px(8),
               c('tag_bg'), c('single_button_text'), c('single_button_text')))
        # The owning page and theme bus both notify this shared component.
        # Reapplying identical popup QSS would repolish every platform chip.
        if self.popup.styleSheet() != popup_rules:
            self.popup.setStyleSheet(popup_rules)
        for chip in self.selected_chips:
            chip.setStyleSheet('background:%s;color:%s;border-radius:%dpx;padding:2px 3px;font-size:%dpx;'
                              % (c('tag_bg'), c('single_button_text'), px(4), px(11)))
