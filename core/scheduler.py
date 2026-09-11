"""Persistent local-time occurrences; consume before starting a generation."""
from datetime import datetime, timedelta
import re


class ScheduleEngine:
    def __init__(self, config_manager, now=None, log=None):
        self.config_manager = config_manager
        self.now = now or datetime.now
        self.log = log or (lambda *_: None)
        self.next_run = None
        self._previous_remaining = None
        self._reminded = False
        self.configure()

    def _save(self, values):
        try:
            if not self.config_manager.update(('schedule',), values):
                raise OSError('配置未保存')
            return True
        except (OSError, ValueError) as error:
            self.log(f'定时状态保存失败，本次不会启动：{error}', 'error')
            return False

    @staticmethod
    def _next_time(now, time_text):
        hour, minute = map(int, time_text.split(':'))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(days=1)

    def configure(self, reset=False):
        values = dict(self.config_manager.config.get('schedule', {}))
        self.next_run = None
        self._reminded = False
        self._previous_remaining = None
        if not values.get('enabled'):
            if values.get('next_run'):
                values['next_run'] = ''; self._save(values)
            return
        time_text = values.get('time', '23:00')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_text) or values.get('mode') not in {'once', 'daily'}:
            self.log('定时配置无效，请重新选择时间和模式', 'error')
            return
        now = self.now()
        try:
            candidate = datetime.fromisoformat(values.get('next_run') or '') if not reset else None
            if candidate is not None and (candidate.tzinfo is not None or candidate.strftime('%H:%M') != time_text):
                candidate = None
        except ValueError:
            candidate = None
        try:
            consumed = datetime.fromisoformat(values.get('last_run') or '')
            if consumed.tzinfo is not None:
                consumed = None
        except ValueError:
            consumed = None
        if candidate is not None and consumed is not None and candidate <= consumed:
            candidate = None
        if candidate is not None and candidate <= now:
            self.log('软件关闭期间错过的定时不补交，重新安排下次执行', 'warning')
            if values['mode'] == 'once':
                values.update(enabled=False, next_run='')
                self._save(values); return
            candidate = None
        # Clock calibration can move backwards. Never re-arm an occurrence already
        # consumed, even if the persisted future occurrence is being rebuilt.
        lower_bound = max(now, consumed) if consumed is not None else now
        candidate = candidate or self._next_time(lower_bound, time_text)
        values['next_run'] = candidate.isoformat(timespec='seconds')
        if self._save(values):
            self.next_run = candidate
            self._previous_remaining = (candidate - now).total_seconds()

    def tick(self, busy=False):
        values = dict(self.config_manager.config.get('schedule', {}))
        if not values.get('enabled') or self.next_run is None:
            return False
        now = self.now()
        remaining = (self.next_run - now).total_seconds()
        if remaining > 0:
            if not self._reminded and self._previous_remaining is not None and self._previous_remaining >= 300 >= remaining and remaining >= 240:
                self.log('距离定时开始还有5分钟', 'info')
                self._reminded = True
            self._previous_remaining = remaining
            return False
        occurrence = self.next_run
        following = self._next_time(now, values['time']) if values['mode'] == 'daily' else None
        values.update(last_run=occurrence.isoformat(timespec='seconds'), next_run=following.isoformat(timespec='seconds') if following else '')
        if values['mode'] == 'once':
            values['enabled'] = False
        # The disk checkpoint is the gate: a crash or restart cannot submit twice.
        if not self._save(values):
            self.next_run = None
            return False
        self.next_run = following
        self._reminded = False
        self._previous_remaining = (following - now).total_seconds() if following else None
        if busy:
            self.log('定时已到，但有任务正在运行，跳过本次定时执行', 'warning')
            return False
        self.log('定时已到，自动开始生成', 'info')
        return True

    def next_text(self):
        return '下次定时执行：' + (self.next_run.strftime('%Y-%m-%d %H:%M') if self.next_run else '未启用')
