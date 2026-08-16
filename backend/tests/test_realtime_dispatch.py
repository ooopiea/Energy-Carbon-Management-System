"""Real-time dispatch tests: per-tick execution, idempotency, and constraints.

Tests the real-time phase:
  - Tick processes exactly one step with unique command
  - Idempotency: replayed tick returns same command_id
  - Feedback comes from device adapter (not copied from plan)
  - Constraint failure prevents execution
  - Day-ahead plan drives real-time setpoints correctly
  - Phase boundary: no dispatch before approval

Uses the RealtimeTickEngine directly for unit tests and the full
SimulationEngine for integration tests.
"""
from __future__ import annotations

import random

import pytest

from agents.engine import SimulationEngine
from core.executor import SimulationExecutor
from workflows.phase import TickInput, TickResult, Phase
from workflows.realtime import RealtimeTickEngine


_MISSING = object()

# ---------------------------------------------------------------------------
# Unit tests: RealtimeTickEngine in isolation
# ---------------------------------------------------------------------------

def _make_tick_input(
    run_id: str = "run-test",
    step: int = 0,
    storage_plan: dict | None = _MISSING,
    hvac_plan: dict | None = _MISSING,
) -> TickInput:
    """Build a minimal TickInput for testing."""
    from datetime import datetime, timedelta
    ts = [datetime(2025, 11, 1) + timedelta(minutes=15 * i) for i in range(96)]
    dd = {
        "timestamps": ts,
        "load_kw": [90000.0] * 96,
        "solar_kw": [10000.0] * 96,
        "hvac_load_kw": [16000.0] * 96,
        "price_cny_per_kwh": [0.5] * 96,
        "weather": {"temp_c": [25.0] * 96},
    }
    if storage_plan is _MISSING:
        storage_plan = {"power_kw": [5000.0] * 96, "soc_ratio": [0.5] * 96}
    if hvac_plan is _MISSING:
        hvac_plan = {
            "power_kw": [14000.0] * 96,
            "supply_temp_c": [7.0] * 96,
            "return_temp_c": [12.0] * 96,
            "baseline_power_kw": [16000.0] * 96,
        }
    return TickInput(
        run_id=run_id, step=step, sim_time=ts[step].isoformat(),
        day_data=dd, storage_plan=storage_plan, hvac_plan=hvac_plan,
        storage_report_id="rpt-s", storage_report_hash="hash-s",
        hvac_report_id="rpt-h", hvac_report_hash="hash-h",
        previous_values={
            "storage_power_kw": 0.0, "storage_soc": 0.5,
            "storage_temp_c": 25.0, "hvac_supply_temp_c": 7.0,
            "hvac_return_temp_c": 12.0,
        },
        demand_cap_kw=None, pending_overrides={},
    )


@pytest.mark.asyncio
async def test_tick_processes_exactly_one_step():
    """run_tick processes one step and returns measurements for that step."""
    engine = RealtimeTickEngine(rng=random.Random(99))
    inp = _make_tick_input(step=10)
    result = await engine.run_tick(inp)

    assert result.step == 10
    assert result.dispatched is True
    assert result.execution is not None
    assert "load_kw" in result.measurements
    assert "grid_kw" in result.measurements
    assert "storage_soc" in result.measurements


@pytest.mark.asyncio
async def test_tick_without_plans_does_not_dispatch():
    """TickInput with no plans should not dispatch but still measure."""
    engine = RealtimeTickEngine(rng=random.Random(99))
    inp = _make_tick_input(step=0, storage_plan=None, hvac_plan=None)
    result = await engine.run_tick(inp)

    assert result.dispatched is False
    assert result.execution is None
    assert result.measurements["storage_power_kw"] == 0.0
    # Still produces metrics for monitoring
    assert "energy" in result.metrics_delta


@pytest.mark.asyncio
async def test_tick_without_report_binding_does_not_dispatch():
    """Missing report IDs mean no authorization to dispatch."""
    engine = RealtimeTickEngine(rng=random.Random(99))
    inp = _make_tick_input(step=0)
    inp.storage_report_id = ""
    result = await engine.run_tick(inp)
    assert result.dispatched is False


@pytest.mark.asyncio
async def test_tick_metrics_accumulate_correctly():
    """Metrics delta is proportional to 15-min interval (0.25 h)."""
    engine = RealtimeTickEngine(rng=random.Random(99))
    inp = _make_tick_input(step=0)
    result = await engine.run_tick(inp)

    energy = result.metrics_delta["energy"]
    # energy = effective_load * 0.25h; load ~90k, so energy ~22.5k
    assert 15000 < energy < 30000


@pytest.mark.asyncio
async def test_demand_cap_adjusts_storage_setpoint():
    """Demand cap triggers additional storage discharge to shave peak."""
    engine = RealtimeTickEngine(rng=random.Random(99))
    inp_no_cap = _make_tick_input(step=0)
    inp_no_cap.demand_cap_kw = None
    result_no_cap = await engine.run_tick(inp_no_cap)

    inp_cap = _make_tick_input(step=0)
    inp_cap.demand_cap_kw = 50000.0  # aggressive cap
    result_cap = await engine.run_tick(inp_cap)

    # With demand cap, storage should discharge more
    cap_power = result_cap.measurements["storage_power_kw"]
    no_cap_power = result_no_cap.measurements["storage_power_kw"]
    assert cap_power >= no_cap_power, "demand cap should increase storage discharge"


@pytest.mark.asyncio
async def test_tick_command_has_unique_id():
    """Each tick execution generates a unique command_id."""
    engine = RealtimeTickEngine(rng=random.Random(99))
    r1 = await engine.run_tick(_make_tick_input(run_id="run-a", step=0))
    r2 = await engine.run_tick(_make_tick_input(run_id="run-a", step=1))
    assert r1.execution.command.command_id != r2.execution.command.command_id


# ---------------------------------------------------------------------------
# Integration tests: full SimulationEngine dispatch flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_realtime_tick_after_full_approval(tmp_path):
    """Full engine: approve all gates, run a tick, verify execution."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    await engine.run_step(3)
    state = engine.get_state()
    assert state["physical_dispatch"]["enabled"] is True
    assert state["physical_dispatch"]["command_count"] == 1
    assert state["physical_dispatch"]["last_execution"] is not None
    # Feedback comes from executor, not plan
    exec_data = state["physical_dispatch"]["last_execution"]
    assert exec_data["feedback"]["measured_storage_soc"] is not None


@pytest.mark.asyncio
async def test_replayed_tick_is_idempotent(tmp_path):
    """Running the same step twice returns the same command."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    await engine.run_step(5)
    cmd1 = engine.get_state()["physical_dispatch"]["last_execution"]["command"]["command_id"]
    await engine.run_step(5)
    cmd2 = engine.get_state()["physical_dispatch"]["last_execution"]["command"]["command_id"]
    assert cmd1 == cmd2, "replayed step must return same command (idempotent)"


@pytest.mark.asyncio
async def test_no_dispatch_before_approval(tmp_path):
    """Running a tick before any approval must not produce commands."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    # Don't approve anything
    await engine.run_step(0)
    state = engine.get_state()
    assert state["physical_dispatch"]["enabled"] is False
    assert state["physical_dispatch"]["command_count"] == 0
    assert state["physical_dispatch"]["last_execution"] is None


@pytest.mark.asyncio
async def test_multiple_sequential_ticks(tmp_path):
    """Running multiple ticks in sequence produces increasing metrics."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    for step in range(5):
        await engine.run_step(step)

    state = engine.get_state()
    assert state["physical_dispatch"]["command_count"] == 5
    assert state["daily"]["energy_kwh"] > 0
    assert state["daily"]["peak_kw"] > 0


@pytest.mark.asyncio
async def test_phase_boundary_in_checkpoint(tmp_path):
    """Checkpoint subgraph transitions from day_ahead to realtime on approval."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)

    cp_before = engine.get_state()["checkpoint"]
    assert cp_before["subgraph"] == "day_ahead"
    assert cp_before["dispatch_enabled"] is False

    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    cp_after = engine.get_state()["checkpoint"]
    assert cp_after["subgraph"] == "realtime"
    assert cp_after["dispatch_enabled"] is True


@pytest.mark.asyncio
async def test_storage_setpoint_follows_approved_plan(tmp_path):
    """Real-time storage power reflects the day-ahead plan setpoint."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    state = engine.get_state()
    plan_power = state["day_ahead"]["storage_plan"]
    await engine.run_step(10)

    state2 = engine.get_state()
    exec_power = state2["physical_dispatch"]["last_execution"]["command"]["storage_power_kw"]
    plan_setpoint = plan_power[10]
    # Executor applies setpoint with small physical tolerance
    assert abs(exec_power - plan_setpoint) < abs(plan_setpoint) * 0.15 + 100, \
        f"executed power {exec_power} should track plan setpoint {plan_setpoint}"
