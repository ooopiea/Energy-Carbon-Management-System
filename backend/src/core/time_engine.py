"""Monotonic 200x simulation clock with atomic pause/resume/reset semantics."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from core.config import SIM_STEP_MINUTES, SIMULATION_START_DATE, TIME_SCALE


class TimeEngine:
    """Thread-safe simulation clock; one 15-minute step is 4.5 real seconds at 200x."""

    def __init__(
        self,
        start_time: datetime | None = None,
        time_scale: int = TIME_SCALE,
        step_minutes: int = SIM_STEP_MINUTES,
        loop_end_time: datetime | None = None,
    ):
        if time_scale <= 0:
            raise ValueError("time_scale must be positive")
        if step_minutes <= 0 or 1440 % step_minutes:
            raise ValueError("step_minutes must divide one day")
        self._sim_start = start_time or datetime.combine(SIMULATION_START_DATE, datetime.min.time())
        self._loop_end_time = loop_end_time
        if loop_end_time is not None and loop_end_time <= self._sim_start:
            raise ValueError("loop_end_time must be later than start_time")
        self._time_scale = time_scale
        self._step_minutes = step_minutes
        self._points_per_day = 1440 // step_minutes
        self._lock = threading.RLock()
        self._anchor_real = time.monotonic()
        self._elapsed_real_seconds = 0.0
        self._paused = False

    def _elapsed_real(self, now: float) -> float:
        elapsed = self._elapsed_real_seconds
        if not self._paused:
            elapsed += now - self._anchor_real
        return max(0.0, elapsed)

    def _snapshot_values(self) -> tuple[datetime, int, int, float, int]:
        with self._lock:
            now = time.monotonic()
            elapsed_sim = timedelta(
                seconds=self._elapsed_real(now) * self._time_scale
            )
            cycle = 0
            sim_time = self._sim_start + elapsed_sim
            if self._loop_end_time is not None:
                loop_duration = self._loop_end_time - self._sim_start
                cycle = int(elapsed_sim // loop_duration)
                sim_time = self._sim_start + (elapsed_sim % loop_duration)
            minutes = sim_time.hour * 60 + sim_time.minute
            step = min(self._points_per_day - 1, minutes // self._step_minutes)
            day = (sim_time.date() - self._sim_start.date()).days
            progress = step / float(self._points_per_day)
            return sim_time, int(step), day, progress, cycle

    @property
    def sim_time(self) -> datetime:
        return self._snapshot_values()[0]

    @property
    def current_step(self) -> int:
        return self._snapshot_values()[1]

    @property
    def day_count(self) -> int:
        return self._snapshot_values()[2]

    @property
    def cycle_count(self) -> int:
        return self._snapshot_values()[4]

    @property
    def sim_step_seconds(self) -> float:
        return (self._step_minutes * 60) / self._time_scale

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def progress_ratio(self) -> float:
        return self._snapshot_values()[3]

    def pause(self) -> None:
        with self._lock:
            if self._paused:
                return
            now = time.monotonic()
            self._elapsed_real_seconds += now - self._anchor_real
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            if not self._paused:
                return
            self._anchor_real = time.monotonic()
            self._paused = False

    def reset(self, start_time: datetime | None = None) -> None:
        """Atomically return to step zero; the owning SimulationEngine resets domain state."""
        with self._lock:
            if start_time is not None:
                self._sim_start = start_time
            self._anchor_real = time.monotonic()
            self._elapsed_real_seconds = 0.0
            self._paused = False

    def configure_loop(self, end_time: datetime) -> None:
        """Set the exclusive endpoint at which the clock wraps to its start."""
        with self._lock:
            if end_time <= self._sim_start:
                raise ValueError("loop end time must be later than start time")
            self._loop_end_time = end_time

    def tick_info(self) -> dict:
        sim_time, step, day, progress, cycle = self._snapshot_values()
        with self._lock:
            paused = self._paused
            loop_end_time = self._loop_end_time
        return {
            "sim_time": sim_time.isoformat(),
            "step": step,
            "day": day,
            "hour": sim_time.hour + sim_time.minute / 60.0,
            "progress": progress,
            "paused": paused,
            "time_scale": self._time_scale,
            "cycle": cycle,
            "loop_enabled": loop_end_time is not None,
            "loop_end": loop_end_time.isoformat() if loop_end_time else None,
        }


_engine: TimeEngine | None = None
_engine_lock = threading.Lock()


def get_time_engine(
    start_time: datetime | None = None,
    loop_end_time: datetime | None = None,
) -> TimeEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = TimeEngine(start_time=start_time, loop_end_time=loop_end_time)
    elif loop_end_time is not None:
        with _engine_lock:
            _engine.configure_loop(loop_end_time)
    return _engine
