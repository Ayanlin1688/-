"""全局异常兜底：未捕获异常写现场并通知 UI，避免无人值守进程被直接终止。

PyQt5 在槽函数出现未捕获异常时会走 sys.excepthook；安装自定义钩子后
进程不再被 qFatal 终止，挂机期间的单点异常不会拖垮整条队列。
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

_reporter = None


class CrashReporter:
    def __init__(self, log_root):
        self.log_root = Path(log_root)
        self._notify = None

    def set_notify(self, callback):
        self._notify = callback

    def report(self, exc_type, exc, tb):
        path = ''
        try:
            self.log_root.mkdir(parents=True, exist_ok=True)
            target = self.log_root / time.strftime('crash-%Y%m%d.log')
            with open(target, 'a', encoding='utf-8') as stream:
                stream.write('\n===== 未捕获异常 ' + time.strftime('%Y-%m-%d %H:%M:%S') + ' =====\n')
                traceback.print_exception(exc_type, exc, tb, file=stream)
            path = str(target)
        except Exception:
            path = ''
        if self._notify is not None:
            try:
                self._notify(exc_type, exc, path)
            except Exception:
                pass

    def install(self):
        def excepthook(exc_type, exc, tb):
            self.report(exc_type, exc, tb)

        def threading_hook(args):
            if args.exc_type is SystemExit:
                return
            self.report(args.exc_type, args.exc_value, args.exc_traceback)

        sys.excepthook = excepthook
        threading.excepthook = threading_hook


def install_default(log_root):
    global _reporter
    _reporter = CrashReporter(log_root)
    _reporter.install()
    return _reporter


def set_notify(callback):
    if _reporter is not None:
        _reporter.set_notify(callback)
