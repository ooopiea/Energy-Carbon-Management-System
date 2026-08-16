from __future__ import annotations

import json

import pytest

from agents.engine import SimulationEngine


@pytest.mark.asyncio
async def test_pending_approvals_never_execute_dispatch(tmp_path):
    engine = SimulationEngine(archive_root=tmp_path)

    await engine.start_day(0)
    state = engine.get_state()
    assert state["approval_gates"]["forecast_approval"]["status"] == "pending_approval"
    assert state["day_ahead"]["storage_plan"] == []
    assert state["day_ahead"]["hvac_plan"] == []

    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    state = engine.get_state()
    assert len(state["day_ahead"]["storage_plan"]) == 96
    assert len(state["day_ahead"]["hvac_plan"]) == 96
    assert state["approval_gates"]["storage_approval"]["status"] == "pending_approval"
    assert state["approval_gates"]["hvac_approval"]["status"] == "pending_approval"

    await engine.run_step(1)
    state = engine.get_state()
    assert state["physical_dispatch"]["enabled"] is False
    assert state["physical_dispatch"]["last_execution"] is None
    assert state["storage_power_kw"] == 0


@pytest.mark.asyncio
async def test_both_dispatch_approvals_are_required_for_acknowledged_execution(tmp_path):
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.submit_approval("storage_approval", "approve", actor="tester")

    await engine.run_step(1)
    assert engine.get_state()["physical_dispatch"]["last_execution"] is None

    await engine.submit_approval("hvac_approval", "approve", actor="tester")
    await engine.run_step(2)
    execution = engine.get_state()["physical_dispatch"]["last_execution"]
    assert execution["ack"]["accepted"] is True
    assert execution["command"]["step"] == 2
    assert execution["feedback"]["command_id"] == execution["command"]["command_id"]


@pytest.mark.asyncio
async def test_rejected_forecast_cannot_create_or_execute_plans(tmp_path):
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)

    await engine.submit_approval("forecast_approval", "reject", comment="bad forecast", actor="tester")
    await engine.run_step(1)
    state = engine.get_state()

    assert state["workflow"]["status"] == "rejected"
    assert state["day_ahead"]["storage_plan"] == []
    assert state["day_ahead"]["hvac_plan"] == []
    assert state["physical_dispatch"]["last_execution"] is None


@pytest.mark.asyncio
async def test_gate_is_bound_to_archived_report_id_and_hash(tmp_path):
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    state = engine.get_state()
    gate = state["approval_gates"]["forecast_approval"]

    assert gate["report_id"]
    assert gate["report_hash"]
    assert gate["report"]["report_id"] == gate["report_id"]
    assert gate["report"]["content_hash"] == gate["report_hash"]

    report_files = list(tmp_path.rglob("reports/*.json"))
    assert report_files
    archived = [json.loads(path.read_text(encoding="utf-8")) for path in report_files]
    assert any(item["payload"]["content_hash"] == gate["report_hash"] for item in archived)


@pytest.mark.asyncio
async def test_reset_clears_runtime_and_starts_fresh_pending_workflow(tmp_path):
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.submit_approval("hvac_approval", "approve", actor="tester")
    await engine.run_step(2)
    assert engine.get_state()["series"]

    await engine.reset()
    state = engine.get_state()

    assert state["time"]["step"] == 0
    assert state["daily"] == {
        "energy_kwh": 0.0,
        "cost_cny": 0.0,
        "carbon_kg": 0.0,
        "peak_kw": 0.0,
    }
    assert state["series"] == {}
    assert state["storage_soc"] == pytest.approx(0.10)
    assert state["physical_dispatch"]["last_execution"] is None
    assert state["approval_gates"]["forecast_approval"]["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_promoted_pending_plan_syncs_realtime_soc(tmp_path):
    """Cross-day model: auto-approved D+1 plan promotes on start_day, syncing SOC."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    engine._current_values["storage_soc"] = 0.37

    await engine.start_day(1)
    state = engine.get_state()
    summary = state["storage_summary"]
    # Promoted plan syncs SOC to the plan's initial_soc immediately.
    assert state["storage_soc"] == pytest.approx(summary["initial_soc"])
    assert summary["initial_soc"] == pytest.approx(0.10)
    assert summary["terminal_soc_target"] == pytest.approx(0.10)
    # Promotion activates dispatch and all gates.
    assert state["physical_dispatch"]["enabled"] is True
    for gate in ("forecast_approval", "storage_approval", "hvac_approval"):
        assert state["approval_gates"][gate]["status"] == "approved"


@pytest.mark.asyncio
async def test_fallback_path_carries_over_realtime_soc(tmp_path):
    """When no approved pending plan exists, SOC carries over until forecast approval."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    engine._current_values["storage_soc"] = 0.37
    # Simulate unapproved / missing pending plan — force the fallback path.
    engine._pending_day_plan = None

    await engine.start_day(1)
    # Fallback path has no storage plan yet, so carryover SOC is preserved.
    assert engine.get_state()["storage_soc"] == pytest.approx(0.37)

    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    state = engine.get_state()
    summary = state["storage_summary"]
    # After the storage agent runs, realtime SOC aligns to the plan's initial_soc.
    assert state["storage_soc"] == pytest.approx(summary["initial_soc"])
    assert summary["initial_soc"] == pytest.approx(0.10)
    assert summary["terminal_soc_target"] == pytest.approx(0.10)
