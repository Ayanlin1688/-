"""Bounded polling that stays responsive near completion and during URL publication."""

INITIAL_INTERVAL = 5.0
MAX_INTERVAL = 15.0
NEAR_COMPLETION_INTERVAL = 3.0
FINAL_INTERVAL = 2.0
PROCESSING_FAST_AFTER = 60.0
PROCESSING_FINAL_AFTER = 90.0
RESULT_URL_WAIT = 45.0


class PollSchedule:
    def __init__(self, initial_interval=INITIAL_INTERVAL):
        self.initial_interval = max(.05, min(INITIAL_INTERVAL, float(initial_interval)))
        self.interval = self.initial_interval
        self.last_progress = None
        self.peak_progress = 0
        self.processing_since = None

    def observe(self, status, progress, now):
        changed = progress != self.last_progress
        self.last_progress = progress
        self.peak_progress = max(self.peak_progress, progress)
        if status == 'processing':
            if self.processing_since is None:
                self.processing_since = now
        else:
            self.processing_since = None
        processing_seconds = 0 if self.processing_since is None else now - self.processing_since
        if status == 'completed' or self.peak_progress >= 95 or processing_seconds >= PROCESSING_FINAL_AFTER:
            self.interval = min(self.initial_interval, FINAL_INTERVAL)
        elif self.peak_progress >= 70 or processing_seconds >= PROCESSING_FAST_AFTER:
            self.interval = min(self.initial_interval, NEAR_COMPLETION_INTERVAL)
        elif changed:
            self.interval = self.initial_interval
        else:
            self.interval = min(MAX_INTERVAL, self.interval * 1.2)
        return self.interval
