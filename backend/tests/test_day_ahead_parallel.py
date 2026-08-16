"""Stage-2 tests: day-ahead parallel branches and local revision (revise_guide 12.2).

These tests prove that storage and HVAC form true parallel branches with
independent approval gates, and that revising one branch does not recompute
the other.
"""
from __future__ import annotations

import pytest

from agents.engine import ApprovalBindingMismatch, SimulationEngine
from graph.spec import WORKFLOW_SPEC


@pytest.mark.asyncio
async def test_storage_and_hvac_branches_have_no_order_dependency(tmp_path):
    """After forecast approval, both plans exist - no sequential dependency."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    state = engine.get_state()

    assert len(state["day_ahead"]["storage_plan"]) == 96
    assert len(state["day_ahead"]["hvac_plan"]) == 96
    assert state["approval_gates"]["storage_approval"]["status"] == "pending_approval"
    assert state["approval_gates"]["hvac_approval"]["status"] == "pending_approval"

    # Structural: no sequential execution edge from storage to hvac in the spec.
    exec_edges = [
        e for e in WORKFLOW_SPEC.edges
        if e.is_execution_edge and e.from_node == "storage_agent" and e.to_node == "hvac_agent"
    ]
    assert exec_edges == [], "storage -> hvac sequential edge must not exist"


@pytest.mark.asyncio
async def test_storage_revision_does_not_recompute_hvac(tmp_path):
    """Revising storage regenerates only the storage report; HVAC is untouched."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")

    original_hvac_report_id = engine.get_state()["approval_gates"]["hvac_approval"]["report_id"]
    original_storage_report_id = engine.get_state()["approval_gates"]["storage_approval"]["report_id"]

    # Revise storage approval.
    gate = await engine.submit_approval("storage_approval", "revise", comment="adjust", actor="tester")
    state = engine.get_state()

    # Storage report must be regenerated (new report_id).
    assert gate.report_id != original_storage_report_id
    assert gate.status == "pending_approval"

    # HVAC report must be unchanged.
    assert state["approval_gates"]["hvac_approval"]["report_id"] == original_hvac_report_id


@pytest.mark.asyncio
async def test_hvac_revision_does_not_recompute_storage(tmp_path):
    """Revising HVAC regenerates only the HVAC report; storage is untouched."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")

    original_storage_report_id = engine.get_state()["approval_gates"]["storage_approval"]["report_id"]
    original_hvac_report_id = engine.get_state()["approval_gates"]["hvac_approval"]["report_id"]

    # Revise HVAC approval.
    gate = await engine.submit_approval("hvac_approval", "revise", comment="adjust", actor="tester")
    state = engine.get_state()

    # HVAC report must be regenerated.
    assert gate.report_id != original_hvac_report_id
    assert gate.status == "pending_approval"

    # Storage report must be unchanged.
    assert state["approval_gates"]["storage_approval"]["report_id"] == original_storage_report_id


@pytest.mark.asyncio
async def test_join_requires_two_current_approvals(tmp_path):
    """Physical dispatch requires both storage and HVAC approvals to pass."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")

    # Only storage approved.
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.run_step(1)
    assert engine.get_state()["physical_dispatch"]["enabled"] is False
    assert engine.get_state()["physical_dispatch"]["last_execution"] is None

    # Now HVAC approved too.
    await engine.submit_approval("hvac_approval", "approve", actor="tester")
    await engine.run_step(2)
    execution = engine.get_state()["physical_dispatch"]["last_execution"]
    assert execution is not None
    assert execution["ack"]["accepted"] is True


@pytest.mark.asyncio
async def test_old_report_binding_cannot_authorize_dispatch(tmp_path):
    """After storage revision, the old report_id is rejected at the approval gate."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")

    old_storage_report_id = engine.get_state()["approval_gates"]["storage_approval"]["report_id"]

    # Revise storage to generate a new report.
    await engine.submit_approval("storage_approval", "revise", comment="adjust", actor="tester")
    new_storage_report_id = engine.get_state()["approval_gates"]["storage_approval"]["report_id"]
    assert new_storage_report_id != old_storage_report_id

    # Attempting to approve with the old report_id must fail.
    with pytest.raises(ApprovalBindingMismatch, match="报告 ID"):
        await engine.submit_approval(
            "storage_approval", "approve", actor="tester",
            report_id=old_storage_report_id,
        )

    # Approving with the new report_id works.
    await engine.submit_approval(
        "storage_approval", "approve", actor="tester",
        report_id=new_storage_report_id,
    )
    assert engine.get_state()["approval_gates"]["storage_approval"]["status"] == "approved"