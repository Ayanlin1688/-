"""Thread-safe model selection and cooldown tracking."""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class _ModelState:
    name: str
    cooldown_deadline: float = 0.0
    consecutive_failures: int = 0


class ModelPool:
    """Select configured models and quarantine repeatedly failing ones."""

    def __init__(
        self,
        settings: dict[str, Any],
        default_model: str,
        failure_threshold: int = 3,
        log: Callable[[str, str], None] | None = None,
        on_change: Callable[[list[dict[str, Any]]], None] | None = None,
        now: Callable[[], float] | None = None,
        wall: Callable[[], float] | None = None,
        rng: Any = None,
    ) -> None:
        settings = settings or {}
        self.enabled = bool(settings.get('enabled', False))
        self._strategy = settings.get('strategy', '轮询')
        self._cooldown = max(0.0, float(settings.get('cooldown', 30)))
        self._failure_threshold = max(1, int(failure_threshold))
        self._log = log
        self._on_change = on_change
        self._now = now or time.monotonic
        self._wall = wall or time.time
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._notification_lock = threading.Lock()
        self._notification_queue: list[list[dict[str, Any]]] = []
        self._notifying = False
        self._revision = 0
        self._notified_revision = 0
        self._cursor = 0

        entries: list[dict[str, Any]]
        if self.enabled:
            entries = [entry for entry in settings.get('models', []) if isinstance(entry, dict)]
        else:
            entries = [dict(name=default_model, enabled=True, status='健康')]

        monotonic_now = self._now()
        wall_now = self._wall()
        seen = set()
        self._states: dict[str, _ModelState] = {}
        self.names: list[str] = []
        for entry in entries:
            if not entry.get('enabled', False):
                continue
            name = entry.get('name')
            if not isinstance(name, str) or not name or name in seen:
                continue
            seen.add(name)
            state = _ModelState(name)
            if entry.get('status') == '冷却中':
                persisted = entry.get('cooldown_until')
                if persisted is None:
                    remaining = self._cooldown
                else:
                    try:
                        remaining = max(0.0, float(persisted) - wall_now)
                    except (TypeError, ValueError):
                        remaining = self._cooldown
                if remaining > 0:
                    state.cooldown_deadline = monotonic_now + remaining
            self.names.append(name)
            self._states[name] = state

    def pick(self, after: str | None = None, preferred: str | None = None, preferred_names=None) -> str | None:
        recovered: list[str]
        change_snapshot = None
        with self._lock:
            self._require_models_locked()
            current = self._now()
            recovered = self._refresh_locked(current)
            if recovered:
                change_snapshot = self._snapshot_locked(current, self._wall())

            compatible = [name for name in (preferred_names or [])
                          if name in self._states and self._eligible(self._states[name], current)]
            if preferred is not None:
                state = self._states.get(preferred)
                selected = preferred if state is not None and self._eligible(state, current) else None
            elif compatible:
                selected = compatible[0]
            elif after in self._states:
                selected = self._pick_after_locked(after, current)
            else:
                selected = self._pick_by_strategy_locked(current)

        self._publish(recovered, change_snapshot)
        return selected

    def wait_seconds(self, preferred: str | None = None) -> float:
        recovered: list[str]
        change_snapshot = None
        with self._lock:
            self._require_models_locked()
            current = self._now()
            recovered = self._refresh_locked(current)
            if recovered:
                change_snapshot = self._snapshot_locked(current, self._wall())

            if preferred is not None:
                state = self._states.get(preferred)
                remaining = 0.0 if state is None else self._remaining(state, current)
            else:
                waits = [self._remaining(self._states[name], current) for name in self.names]
                remaining = 0.0 if any(wait == 0 for wait in waits) else min(waits)

        self._publish(recovered, change_snapshot)
        return remaining

    def failed(self, name: str, force_cooldown: bool = False) -> float:
        recovered: list[str]
        with self._lock:
            self._require_models_locked()
            state = self._state_locked(name)
            current = self._now()
            recovered = self._refresh_locked(current)
            state.consecutive_failures += 1
            if force_cooldown or state.consecutive_failures >= self._failure_threshold:
                state.cooldown_deadline = current + self._cooldown
            remaining = self._remaining(state, current)
            change_snapshot = self._snapshot_locked(current, self._wall())

        self._publish(recovered, change_snapshot)
        return remaining

    def succeeded(self, name: str) -> None:
        recovered: list[str]
        change_snapshot = None
        with self._lock:
            self._require_models_locked()
            state = self._state_locked(name)
            current = self._now()
            recovered = self._refresh_locked(current)
            changed = bool(recovered)
            if state.consecutive_failures:
                state.consecutive_failures = 0
                changed = True
            if changed:
                change_snapshot = self._snapshot_locked(current, self._wall())

        self._publish(recovered, change_snapshot)

    def snapshot(self) -> list[dict[str, Any]]:
        recovered: list[str]
        with self._lock:
            current = self._now()
            recovered = self._refresh_locked(current)
            result = self._snapshot_locked(current, self._wall())
            change_snapshot = result if recovered else None

        self._publish(recovered, change_snapshot)
        return result

    def refresh(self) -> list[dict[str, Any]]:
        recovered: list[str]
        with self._lock:
            current = self._now()
            recovered = self._refresh_locked(current)
            result = self._snapshot_locked(current, self._wall())
            change_snapshot = result if recovered else None

        self._publish(recovered, change_snapshot)
        return result

    def _require_models_locked(self) -> None:
        if not self.names:
            raise ValueError('模型池中没有已启用的模型')

    def _state_locked(self, name: str) -> _ModelState:
        try:
            return self._states[name]
        except KeyError as error:
            raise ValueError(f'模型未在模型池中启用: {name}') from error

    @staticmethod
    def _eligible(state: _ModelState, current: float) -> bool:
        return state.cooldown_deadline <= current

    @staticmethod
    def _remaining(state: _ModelState, current: float) -> float:
        return max(0.0, state.cooldown_deadline - current)

    def _pick_after_locked(self, after: str, current: float) -> str | None:
        source = self.names.index(after)
        for offset in range(1, len(self.names)):
            name = self.names[(source + offset) % len(self.names)]
            if self._eligible(self._states[name], current):
                return name
        return after if self._eligible(self._states[after], current) else None

    def _pick_by_strategy_locked(self, current: float) -> str | None:
        eligible = [name for name in self.names if self._eligible(self._states[name], current)]
        if not eligible:
            return None
        if self._strategy == '随机':
            return self._rng.choice(eligible)
        if self._strategy == '优先级':
            return eligible[0]

        for offset in range(len(self.names)):
            index = (self._cursor + offset) % len(self.names)
            name = self.names[index]
            if self._eligible(self._states[name], current):
                self._cursor = (index + 1) % len(self.names)
                return name
        return None

    def _refresh_locked(self, current: float) -> list[str]:
        recovered = []
        for name in self.names:
            state = self._states[name]
            if state.cooldown_deadline and state.cooldown_deadline <= current:
                state.cooldown_deadline = 0.0
                state.consecutive_failures = 0
                recovered.append(name)
        return recovered

    def _snapshot_locked(self, current: float, wall_now: float) -> list[dict[str, Any]]:
        self._revision += 1
        result = []
        for name in self.names:
            state = self._states[name]
            remaining = self._remaining(state, current)
            result.append({
                'name': name,
                'status': '冷却中' if remaining else '健康',
                'cooldown_until': wall_now + remaining if remaining else 0,
                'remaining': remaining,
                'consecutive_failures': state.consecutive_failures,
                '_revision': self._revision,
            })
        return result

    def _publish(self, recovered: list[str], snapshot: list[dict[str, Any]] | None) -> None:
        if self._log:
            for name in recovered:
                self._log(f'模型 {name} 冷却结束，恢复可用', 'info')
        if self._on_change and snapshot is not None:
            self._notify(snapshot)

    def _notify(self, snapshot: list[dict[str, Any]]) -> None:
        with self._notification_lock:
            # Snapshots can reach this lock in a different order to the state
            # mutations. Never deliver an older revision after a newer one.
            revision = snapshot[0]['_revision'] if snapshot else 0
            if revision <= self._notified_revision:
                return
            self._notified_revision = revision
            self._notification_queue.append(snapshot)
            if self._notifying:
                return
            self._notifying = True

        while True:
            with self._notification_lock:
                if not self._notification_queue:
                    self._notifying = False
                    return
                pending = self._notification_queue.pop(0)
            try:
                self._on_change(pending)
            except BaseException:
                with self._notification_lock:
                    self._notification_queue.clear()
                    self._notifying = False
                raise
