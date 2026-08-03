"""
Stage-0 safety invariant tests (revise_guide 12.0).

These six invariants must remain true through all subsequent refactoring.
Each test targets one invariant independently so that a regression points
to the exact safety property that was violated.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from agents.engine import (
    ApprovalBindingMismatch,
    SimulationEngine,
)
from core.executor import ExecutionRejected, SimulationExecutor


# ---------------------------------------------------------------------------
# Invariant 1: unapproved dispatch cannot execute
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invariant_1_unapproved_dispatch_cannot_execute(tmp_path):
    """No physical command may leave the system until all three approvals pass."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)

    # Fresh day - nothing approved yet.
    await engine.run_step(1)
    assert engine.get_state()["physical_dispatch"]["enabled"] is False
    assert engine.get_state()["physical_dispatch"]["last_execution"] is None

    # Only forecast approved - storage/HVAC plans exist but dispatch still locked.
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.run_step(2)
    assert engine.get_state()["physical_dispatch"]["enabled"] is False
    assert engine.get_state()["physical_dispatch"]["last_execution"] is None

    # Storage approved, HVAC still pending - still locked.
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.run_step(3)
    assert engine.get_state()["physical_dispatch"]["enabled"] is False
    assert engine.get_state()["physical_dispatch"]["last_execution"] is None

    # Both approved - dispatch unlocked, command executed.
    await engine.submit_approval("hvac_approval", "approve", actor="tester")
    await engine.run_step(4)
    execution = engine.get_state()["physical_dispatch"]["last_execution"]
    assert execution is not None
    assert execution["ack"]["accepted"] is True


# ---------------------------------------------------------------------------
# Invariant 2: stale report binding cannot execute
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invariant_2a_stale_report_id_is_rejected(tmp_path):
    """Submitting approval with a mismatched report_id must raise."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    gate = engine.get_state()["approval_gates"]["forecast_approval"]
    stale_id = gate["report_id"] + "-stale"

    with pytest.raises(ApprovalBindingMismatch, match="报告 ID"):
        await engine.submit_approval(
            "forecast_approval", "approve", actor="tester", report_id=stale_id,
        )


@pytest.mark.asyncio
async def test_invariant_2b_stale_report_hash_is_rejected(tmp_path):
    """Submitting approval with a mismatched report_hash must raise."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    gate = engine.get_state()["approval_gates"]["forecast_approval"]
    stale_hash = "0" * 64

    with pytest.raises(ApprovalBindingMismatch, match="报告哈希"):
        await engine.submit_approval(
            "forecast_approval", "approve", actor="tester",
            report_id=gate["report_id"], report_hash=stale_hash,
        )


@pytest.mark.asyncio
async def test_invariant_2c_executor_rejects_missing_report_binding():
    """The executor itself must reject commands without report binding."""
    executor = SimulationExecutor()
    with pytest.raises(ExecutionRejected, match="报告绑定"):
        await executor.execute(
            run_id="test-run",
            step=0,
            sim_time=datetime(2025, 11, 1, 0, 0),
            storage_power_kw=0.0,
            hvac_power_kw=0.0,
            hvac_supply_temp_c=7.0,
            hvac_return_temp_c=12.0,
            previous_storage_power_kw=0.0,
            previous_storage_soc=0.5,
            previous_storage_temp_c=25.0,
            ambient_temp_c=20.0,
            storage_report_id="",
            storage_report_hash="",
            hvac_report_id="",
            hvac_report_hash="",
        )


# ---------------------------------------------------------------------------
# Invariant 3: unconfirmed proposal does not trigger replan
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invariant_3_proposed_disturbance_does_not_invalidate_plans(tmp_path):
    """A disturbance event created from NL input stays proposed until confirmed."""
    from agents.chat_service import FacilityChatService
    from core.time_engine import TimeEngine
    from llm.glm_client import GlmClient, GlmConfig

    clock = TimeEngine(start_time=datetime(2025, 5, 1), time_scale=200)
    engine = SimulationEngine(archive_root=tmp_path, time_engine=clock)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.submit_approval("hvac_approval", "approve", actor="tester")
    original_storage_plan = list(engine.get_state()["day_ahead"]["storage_plan"])
    original_hvac_plan = list(engine.get_state()["day_ahead"]["hvac_plan"])

    service = FacilityChatService(engine, GlmClient(GlmConfig(api_key="")))
    response = await service.chat(
        message="3号冷机故障，预计2小时恢复",
        actor="测试", actor_role="engineer", session_id="test",
    )
    assert response["needs_confirmation"] is True
    assert response["events"][0]["status"] == "proposed"

    # Plans must NOT change from an unconfirmed proposal.
    state = engine.get_state()
    assert state["day_ahead"]["storage_plan"] == original_storage_plan
    assert state["day_ahead"]["hvac_plan"] == original_hvac_plan


# ---------------------------------------------------------------------------
# Invariant 4: out-of-bounds setpoint cannot be dispatched
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invariant_4a_executor_rejects_over_power_storage():
    """Executor rejects storage power beyond hardware limits."""
    executor = SimulationExecutor()
    with pytest.raises(ExecutionRejected, match="储能功率"):
        await executor.execute(
            run_id="test", step=0,
            sim_time=datetime(2025, 11, 1),
            storage_power_kw=999_999.0,
            hvac_power_kw=0.0, hvac_supply_temp_c=7.0, hvac_return_temp_c=12.0,
            previous_storage_power_kw=0.0, previous_storage_soc=0.5,
            previous_storage_temp_c=25.0, ambient_temp_c=20.0,
            storage_report_id="r", storage_report_hash="h",
            hvac_report_id="r", hvac_report_hash="h",
        )


@pytest.mark.asyncio
async def test_invariant_4b_executor_rejects_excessive_ramp():
    """Executor rejects storage ramp exceeding per-step limit."""
    executor = SimulationExecutor()
    with pytest.raises(ExecutionRejected, match="爬坡"):
        await executor.execute(
            run_id="test", step=0,
            sim_time=datetime(2025, 11, 1),
            storage_power_kw=15000.0,
            hvac_power_kw=0.0, hvac_supply_temp_c=7.0, hvac_return_temp_c=12.0,
            previous_storage_power_kw=-15000.0, previous_storage_soc=0.5,
            previous_storage_temp_c=25.0, ambient_temp_c=20.0,
            storage_report_id="r", storage_report_hash="h",
            hvac_report_id="r", hvac_report_hash="h",
        )


@pytest.mark.asyncio
async def test_invariant_4c_executor_rejects_out_of_range_supply_temp():
    """Executor rejects HVAC supply temperature outside [5, 12] C."""
    executor = SimulationExecutor()
    with pytest.raises(ExecutionRejected, match="供水温度"):
        await executor.execute(
            run_id="test", step=0,
            sim_time=datetime(2025, 11, 1),
            storage_power_kw=0.0,
            hvac_power_kw=0.0, hvac_supply_temp_c=3.0, hvac_return_temp_c=12.0,
            previous_storage_power_kw=0.0, previous_storage_soc=0.5,
            previous_storage_temp_c=25.0, ambient_temp_c=20.0,
            storage_report_id="r", storage_report_hash="h",
            hvac_report_id="r", hvac_report_hash="h",
        )


# ---------------------------------------------------------------------------
# Invariant 5: already-ACKed command must not be re-executed
#
# G3 fix: (run_id, step) idempotency is now implemented in the engine.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invariant_5_acked_command_is_not_re_executed(tmp_path):
    """Re-running the same step must not produce a new command if the previous one was ACKed."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    await engine.run_step(1)
    first_cmd = engine.get_state()["physical_dispatch"]["last_execution"]["command"]["command_id"]

    await engine.run_step(1)  # same step again
    second_cmd = engine.get_state()["physical_dispatch"]["last_execution"]["command"]["command_id"]
    assert second_cmd == first_cmd, "replay should return the same command_id, not a new one"


# ---------------------------------------------------------------------------
# Invariant 6: GLM failure does not block deterministic flow
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invariant_6a_glm_unconfigured_does_not_block_workflow(tmp_path):
    """The full day-ahead + real-time workflow must complete with GLM not configured."""
    engine = SimulationEngine(archive_root=tmp_path)
    assert not engine._reasoning.glm.configured, "precondition: GLM must be unconfigured"

    await engine.start_day(0)
    state = engine.get_state()
    assert state["approval_gates"]["forecast_approval"]["status"] == "pending_approval"

    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.submit_approval("hvac_approval", "approve", actor="tester")

    await engine.run_step(1)
    execution = engine.get_state()["physical_dispatch"]["last_execution"]
    assert execution is not None
    assert execution["ack"]["accepted"] is True


@pytest.mark.asyncio
async def test_invariant_6b_glm_error_falls_back_gracefully(tmp_path):
    """AgentReasoningService returns deterministic fallback on GlmError."""
    from agents.reasoning import AgentReasoningService
    from llm.glm_client import GlmClient, GlmConfig, GlmError

    client = GlmClient(GlmConfig(api_key="fake-key"))
    reasoning = AgentReasoningService(client)

    with patch.object(client, "complete", new=AsyncMock(side_effect=GlmError("network down"))):
        content, meta = await reasoning.explain(
            "data", "deterministic fallback text", {"key": "value"}, "test task",
        )

    assert content == "deterministic fallback text"
    assert meta["mode"] == "deterministic_fallback"
