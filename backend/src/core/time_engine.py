"""200x 速时间引擎：系统自主计时，1 现实秒 = 200 模拟秒。

一个完整模拟日（24h = 86400s）仅需 432 现实秒（约 7.2 分钟）。
每次 tick 推进 15 分钟模拟时间（= 4.5 秒现实时间）。
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from core.config import SIM_STEP_MINUTES, TIME_SCALE


class TimeEngine:
    """模拟时钟：基于系统真实时间乘以倍率推进。

    设计为线程安全单例，供 FastAPI 和 LangGraph 共享。
    每次 tick 对应一个 15min 模拟步，推进时回调所有注册的监听器。
    """

    def __init__(
        self,
        start_time: datetime | None = None,
        time_scale: int = TIME_SCALE,
        step_minutes: int = SIM_STEP_MINUTES,
    ):
        # 模拟日从 00:00 开始
        self._sim_start = start_time or datetime(2025, 7, 15, 0, 0, 0)
        self._real_start = datetime.now()
        self._time_scale = time_scale
        self._step_minutes = step_minutes
        self._paused = False
        self._pause_offset = timedelta(0)
        self._pause_start: datetime | None = None
        self._listeners: list[Callable[[datetime, int], None]] = []
        self._async_listeners: list[Callable] = []
        self._lock = threading.Lock()
        self._current_step = 0
        self._day_count = 0

    @property
    def sim_time(self) -> datetime:
        """当前模拟时间。"""
        if self._paused:
            elapsed = self._pause_start - self._real_start - self._pause_offset
        else:
            elapsed = datetime.now() - self._real_start - self._pause_offset
        sim_delta = timedelta(seconds=elapsed.total_seconds() * self._time_scale)
        return self._sim_start + sim_delta

    @property
    def current_step(self) -> int:
        """当前 15min 步索引（0-95）。"""
        t = self.sim_time
        return int(t.hour * 4 + t.minute // self._step_minutes)

    @property
    def day_count(self) -> int:
        """已过去的模拟天数（0=第一天）。"""
        t = self.sim_time
        return (t.date() - self._sim_start.date()).days

    @property
    def sim_step_seconds(self) -> float:
        """一个 15min 模拟步对应的现实秒数。"""
        return (self._step_minutes * 60) / self._time_scale

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def progress_ratio(self) -> float:
        """当天进度（0.0 - 1.0）。"""
        return self.current_step / 96.0

    def pause(self):
        if not self._paused:
            self._pause_start = datetime.now()
            self._paused = True

    def resume(self):
        if self._paused and self._pause_start:
            self._pause_offset += datetime.now() - self._pause_start
            self._paused = False

    def reset(self):
        self._real_start = datetime.now()
        self._pause_offset = timedelta(0)
        self._paused = False
        self._current_step = 0

    def add_listener(self, callback: Callable[[datetime, int], None]):
        with self._lock:
            self._listeners.append(callback)

    def add_async_listener(self, callback):
        with self._lock:
            self._async_listeners.append(callback)

    def tick_info(self) -> dict:
        """返回当前 tick 的完整信息。"""
        t = self.sim_time
        step = self.current_step
        return {
            "sim_time": t.isoformat(),
            "step": step,
            "day": self.day_count,
            "hour": t.hour + t.minute / 60.0,
            "progress": self.progress_ratio,
            "paused": self._paused,
            "time_scale": self._time_scale,
        }


# 全局单例
_engine: TimeEngine | None = None


def get_time_engine() -> TimeEngine:
    global _engine
    if _engine is None:
        _engine = TimeEngine()
    return _engine
