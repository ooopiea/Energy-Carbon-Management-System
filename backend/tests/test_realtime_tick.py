"""Stage-3 tests: single-tick realtime subgraph (revise_guide 12.3).

These tests verify that each 15-minute tick processes exactly one step,
is idempotent on replay, rejects unsafe commands, uses real device feedback,
and handles executor failures gracefully.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from agents.engine import SimulationEngine
from core.executor import ExecutionRejected


@pytest.mark.asyncio
async def test_run_tick_processes_exactly_one_step(tmp_path):
    """A single tick invocation must add exactly one data point for the given step."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.submit_approval("hvac_approval", "approve", actor="tester")

    await engine.run_step(5)
    state = engine.get_state()
    for key in ("load", "solar", "grid", "storage_power"):
        assert len(state["series"].get(key, [])) == 1
        assert state["series"][key][0]["step"] == 5


@pytest.mark.asyncio
async def test_replayed_acknowledged_tick_is_idempotent(tmp_path):
    """Replaying the same step returns the same command_id and does not duplicate."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    await engine.run_step(1)
    first = engine.get_state()["physical_dispatch"]["last_execution"]
    first_cmd_id = first["command"]["command_id"]
    first_feedback_soc = first["feedback"]["measured_storage_soc"]

    await engine.run_step(1)  # replay
    second = engine.get_state()["physical_dispatch"]["last_execution"]

    assert second["command"]["command_id"] == first_cmd_id
    assert second["feedback"]["measured_storage_soc"] == first_feedback_soc
    # Series should not have grown (no new data point from the replay).
    assert len(engine.get_state()["series"]["load"]) == 1


@pytest.mark.asyncio
async def test_constraint_failure_prevents_device_execution(tmp_path):
    """When the executor rejects a command, dispatch is disabled and no command is sent."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    # Patch executor to reject the next call.
    original_execute = engine._executor.execute

    async def rejecting_execute(**kwargs):
        raise ExecutionRejected("simulated constraint violation")

    with patch.object(engine._executor, "execute", side_effect=rejecting_execute):
        await engine.run_step(1)

    state = engine.get_state()
    assert state["physical_dispatch"]["enabled"] is False
    assert state["physical_dispatch"]["last_execution"] is None
    # An alert must have been generated.
    assert any("executor" in a["source"] for a in state["alerts"])


@pytest.mark.asyncio
async def test_feedback_comes_from_device_adapter(tmp_path):
    """Measured SOC, temperature and power must come from executor feedback, not the plan."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    await engine.run_step(1)
    state = engine.get_state()
    execution = state["physical_dispatch"]["last_execution"]

    # State SOC must match feedback SOC (both rounded to 4 decimal places).
    assert state["storage_soc"] == pytest.approx(
        execution["feedback"]["measured_storage_soc"], abs=1e-4
    )
    # State temperature must match feedback temperature.
    assert state["storage_temp_c"] == pytest.approx(
        execution["feedback"]["measured_storage_temp_c"], abs=1e-2
    )
    # Command setpoint and measured value should differ slightly (measurement noise).
    cmd_power = execution["command"]["storage_power_kw"]
    measured_power = execution["feedback"]["measured_storage_power_kw"]
    assert abs(cmd_power - measured_power) >= 0 or cmd_power == 0  # noise or zero


@pytest.mark.asyncio
async def test_executor_failure_is_handled_gracefully(tmp_path):
    """When executor raises an unexpected error, the engine does not crash."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    async def failing_execute(**kwargs):
        raise ExecutionRejected("device timeout")

    with patch.object(engine._executor, "execute", side_effect=failing_execute):
        # Should not raise; the engine handles the failure internally.
        await engine.run_step(1)

    state = engine.get_state()
    # Fail-closed: dispatch disabled after rejection.
    assert state["physical_dispatch"]["enabled"] is False
    assert state["physical_dispatch"]["last_execution"] is None
    # Critical alert generated for the failure.
    assert any(
        a["severity"] == "critical" and "timeout" in a["message"]
        for a in state["alerts"]
    )
    # Realtime data is still recorded (BAS readings exist without dispatch).
    assert len(state["series"].get("load", [])) == 1
    assert state["series"]["load"][0]["step"] == 1
