"""Build the reference workflow's analysis prompts without network or Qt."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .platforms import LANGUAGE_TEXT, MAX_PLATFORMS, RATIOS, _resolve_platforms


def _quantity(d: int, g: int, cr: str, dr: str) -> str:
    if any(type(count) is not int or count < 0 for count in (d, g)) or d + g == 0:
        raise ValueError('图片数量必须为非负整数，且总数至少为 1')
    if cr not in RATIOS or dr not in RATIOS:
        raise ValueError('图片比例不在可选比例集合中')
    return f'轮播图 {d} 张（宽高比 {cr}） + 详情图 {g} 张（宽高比 {dr}）'


def _background_rule(platforms: list[dict]) -> str:
    white = '、'.join(item['name'] for item in platforms if item['bgRule'] == 'white')
    no_white = '、'.join(item['name'] for item in platforms if item['bgRule'] == 'no-white')
    clauses = []
    if white:
        clauses.append(f'凡平台规范要求白底的（仅适用于：{white}），prompt 必须包含 '
                       '"pure white background #FFFFFF, studio lighting"')
    if no_white:
        clauses.append(f'凡规范禁止白底的（仅适用于：{no_white}），'
                       'prompt 中不得出现 white background')
    if white and no_white:
        clauses.append('所选平台的白底与禁白底要求互斥，需调整平台选择或分两批生成，'
                       '不得把白底关键词要求套用到禁止白底的平台')
    return '- 背景要求：' + ('；'.join(clauses) if clauses else '按所选平台规范设置背景')


def build_system(platforms: Iterable[str | Mapping], language: str,
                 d: int, g: int, cr: str, dr: str) -> str:
    """Accept platform IDs or records. Resolve reported conflicts before generation."""
    selected = _resolve_platforms(platforms)
    if not selected:
        raise ValueError('请至少选择 1 个平台')
    if language not in LANGUAGE_TEXT:
        raise ValueError(f'不支持的图片文字语言：{language}')
    quantity = _quantity(d, g, cr, dr)
    platform_text = '\n\n'.join(
        f'【{item["name"]}】调性：{item["toneDesc"]}\n'
        f'  轮播图规范：{item["rules"]["carousel"]}\n'
        f'  详情图规范：{item["rules"]["detail"]}' for item in selected)
    return f'''你是专业电商视觉策划师，精通国内外主流电商平台的视觉规范和审美标准。
用户会提供产品图片和产品信息，你需要为电商平台生成专业的电商图提示词。

平台规范（每个平台都有明确的轮播图/详情图硬性规定，生成 prompt 时必须严格遵守）：
{platform_text}

图片文字语言要求：{LANGUAGE_TEXT[language]}
输出要求：
- 必须严格生成 {quantity}，JSON 数组长度必须恰好为 {d + g} 项，不多不少
- 每张图输出一个 JSON 对象，包含以下字段：
  - type: "carousel"（轮播图）或 "detail"（详情图）
  - index: 序号（从 1 开始，carousel 和 detail 各自独立编号）
  - prompt: 详细的英文图片生成 prompt（描述场景、构图、灯光、风格，要专业具体）
  - desc: 该图片的中文说明（一句话概括用途和内容）
- 最终输出一个 JSON 数组，不包含任何其他文字说明
{_background_rule(selected)}
- 如果用户提供了产品名称，轮播图 prompt 中应自然融入品牌/产品名称感；详情图 prompt 聚焦功能细节展示
- 多平台时，综合各平台的共同规范生成兼容性最强的一套 prompt（每个序号各一张，不是每个平台各一套）

直接输出 JSON 数组，不要有任何前缀或后缀文字。'''


def build_user(product_name: str, product_info: str, platform_names: Iterable[str],
               d: int, g: int, cr: str, dr: str) -> str:
    if isinstance(platform_names, (str, bytes)):
        raise ValueError('平台名称必须以列表形式提供')
    names = list(platform_names)
    if not 1 <= len(names) <= MAX_PLATFORMS:
        raise ValueError(f'请选择 1 至 {MAX_PLATFORMS} 个平台')
    quantity = _quantity(d, g, cr, dr)
    text = f'已选平台（共 {len(names)} 个）：{"、".join(names)}'
    name = (product_name or '').strip()
    if name:
        text += f'\n\n产品名称：{name}\n'
    return (f'{text}\n产品信息：\n{product_info}\n\n'
            f'请严格按配置生成 {d + g} 条电商图提示词（{quantity}）。')


def build_reference_order(product_count: int, has_style_reference: bool | None = None) -> str:
    """Return reference-image instructions for the supplied image layout.

    ``None`` preserves the historical one-argument helper contract (a positive
    count describes a product-plus-style layout).  Callers that
    know the actual layout pass an explicit boolean so product-only and
    style-only prompts receive their precise instructions.
    """
    if type(product_count) is not int or product_count < 0:
        raise ValueError('产品图数量必须为非负整数')
    legacy_layout = has_style_reference is None
    has_style = True if legacy_layout else bool(has_style_reference)
    if product_count == 0 and (legacy_layout or not has_style):
        return ''
    if product_count == 0:
        return (
            'Reference image order (critical): Image 1 (the only reference image) is style '
            'reference only — match its color palette, lighting, mood, composition and visual '
            'atmosphere; do not copy objects, products, logos or text from the style reference '
            'image.'
        )
    if not has_style:
        return (
            f'Reference image order (critical): Images 1-{product_count} are product photos — '
            'preserve the product appearance, shape, colors, materials, branding and text from '
            f'these images. Images 1-{product_count} are all product references.'
        )
    return (
        f'Reference image order (critical): Images 1-{product_count} are product photos — '
        'preserve the product appearance, shape, colors, materials and branding from these images. '
        f'Image {product_count + 1} (the last image) is style reference only — '
        'match its color palette, lighting, mood, composition and visual atmosphere; '
        'do not copy objects, products, logos or text from the style reference image.'
    )


def append_reference_order(prompt: str, product_count: int,
                           has_style_reference: bool = False) -> str:
    """Append reference-image instructions when either reference type is present."""
    suffix = build_reference_order(product_count, has_style_reference)
    return f'{prompt}\n\n{suffix}' if suffix else prompt
