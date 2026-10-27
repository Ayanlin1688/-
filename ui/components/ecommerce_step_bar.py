"""电商图制作的三步进度条，仅呈现页面传入的阶段。"""
from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from core.i18n import register_retranslate, tr
from .. import materials
from ..tokens import px


class _StepCircle(QWidget):
    def __init__(self, number, parent=None):
        super().__init__(parent)
        self.number = number
        self.setProperty('stepState', 'pending')
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        state = self.property('stepState')
        color_key = {'done': 'step_done', 'active': 'step_active',
                     'failed': 'completion_error'}.get(state, 'step_pending')
        color = QColor(materials.ecommerce_color(color_key))
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(color, max(1.0, float(px(1)))))
        painter.setBrush(color if state in ('done', 'failed') else Qt.NoBrush)
        painter.drawEllipse(rect)
        if state == 'active':
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            radius = max(2, px(3))
            painter.drawEllipse(rect.center(), radius, radius)
            return
        painter.setPen(QColor(materials.ecommerce_color('step_text_on_color'))
                       if state in ('done', 'failed') else color)
        font = self.font()
        font.setFamily(materials.ui_font_family())
        font.setPixelSize(px(12))
        font.setBold(state == 'done')
        painter.setFont(font)
        text = ('✓' if state == 'done' else '!' if state == 'failed'
                else '×' if state == 'cancelled' else str(self.number))
        painter.drawText(rect, Qt.AlignCenter, text)


class EcommerceStepBar(QWidget):
    """显示分析、生成和完成三步；状态变更不创建动画或后台任务。"""

    STAGES = frozenset(('idle', 'analyzing', 'generating', 'finished', 'failed', 'cancelled'))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('ecommerceStepBar')
        self._stage = None
        self.circles, self.labels, self.lines = [], [], []
        self._step_layouts = []
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        for index in range(3):
            step = QWidget(self)
            step_layout = QHBoxLayout(step)
            step_layout.setContentsMargins(0, 0, 0, 0)
            circle = _StepCircle(index + 1, step)
            circle.setObjectName('ecommerceStepCircle' + str(index + 1))
            label = QLabel(step)
            label.setTextFormat(Qt.PlainText)
            label.setObjectName('ecommerceStepLabel' + str(index + 1))
            step_layout.addWidget(circle)
            step_layout.addWidget(label)
            step_layout.addStretch(1)
            self._step_layouts.append(step_layout)
            self.circles.append(circle)
            self.labels.append(label)
            self._layout.addWidget(step, 1)
            if index < 2:
                line = QFrame(self)
                line.setObjectName('ecommerceStepLine' + str(index + 1))
                line.setProperty('lineState', 'pending')
                self.lines.append(line)
                self._layout.addWidget(line, 1)
        register_retranslate(self)
        materials.register_theme_callback(self.refresh_colors)
        self.refresh_interface_scale()
        self.set_stage('idle')

    @property
    def stage(self):
        return self._stage

    def set_stage(self, stage):
        stage = str(stage)
        if stage not in self.STAGES:
            stage = 'idle'
        if stage == self._stage:
            return
        self._stage = stage
        states = {
            'idle': ('pending', 'pending', 'pending'),
            'analyzing': ('active', 'pending', 'pending'),
            'generating': ('done', 'active', 'pending'),
            'finished': ('done', 'done', 'done'),
            'failed': ('done', 'done', 'failed'),
            'cancelled': ('done', 'pending', 'cancelled'),
        }[stage]
        self.setProperty('stage', stage)
        for circle, label, state in zip(self.circles, self.labels, states):
            circle.setProperty('stepState', state)
            label.setProperty('stepState', state)
            circle.update()
        for index, line in enumerate(self.lines):
            line.setProperty('lineState', 'done' if states[index] == 'done' else 'pending')
        self.retranslate_ui()
        self.refresh_colors()

    def retranslate_ui(self):
        self.labels[0].setText(tr('AI 分析'))
        self.labels[1].setText(tr('生成图片'))
        self.labels[2].setText(tr('已取消') if self._stage == 'cancelled'
                               else tr('部分失败') if self._stage == 'failed' else tr('完成'))

    def refresh_interface_scale(self):
        self.setFixedHeight(px(48))
        self._layout.setSpacing(px(8))
        for circle, label, layout in zip(self.circles, self.labels, self._step_layouts):
            circle.setFixedSize(px(22), px(22))
            layout.setSpacing(px(7))
            font = label.font()
            font.setFamily(materials.ui_font_family())
            font.setPixelSize(px(12))
            label.setFont(font)
        for line in self.lines:
            line.setFixedHeight(max(1, px(2)))
            line.setMinimumWidth(px(8))
        self.refresh_colors()

    def refresh_colors(self):
        for label in self.labels:
            state = label.property('stepState')
            color = (materials.ecommerce_color('completion_error') if state == 'failed'
                     else materials.text_color(3 if state == 'pending' else 1))
            label.setStyleSheet('color: %s; background: transparent;' % color)
        for line in self.lines:
            key = 'step_done' if line.property('lineState') == 'done' else 'step_line'
            line.setStyleSheet('background: %s; border: none; border-radius: %dpx;'
                              % (materials.ecommerce_color(key), max(1, px(1))))
        for circle in self.circles:
            circle.update()
