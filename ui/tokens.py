"""设计 token：间距 / 圆角 / 字号 / 动效。

颜色一律从 ui.palettes 的主题 token 获取；本模块只承载与语言无关的
度量常量，供界面代码统一引用（禁止再散落魔法数字）。
"""
from __future__ import annotations

# 间距体系（8pt 制：4 / 8 / 12 / 16 / 24）
SPACE = {
    'xs': 4,    # 元素内微距
    'sm': 8,    # 控件间距
    'md': 12,   # 卡片内距
    'lg': 16,   # 组间距 / 页边距
    'xl': 24,   # 分区留白
}

# 圆角体系（两档：控件 / 容器）
RADIUS = {
    'control': 8,     # 按钮 / 输入 / 小控件
    'container': 12,  # 卡片 / 弹窗 / 容器
}

# 字号梯度（像素）
FONT = {
    'micro': 11,    # 徽标 / 极小注记
    'caption': 12,  # 辅助文字
    'body': 13,     # 正文
    'subtitle': 15, # 小标题
    'title': 20,    # 页面标题
    'hero': 28,     # 主标题 / 统计大数
}

# 图标尺寸三档（冻结：16 / 20 / 24）
ICON = {
    'sm': 16,  # 行内小图标
    'md': 20,  # 工具栏 / 列表
    'lg': 24,  # 大按钮 / 空态
}

# 动效时长（毫秒）
MOTION = {
    'fast': 120,   # 即时反馈（hover / 按压）
    'base': 180,   # 状态切换
    'slow': 300,   # 展开 / 层级过渡
}

# 导航两态宽度
NAV_COLLAPSED_WIDTH = 64
NAV_EXPAND_WIDTH = 240
