"""多主题调色板注册表。

每个主题是一份完整的视觉 token：窗口渐变、环境光晕、玻璃表面、
描边、文字层级与强调色。界面代码只允许从这里取色，禁止再散落硬编码颜色。
"""
from __future__ import annotations


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip('#')
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _hex(value: str) -> str:
    return '#' + value.lstrip('#').upper()


# ---------------------------------------------------------------------------
# 主题 token 说明：
#   bg1/bg2          窗口纵向渐变（上 → 下）
#   bgm              可选：渐变中段色（三段渐变，缺省忽略）
#   glow1/glow2      左上 / 右下环境径向光晕 (r, g, b, a)
#   glow3/glow4      可选：右上 / 左下环境径向光晕（缺省不绘制）
#   surface(_add)    玻璃表面填充 rgba 基值 / 悬停增量
#   border(_add)     发丝描边 rgba 基值 / 悬停增量
#   border_elev      悬浮卡片的提亮描边
#   line             控件发丝线（浅色主题与主色同族；缺省回退 ink）
#   ink              叠加墨色（深色主题用白、浅色主题用深蓝）
#   highlight        玻璃上缘高光透明度
#   text1/2/3        主 / 次 / 弱文字色
#   accent/accent2   强调色渐变（主按钮 / 指示条 / 进度条）
#   acrylic          高斯模糊模式的窗口着色（RRGGBBAA）
#   nav              左侧导航栏底色的 rgba 字符串
#   frame            窗口圆角描边
# ---------------------------------------------------------------------------


BASE_SEMANTIC = {
    'success': '#22C55E',
    'warning': '#F59E0B',
    'error': '#F56C6C',
}


THEMES: dict[str, dict] = {
    'dark': {
        'id': 'dark', 'name': '深空', 'name_en': 'Deep Space', 'light': False,
        'bg1': '#0A0B12', 'bg2': '#10111C',
        'glow1': (91, 141, 239, 16), 'glow2': (124, 108, 240, 14),
        'surface': (255, 255, 255, 13), 'surface_add': 8,
        'border': (255, 255, 255, 26), 'border_add': 20,
        'border_elev': (150, 168, 255, 128),
        'ink': (255, 255, 255), 'highlight': 22,
        'text1': '#F4F5F7', 'text2': '#A9B1C1', 'text3': '#7C8596',
        'accent': '#5B8DEF', 'accent2': '#7C6CF0',
        'acrylic': '12141CA8', 'nav': 'rgba(10,11,18,0.45)',
        'frame': (255, 255, 255, 26),
    },
    'midnight': {
        'id': 'midnight', 'name': '午夜', 'name_en': 'Midnight', 'light': False,
        'bg1': '#05060D', 'bg2': '#0A0D1A',
        'glow1': (99, 102, 241, 15), 'glow2': (56, 89, 248, 12),
        'surface': (250, 250, 255, 12), 'surface_add': 8,
        'border': (200, 205, 255, 24), 'border_add': 18,
        'border_elev': (130, 140, 255, 122),
        'ink': (255, 255, 255), 'highlight': 20,
        'text1': '#F2F3F8', 'text2': '#A6ACBF', 'text3': '#7A8195',
        'accent': '#6366F1', 'accent2': '#8B5CF6',
        'acrylic': '0A0D1AA8', 'nav': 'rgba(6,7,14,0.45)',
        'frame': (200, 205, 255, 28),
    },
    'graphite': {
        'id': 'graphite', 'name': '石墨', 'name_en': 'Graphite', 'light': False,
        'bg1': '#0C0C0E', 'bg2': '#141416',
        'glow1': (255, 176, 120, 10), 'glow2': (255, 255, 255, 7),
        'surface': (255, 255, 255, 12), 'surface_add': 8,
        'border': (255, 255, 255, 25), 'border_add': 18,
        'border_elev': (255, 190, 140, 105),
        'ink': (255, 255, 255), 'highlight': 20,
        'text1': '#F3F3F4', 'text2': '#ABACB1', 'text3': '#7E7F85',
        'accent': '#B8743F', 'accent2': '#D08B5B',
        'acrylic': '141317A8', 'nav': 'rgba(12,12,14,0.45)',
        'frame': (255, 255, 255, 26),
    },
    'nebula': {
        'id': 'nebula', 'name': '星云', 'name_en': 'Nebula', 'light': False,
        'bg1': '#0B0713', 'bg2': '#150C22',
        'glow1': (168, 85, 247, 16), 'glow2': (124, 58, 237, 13),
        'surface': (250, 245, 255, 13), 'surface_add': 8,
        'border': (220, 190, 255, 26), 'border_add': 18,
        'border_elev': (190, 140, 255, 118),
        'ink': (255, 255, 255), 'highlight': 22,
        'text1': '#F6F3FA', 'text2': '#AFA5C0', 'text3': '#847A99',
        'accent': '#A855F7', 'accent2': '#7C3AED',
        'acrylic': '150C22A8', 'nav': 'rgba(11,7,19,0.45)',
        'frame': (220, 190, 255, 30),
    },
    'abyss': {
        'id': 'abyss', 'name': '深海', 'name_en': 'Abyss', 'light': False,
        'bg1': '#050F14', 'bg2': '#0A1A20',
        'glow1': (45, 212, 191, 14), 'glow2': (56, 189, 248, 12),
        'surface': (240, 255, 252, 12), 'surface_add': 8,
        'border': (170, 235, 230, 24), 'border_add': 18,
        'border_elev': (110, 220, 215, 112),
        'ink': (255, 255, 255), 'highlight': 20,
        'text1': '#EEF4F4', 'text2': '#A0B2B5', 'text3': '#738A8F',
        'accent': '#0D9488', 'accent2': '#0891B2',
        'acrylic': '071017A8', 'nav': 'rgba(4,13,17,0.45)',
        'frame': (170, 235, 230, 28),
    },
    'dusk': {
        'id': 'dusk', 'name': '暮色', 'name_en': 'Dusk', 'light': False,
        'bg1': '#120B07', 'bg2': '#1D120B',
        'glow1': (249, 115, 22, 13), 'glow2': (244, 63, 94, 10),
        'surface': (255, 248, 240, 13), 'surface_add': 8,
        'border': (255, 215, 185, 26), 'border_add': 18,
        'border_elev': (255, 170, 120, 112),
        'ink': (255, 255, 255), 'highlight': 20,
        'text1': '#FAF4EE', 'text2': '#BCACA0', 'text3': '#8F8076',
        'accent': '#EA580C', 'accent2': '#E11D48',
        'acrylic': '1C110AA8', 'nav': 'rgba(18,11,7,0.45)',
        'frame': (255, 215, 185, 28),
    },
    'light': {
        'id': 'light', 'name': '白玉', 'name_en': 'Porcelain', 'light': True,
        'bg1': '#F7F9FE', 'bg2': '#E8EDF8',
        'glow1': (91, 141, 239, 30), 'glow2': (124, 108, 240, 24),
        'surface': (255, 255, 255, 158), 'surface_add': 24,
        'border': (60, 76, 165, 40), 'border_add': 26,
        'border_elev': (70, 90, 190, 52),
        'line': (60, 76, 165),
        'ink': (15, 26, 52), 'highlight': 150,
        'text1': '#1A1D24', 'text2': '#4F586A', 'text3': '#566070',
        'accent': '#5B8DEF', 'accent2': '#7C6CF0',
        'acrylic': 'F3F6FCA8', 'nav': 'rgba(247,249,254,0.60)',
        'frame': (15, 26, 52, 46),
    },
    'mist': {
        'id': 'mist', 'name': '晨雾', 'name_en': 'Mist', 'light': True,
        'bg1': '#F1F5FA', 'bg2': '#E2EAF4',
        'glow1': (96, 165, 250, 28), 'glow2': (125, 211, 252, 22),
        'surface': (255, 255, 255, 162), 'surface_add': 22,
        'border': (30, 50, 90, 36), 'border_add': 26,
        'border_elev': (59, 130, 246, 62),
        'ink': (30, 50, 90), 'highlight': 160,
        'text1': '#17233A', 'text2': '#4E5F7A', 'text3': '#5A6C86',
        'accent': '#2F6FED', 'accent2': '#5B8DEF',
        'acrylic': 'EAF1FAA8', 'nav': 'rgba(241,245,250,0.60)',
        'frame': (30, 50, 90, 48),
    },
    'sand': {
        'id': 'sand', 'name': '暖沙', 'name_en': 'Sand', 'light': True,
        'bg1': '#F8F5EE', 'bg2': '#EFE8DA',
        'glow1': (235, 170, 105, 26), 'glow2': (215, 150, 100, 20),
        'surface': (255, 253, 248, 168), 'surface_add': 22,
        'border': (92, 70, 45, 36), 'border_add': 26,
        'border_elev': (190, 140, 80, 62),
        'ink': (80, 60, 40), 'highlight': 165,
        'text1': '#2C2519', 'text2': '#635A49', 'text3': '#786E5B',
        'accent': '#B06E2C', 'accent2': '#C98E4D',
        'acrylic': 'F7F3EAA8', 'nav': 'rgba(248,245,238,0.60)',
        'frame': (92, 70, 45, 48),
    },
    'aurora': {
        'id': 'aurora', 'name': '霞光', 'name_en': 'Aurora', 'light': True,
        'bg1': '#C6D8FF', 'bgm': '#E4C4DB', 'bg2': '#F0B48F',
        'glow1': (146, 178, 255, 86), 'glow2': (242, 116, 62, 80),
        'glow3': (244, 158, 198, 88), 'glow4': (248, 192, 152, 94),
        'glow_scale': 1.6,
        'surface': (255, 255, 255, 84), 'surface_add': 14,
        'border': (255, 255, 255, 214), 'border_add': 22,
        'border_elev': (255, 255, 255, 238),
        'line': (74, 141, 255),
        'ink': (30, 45, 80), 'highlight': 240,
        'text1': '#1C2836', 'text2': '#3A475D', 'text3': '#4E5C70',
        'accent': '#4A8DFF', 'accent2': '#8B5CF6',
        'acrylic': 'F2ECF4C8', 'nav': 'rgba(255,250,252,0.55)',
        'frame': (255, 255, 255, 170),
    },
}

# 主题选择下拉的顺序：深色家族 → 浅色家族 → 跟随系统
THEME_ORDER = ['dark', 'midnight', 'graphite', 'nebula', 'abyss', 'dusk', 'light', 'mist', 'sand', 'aurora']
SYSTEM_OPTION = ('system', '跟随系统')

for _theme in THEMES.values():
    _theme['accent_rgb'] = _rgb(_theme['accent'])
    _theme['accent2_rgb'] = _rgb(_theme['accent2'])
    _theme['text1'] = _hex(_theme['text1'])
    _theme['text2'] = _hex(_theme['text2'])
    _theme['text3'] = _hex(_theme['text3'])
    _theme['success'] = BASE_SEMANTIC['success']
    _theme['warning'] = BASE_SEMANTIC['warning']
    _theme['error'] = BASE_SEMANTIC['error']


def system_prefers_light() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
        value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
        return bool(value)
    except Exception:
        return False


def resolve(theme_id):
    """把配置值解析成有效的主题 id；兼容历史值 dark / light / system。"""
    tid = str(theme_id or 'dark').strip()
    if tid == 'system':
        tid = 'light' if system_prefers_light() else 'dark'
    if tid not in THEMES:
        tid = 'dark'
    return tid


def theme_palette(theme_id):
    return THEMES[resolve(theme_id)]


def combo_choices():
    """主题下拉的 (值, 显示名) 列表。"""
    items = [(tid, THEMES[tid]['name']) for tid in THEME_ORDER]
    items.append(SYSTEM_OPTION)
    return [value for value, _ in items], [text for _, text in items]
