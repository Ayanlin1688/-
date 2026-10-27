"""One ordered font fallback chain for native widgets and style sheets."""
from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtWidgets import QApplication

CJK_FAMILIES = (
    'Microsoft YaHei UI', 'Microsoft YaHei', '微软雅黑', 'SimSun',
    'Noto Sans CJK SC', 'Source Han Sans SC', 'Microsoft JhengHei UI', 'SimHei',
)
LATIN_FAMILIES = ('Segoe UI', 'Arial Unicode MS', 'Arial', 'DejaVu Sans')


def resolve_font_families(available=None):
    """Only choose installed families; Qt's default is the last fallback."""
    app = QApplication.instance()
    fallback = (app.font() if app else QFont()).family() or 'Sans Serif'
    if available is None:
        if app is None:
            return [fallback]
        try:
            available = QFontDatabase().families()
        except (RuntimeError, OSError):
            return [fallback]
    installed = {str(family).casefold(): str(family) for family in available}
    result = [installed[name.casefold()] for name in CJK_FAMILIES + LATIN_FAMILIES
              if name.casefold() in installed]
    if not result:
        result = [installed.get(fallback.casefold()) or next(iter(installed.values()), fallback)]
    return list(dict.fromkeys(result))


def css_font_families():
    return ','.join('"%s"' % family.replace('"', '') for family in resolve_font_families())
