"""User interface package."""
import sys
import traceback


def _fallback_excepthook(exc_type, exc, tb):
    # PyQt5 在槽函数出现未捕获异常时会走 sys.excepthook；默认钩子会触发 qFatal 终止进程。
    # 应用入口由 core.crash_reporter 兜底；本包为「直接导入 ui 的宿主进程」（测试套件 /
    # 工具脚本）提供同等的最小兜底：打印现场后继续运行，不再整段退出。
    traceback.print_exception(exc_type, exc, tb)
    try:
        sys.stderr.write('[ui] 未捕获异常已拦截（非致命），进程继续运行\n')
    except Exception:
        pass


if sys.excepthook is sys.__excepthook__:
    sys.excepthook = _fallback_excepthook
