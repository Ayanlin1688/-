"""Shared admission backoff affects new POSTs, never active job polling."""
import threading
import time


class SubmissionGate:
    def __init__(self, concurrency, now=None):
        self.limit = concurrency
        self.now = now or time.monotonic
        self.deadline = 0.0
        self.consecutive_429 = 0
        self.failures = 0
        self.paused = False
        self.lock = threading.Lock()

    def failed(self, status_code):
        with self.lock:
            if status_code != 429 and not (status_code and 500 <= status_code < 600):
                self.consecutive_429 = 0
                return 0
            self.failures += 1
            seconds = min(60, 5 * 2 ** min(self.failures - 1, 4))
            self.deadline = max(self.deadline, self.now() + seconds)
            if status_code == 429:
                self.limit = max(1, self.limit - 1)
                self.consecutive_429 += 1
                if self.consecutive_429 >= 3:
                    self.paused = True
            else:
                self.consecutive_429 = 0
            return seconds

    def succeeded(self):
        with self.lock:
            self.failures = 0
            self.consecutive_429 = 0

    def remaining(self):
        with self.lock:
            return max(0.0, self.deadline - self.now())

    def resume(self):
        with self.lock:
            self.paused = False
            self.consecutive_429 = 0

    def wait(self, control):
        while True:
            control.check()
            if not self.paused and self.remaining() <= 0:
                return
            control.delay(.1)
