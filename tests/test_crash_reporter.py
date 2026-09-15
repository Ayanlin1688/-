"""全局异常兜底：现场落盘、通知回调、钩子安装均不抛出。"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from core.crash_reporter import CrashReporter


class CrashReporterTests(unittest.TestCase):
    def test_report_writes_traceback_and_notifies(self):
        with tempfile.TemporaryDirectory() as temp:
            reporter = CrashReporter(temp)
            seen = []
            reporter.set_notify(lambda exc_type, exc, path: seen.append((exc_type.__name__, str(exc), path)))
            try:
                raise ValueError('槽函数炸了')
            except ValueError:
                info = sys.exc_info()
            reporter.report(*info)
            files = list(Path(temp).glob('crash-*.log'))
            self.assertEqual(len(files), 1)
            body = files[0].read_text(encoding='utf-8')
            self.assertIn('ValueError', body)
            self.assertIn('槽函数炸了', body)
            self.assertEqual(seen[0][0], 'ValueError')
            self.assertEqual(seen[0][1], '槽函数炸了')
            self.assertTrue(seen[0][2].endswith('.log'))

    def test_install_hooks_survive_exception_without_raising(self):
        with tempfile.TemporaryDirectory() as temp:
            reporter = CrashReporter(temp)
            previous_sys, previous_threading = sys.excepthook, threading.excepthook
            try:
                reporter.install()
                self.assertIsNot(sys.excepthook, previous_sys)
                try:
                    raise RuntimeError('boom')
                except RuntimeError:
                    sys.excepthook(*sys.exc_info())  # 不应抛出，也不应终止
                args = threading.ExceptHookArgs((RuntimeError, RuntimeError('thread-boom'), None, None))
                threading.excepthook(args)
            finally:
                sys.excepthook, threading.excepthook = previous_sys, previous_threading
            files = list(Path(temp).glob('crash-*.log'))
            self.assertEqual(len(files), 1)
            body = files[0].read_text(encoding='utf-8')
            self.assertIn('boom', body)
            self.assertIn('thread-boom', body)

    def test_report_never_raises_when_root_unavailable(self):
        reporter = CrashReporter('\x00invalid-root')
        reporter.set_notify(lambda *_: (_ for _ in ()).throw(RuntimeError('notify-fail')))
        try:
            raise KeyError('k')
        except KeyError:
            info = sys.exc_info()
        reporter.report(*info)  # 双保险：路径无效 + 通知抛错都不外溢


if __name__ == '__main__':
    unittest.main()
