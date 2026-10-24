"""兼容入口：历史记录面板已由 1:1 复刻的「电商图任务」列表组件实现（段2）。

保留本模块名是为了打包 hidden-import 与历史引用的连续性；实际实现与对外契约
见 `ecommerce_task_list.EcommerceTaskList`。
"""
from .ecommerce_task_list import (  # noqa: F401 - re-export for packaging/import sites
    RESUMABLE_STATUSES,
    EcommerceTaskCard,
    EcommerceTaskList,
    PAGE_SIZE,
    TAB_KEYS,
    TAB_STATUSES,
)

__all__ = ['EcommerceTaskList', 'EcommerceTaskCard', 'RESUMABLE_STATUSES',
           'PAGE_SIZE', 'TAB_KEYS', 'TAB_STATUSES']
