"""启动画面：品牌磁贴 + 产品名 + 版本；纯绘制，无外部资源依赖。"""
from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PyQt5.QtWidgets import QSplashScreen

from core.i18n import tr
from core.version import APP_NAME, APP_VERSION
from .components.brand_widgets import logo_pixmap

WIDTH, HEIGHT = 420, 236


def _background():
    pixmap = QPixmap(WIDTH, HEIGHT)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(0, 0, WIDTH, HEIGHT)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0, QColor('#0A0B12'))
    gradient.setColorAt(1, QColor('#141733'))
    painter.setPen(Qt.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(rect, 16, 16)
    # 上缘环境光晕（与主界面同一语言）。
    halo = QRadialGradient(rect.center().x(), 40, 190)
    halo.setColorAt(0, QColor(124, 108, 240, 46))
    halo.setColorAt(1, QColor(124, 108, 240, 0))
    painter.setBrush(halo)
    painter.drawRoundedRect(rect, 16, 16)
    logo = logo_pixmap(72)
    painter.drawPixmap(int((WIDTH - 72) / 2), 46, logo)
    painter.setPen(QColor('#F4F5F7'))
    font = painter.font()
    font.setPixelSize(17)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRectF(0, 136, WIDTH, 26), Qt.AlignCenter, APP_NAME)
    font.setPixelSize(11)
    font.setBold(False)
    painter.setFont(font)
    painter.setPen(QColor('#8B93A3'))
    painter.drawText(QRectF(0, 166, WIDTH, 20), Qt.AlignCenter, f'v{APP_VERSION} · ' + tr('正在启动…'))
    painter.end()
    return pixmap


def show_splash():
    """显示启动画面；任何异常都静默降级，绝不阻塞启动。"""
    try:
        splash = QSplashScreen(_background())
        splash.show()
        return splash
    except Exception:
        return None
