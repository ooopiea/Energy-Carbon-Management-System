"""Day-ahead dispatch tests: full planning chain from data to plan handoff.

Tests the day-ahead phase logic:
  data -> prediction -> forecast approval
  -> parallel storage + HVAC optimization
  -> dispatch approvals
  -> DayAheadPlan handoff to real-time

These tests verify that the day-ahead phase produces correct, complete
plans and that the approval chain is properly enforced before any
physical dispatch can occur.
"""
from __future__ import annotations

import pytest

from agents.engine import SimulationEngine
from core.state import NodeStatus
from workflows.day_ahead import (
    DayAheadPhase,
    DayAheadPlan,
    DA_NEW,
    DA_AWAITING_FORECAST,
    DA_DISPATCH_APPROVED,
    DA_ACTIVE,
    DAY_AHEAD_STATUSES,
)


@pytest.mark.asyncio
async def test_day_ahead_starts_in_new_status(tmp_path):
    """start_day triggers data collection and prediction, landing on awaiting approval."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    state = engine.get_state()
    assert state["workflow"]["status"] in (DA_AWAITING_FORECAST, DA_NEW)
    assert DayAheadPhase.is_day_ahead_status(state["workflow"]["status"])
    assert not DayAheadPhase.is_realtime_status(state["workflow"]["status"])


@pytest.mark.asyncio
async def test_day_ahead_produces_data_and_prediction_reports(tmp_path):
    """After start_day, data collection and prediction reports exist."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    state = engine.get_state()

    # Data collection report
    data_reports = [r for r in state["reports"] if r["agent_type"] == "data"]
    assert len(data_reports) > 0, "data collection report must exist"
    assert data_reports[0]["status"] in ("pending", "approved")

    # Prediction report bound to forecast gate
    forecast_gate = state["approval_gates"]["forecast_approval"]
    assert forecast_gate["status"] == "pending_approval"
    assert forecast_gate["report"] is not None
    assert forecast_gate["report_id"] is not None
    assert forecast_gate["report_hash"] is not None


@pytest.mark.asyncio
async def test_forecast_approval_triggers_parallel_branches(tmp_path):
    """Approving forecast triggers both storage and HVAC optimization in parallel."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="engineer")

    state = engine.get_state()

    # Both storage and HVAC gates should now have reports bound
    storage_gate = state["approval_gates"]["storage_approval"]
    hvac_gate = state["approval_gates"]["hvac_approval"]
    assert storage_gate["report"] is not None, "storage report must be produced"
    assert hvac_gate["report"] is not None, "HVAC report must be produced"
    assert storage_gate["status"] == "pending_approval"
    assert hvac_gate["status"] == "pending_approval"
    assert storage_gate["report_id"] != hvac_gate["report_id"], "reports must be distinct"


@pytest.mark.asyncio
async def test_partial_approval_does_not_activate_dispatch(tmp_path):
    """Approving only storage (not HVAC) must not enable physical dispatch."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="engineer")
    await engine.submit_approval("storage_approval", "approve", actor="engineer")

    state = engine.get_state()
    assert state["physical_dispatch"]["enabled"] is False, \
        "dispatch must not activate with only storage approved"
    assert state["approval_gates"]["hvac_approval"]["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_full_approval_activates_dispatch(tmp_path):
    """All three approvals activate physical dispatch and create DayAheadPlan."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="engineer")
    await engine.submit_approval("storage_approval", "approve", actor="engineer")
    await engine.submit_approval("hvac_approval", "approve", actor="engineer")

    state = engine.get_state()
    assert state["physical_dispatch"]["enabled"] is True
    assert state["storage_summary"] is not None
    assert state["hvac_summary"] is not None

    # Day-ahead plans must have required arrays
    storage_plan = state["storage_summary"]
    assert "power_kw" in storage_plan
    assert "soc_ratio" in storage_plan
    assert len(storage_plan["power_kw"]) == 96

    hvac_plan = state["hvac_summary"]
    assert "power_kw" in hvac_plan
    assert "supply_temp_c" in hvac_plan
    assert len(hvac_plan["power_kw"]) == 96


@pytest.mark.asyncio
async def test_day_ahead_plan_handoff_to_realtime(tmp_path):
    """After full approval, the system transitions from day_ahead to realtime phase."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="engineer")
    await engine.submit_approval("storage_approval", "approve", actor="engineer")
    await engine.submit_approval("hvac_approval", "approve", actor="engineer")

    # Check checkpoint subgraph transition
    cp = engine.get_state()["checkpoint"]
    assert cp["dispatch_enabled"] is True

    # Run a real-time step to confirm the plan works
    await engine.run_step(5)
    state = engine.get_state()
    assert state["physical_dispatch"]["command_count"] >= 1


@pytest.mark.asyncio
async def test_reject_resets_to_pending_new_run(tmp_path):
    """Rejecting forecast resets storage and HVAC plans."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "reject", actor="engineer", comment="data issue")

    state = engine.get_state()
    assert state["workflow"]["status"] == "rejected"
    assert state["physical_dispatch"]["enabled"] is False


def test_day_ahead_phase_classification():
    """DayAheadPhase correctly classifies workflow statuses."""
    assert DayAheadPhase.is_day_ahead_status("new")
    assert DayAheadPhase.is_day_ahead_status("awaiting_forecast_approval")
    assert DayAheadPhase.is_day_ahead_status("forecast_approved")
    assert DayAheadPhase.is_day_ahead_status("storage_revision")
    assert DayAheadPhase.is_day_ahead_status("dispatch_approved")
    assert DayAheadPhase.is_day_ahead_status("rejected")

    assert DayAheadPhase.is_realtime_status("active")
    assert not DayAheadPhase.is_realtime_status("new")
    assert not DayAheadPhase.is_day_ahead_status("active")


def test_build_plan_validation():
    """DayAheadPhase.build_plan returns None if any component is missing."""
    # Complete plan
    plan = DayAheadPhase.build_plan(
        run_id="run-test", day=0,
        storage_plan={"power_kw": [0] * 96},
        hvac_plan={"power_kw": [0] * 96},
        storage_report_id="rpt-s", storage_report_hash="hash-s",
        hvac_report_id="rpt-h", hvac_report_hash="hash-h",
    )
    assert plan is not None
    assert plan.run_id == "run-test"
    assert plan.storage_report_id == "rpt-s"

    # Missing plan
    assert DayAheadPhase.build_plan(
        run_id="r", day=0,
        storage_plan=None, hvac_plan={"power_kw": []},
        storage_report_id="s", storage_report_hash="hs",
        hvac_report_id="h", hvac_report_hash="hh",
    ) is None

    # Missing binding
    assert DayAheadPhase.build_plan(
        run_id="r", day=0,
        storage_plan={"power_kw": []}, hvac_plan={"power_kw": []},
        storage_report_id=None, storage_report_hash="hs",
        hvac_report_id="h", hvac_report_hash="hh",
    ) is None


def test_can_activate_dispatch_logic():
    """Dispatch activation requires both gates approved."""
    assert not DayAheadPhase.can_activate_dispatch("idle", "idle")
    assert not DayAheadPhase.can_activate_dispatch("approved", "idle")
    assert not DayAheadPhase.can_activate_dispatch("idle", "approved")
    assert not DayAheadPhase.can_activate_dispatch("approved", "pending_approval")
    assert DayAheadPhase.can_activate_dispatch("approved", "approved")