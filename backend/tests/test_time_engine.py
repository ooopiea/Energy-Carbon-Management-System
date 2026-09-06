from __future__ import annotations

from datetime import datetime

from core.time_engine import TimeEngine


def test_loop_clock_wraps_at_exclusive_end() -> None:
    clock = TimeEngine(
        start_time=datetime(2025, 11, 1),
        time_scale=1,
        loop_end_time=datetime(2025, 11, 3),
    )
    clock.pause()

    clock._elapsed_real_seconds = 2 * 86400 - 900
    assert clock.sim_time == datetime(2025, 11, 2, 23, 45)
    assert clock.day_count == 1
    assert clock.current_step == 95
    assert clock.cycle_count == 0

    clock._elapsed_real_seconds = 2 * 86400
    assert clock.sim_time == datetime(2025, 11, 1)
    assert clock.day_count == 0
    assert clock.current_step == 0
    assert clock.cycle_count == 1
    assert clock.tick_info()["loop_enabled"] is True
