"""共享准入阀门：退避只作用新提交，绝不打断运行中的轮询。

- 连续 429 会暂停新提交；冷却到点自动试探恢复（无需人工点击）。
- 成功提交会让并发上限逐级回升到初始值，避免限流风暴后长期锁死。
"""
import threading
import time
from contextlib import contextmanager
from collections import deque

DEFAULT_PROBE_SECONDS = 60.0


class SubmissionGate:
    def __init__(self, concurrency, now=None, probe_seconds=DEFAULT_PROBE_SECONDS):
        self.limit = concurrency
        self.base_limit = max(1, int(concurrency))
        self.probe_seconds = max(.05, float(probe_seconds or DEFAULT_PROBE_SECONDS))
        self.now = now or time.monotonic
        self.deadline = 0.0
        self.consecutive_429 = 0
        self.failures = 0
        self.paused = False
        self.pause_count = 0
        self.paused_until = 0.0
        self.on_recover = None
        self.lock = threading.Lock()
        self._epoch = 0
        self._permits = deque()

    @contextmanager
    def permit(self, control):
        """FIFO upload/create admission; release before long-running polling.

        Canceled waiters remove their ticket, so they cannot strand the queue.
        The ledger remains the cross-process authority for duplicate protection.
        """
        ticket = object()
        with self.lock:
            self._permits.append(ticket)
            seen_epoch = self._epoch
        try:
            while True:
                control.check()
                with self.lock:
                    admitted = self._permits[0] is ticket
                if admitted:
                    break
                control.delay(.05)
            self.wait(control)
            control.before_task()
            yield seen_epoch
        finally:
            with self.lock:
                self._permits.remove(ticket)

    def failed(self, status_code):
        with self.lock:
            if status_code != 429 and not (status_code and 500 <= status_code < 600):
                self.consecutive_429 = 0
                return 0
            self._epoch += 1
            self.failures += 1
            seconds = min(60, 5 * 2 ** min(self.failures - 1, 4))
            self.deadline = max(self.deadline, self.now() + seconds)
            if status_code == 429:
                self.limit = max(1, self.limit - 1)
                if self.paused:
                    # 暂停期间的再次限流（多为试探失败）：按倍数延长冷却。
                    self.pause_count += 1
                    self.paused_until = self.now() + min(600.0, self.probe_seconds * 2 ** (self.pause_count - 1))
                else:
                    self.consecutive_429 += 1
                    if self.consecutive_429 >= 3:
                        self.paused = True
                        self.pause_count += 1
                        self.paused_until = self.now() + min(600.0, self.probe_seconds * 2 ** (self.pause_count - 1))
            else:
                self.consecutive_429 = 0
            return seconds

    def succeeded(self, seen_epoch=None):
        with self.lock:
            # A create that queued before this 429/5xx burst must not reopen admission.
            if seen_epoch is not None and seen_epoch != self._epoch:
                return False
            self.failures = 0
            self.consecutive_429 = 0
            # 成功即视为渠道健康：清退避窗口，并发上限逐级回升到初始值。
            self.deadline = 0.0
            self.limit = min(self.base_limit, self.limit + 1)

    def remaining(self):
        with self.lock:
            return max(0.0, self.deadline - self.now())

    def pause_remaining(self):
        with self.lock:
            return max(0.0, self.paused_until - self.now())

    def resume(self):
        """人工「继续」：立即解除暂停并重置升级节奏；上限仍按成功逐步回升。"""
        with self.lock:
            self.paused = False
            self.consecutive_429 = 0
            self.pause_count = 0
            self.paused_until = 0.0

    def auto_resume(self):
        with self.lock:
            if not self.paused or self.now() < self.paused_until:
                return False
            self.paused = False
            self.deadline = 0.0
            return True

    def wait(self, control):
        """阻塞到可提交：退避窗口结束或限流冷却到点（自动试探恢复）。

        冷却到点时立即返回，让调用方发起试探性提交（等待本身已经过冷却间隔）；
        被取消/跳过时抛出 Cancelled。
        """
        while True:
            control.check()
            recovered = False
            with self.lock:
                if self.paused and self.now() >= self.paused_until:
                    self.paused = False
                    self.deadline = 0.0  # 冷却间隔已充当退避，恢复后立即允许试探提交
                    recovered = True
                paused = self.paused
                edge = self.paused_until if paused else self.deadline
                remaining = max(0.0, edge - self.now())
            if recovered:
                if self.on_recover is not None:
                    try:
                        self.on_recover()
                    except Exception:
                        pass
                return
            if not paused and remaining <= 0:
                return
            control.delay(min(.25, max(.02, remaining)))
