"""Offline platform rules and prompt construction for ecommerce images."""

from .platforms import (
    LANGUAGE_TEXT,
    MAX_PLATFORMS,
    RATIOS,
    all_platforms,
    detect_conflicts,
    domestic,
    get_platform,
    overseas,
)
from .prompt_builder import (
    append_reference_order,
    build_reference_order,
    build_system,
    build_user,
)

__all__ = [
    'LANGUAGE_TEXT', 'MAX_PLATFORMS', 'RATIOS', 'all_platforms',
    'detect_conflicts', 'domestic', 'get_platform', 'overseas',
    'append_reference_order', 'build_reference_order', 'build_system', 'build_user',
]
