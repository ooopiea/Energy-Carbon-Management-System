from __future__ import annotations

from datetime import datetime, timedelta

from agents.engine import SimulationEngine
from data.raw_loader import get_load_data_range


class LoopClock:
    def __init__(self, start: datetime, final_day: int) -> None:
        self.start = start
        self.day = final_day
        self.step = 95
        self.cycle = 0
        self.paused = False

    @property
    def sim_time(self) -> datetime:
        return self.start + timedelta(days=self.day, minutes=15 * self.step)

    @property
    def current_step(self) -> int:
        return self.step

    @property
    def day_count(self) -> int:
        return self.day

    @property
    def cycle_count(self) -> int:
        return self.cycle

    @property
    def is_paused(self) -> bool:
        return self.paused

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def reset(self) -> None:
        self.day = 0
        self.step = 0
        self.cycle = 0

    def tick_info(self) -> dict:
        return {
            "sim_time": self.sim_time.isoformat(),
            "step": self.step,
            "day": self.day,
            "progress": self.step / 96,
            "paused": self.paused,
            "cycle": self.cycle,
            "loop_enabled": True,
        }


async def test_cycle_rollover_restarts_first_day_as_new_run(tmp_path) -> None:
    data_start, data_end, _ = get_load_data_range()
    assert data_start is not None and data_end is not None
    start = datetime.combine(data_start, datetime.min.time())
    final_day = (data_end - data_start).days
    clock = LoopClock(start, final_day)
    engine = SimulationEngine(
        archive_root=tmp_path,
        time_engine=clock,
        simulation_loop=True,
    )

    await engine.start_day(final_day)
    first_run_id = engine.get_state()["workflow"]["run_id"]
    assert engine.get_state()["pending_day_plan"] is None

    clock.day = 0
    clock.step = 0
    clock.cycle = 1
    await engine.run_tick()
    state = engine.get_state()

    assert state["time"]["day"] == 0
    assert state["time"]["cycle"] == 1
    assert state["workflow"]["run_id"] != first_run_id
    assert state["series"]
    assert all(points[0]["step"] == 0 for points in state["series"].values())
    assert state["approval_gates"]["forecast_approval"]["status"] == "pending_approval"
    assert engine.health()["simulation_loop"]["cycle"] == 1
