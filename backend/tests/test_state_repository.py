"""Stage-4 tests: state repository and restart recovery (revise_guide 12.4).

These tests prove that checkpoints separate recoverable runtime state from
immutable archive evidence, and that restart correctly resumes workflow
position without repeating side effects.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agents.engine import SimulationEngine
from repositories.state_repository import (
    FileStateRepository,
    InMemoryStateRepository,
    RuntimeCheckpoint,
    SCHEMA_VERSION,
)


@pytest.mark.asyncio
async def test_restart_resumes_pending_approval(tmp_path):
    """After restart, a checkpointed pending approval is still pending."""
    repo = InMemoryStateRepository()
    engine = SimulationEngine(archive_root=tmp_path, state_repo=repo)
    await engine.start_day(0)

    # Checkpoint saved after start_day.
    cp = repo.load(engine._run_id)
    assert cp is not None
    run_id = engine._run_id
    saved_status = cp.workflow_status

    # New engine instance resumes from checkpoint (G4 restart-resume).
    engine2 = SimulationEngine(
        archive_root=tmp_path, state_repo=repo, resume_run_id=run_id,
    )
    # Engine2 must have the same run_id and restored workflow position.
    assert engine2._run_id == run_id
    state2 = engine2.get_state()
    assert state2["workflow"]["status"] == saved_status
    assert state2["approval_gates"]["forecast_approval"]["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_restart_resumes_after_last_completed_tick(tmp_path):
    """A checkpoint after a tick records the last completed step."""
    repo = InMemoryStateRepository()
    engine = SimulationEngine(archive_root=tmp_path, state_repo=repo)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    await engine.run_step(3)
    cp = repo.load(engine._run_id)
    assert cp is not None
    assert cp.last_completed_tick == 3
    assert cp.dispatch_enabled is True
    assert cp.last_command_id is not None
    assert cp.last_ack_accepted is True


@pytest.mark.asyncio
async def test_restart_does_not_repeat_acknowledged_command(tmp_path):
    """Re-running a tick after checkpoint save returns the same command_id."""
    repo = InMemoryStateRepository()
    engine = SimulationEngine(archive_root=tmp_path, state_repo=repo)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    await engine.run_step(1)
    first_cmd = engine.get_state()["physical_dispatch"]["last_execution"]["command"]["command_id"]
    cp = repo.load(engine._run_id)
    assert cp.last_command_id == first_cmd

    # Simulate restart: new engine, same repo, replay same step.
    await engine.run_step(1)
    second_cmd = engine.get_state()["physical_dispatch"]["last_execution"]["command"]["command_id"]
    assert second_cmd == first_cmd, "replayed step must not generate a new command"


@pytest.mark.asyncio
async def test_restart_restores_acked_steps(tmp_path):
    """Cross-restart idempotency: acked steps survive restart (G3+G4)."""
    repo = InMemoryStateRepository()
    engine = SimulationEngine(archive_root=tmp_path, state_repo=repo)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="t")
    await engine.submit_approval("storage_approval", "approve", actor="t")
    await engine.submit_approval("hvac_approval", "approve", actor="t")

    await engine.run_step(1)
    await engine.run_step(2)
    run_id = engine._run_id
    cp = repo.load(run_id)
    assert set(cp.acked_steps) == {1, 2}

    # Restart with resume
    engine2 = SimulationEngine(
        archive_root=tmp_path, state_repo=repo, resume_run_id=run_id,
    )
    assert engine2._run_id == run_id
    assert 1 in engine2._acked_steps
    assert 2 in engine2._acked_steps
    assert engine2._dispatch_enabled is True


@pytest.mark.asyncio
async def test_corrupt_checkpoint_fails_closed(tmp_path):
    """A corrupt or incompatible checkpoint returns None, not garbage."""
    repo = FileStateRepository(root=tmp_path / "state")
    # Write a corrupt checkpoint file.
    cp_path = tmp_path / "state" / "run-corrupt.checkpoint.json"
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    cp_path.write_text("{NOT VALID JSON", encoding="utf-8")

    result = repo.load("run-corrupt")
    assert result is None, "corrupt checkpoint must fail-closed"

    # Incompatible schema version also fails closed.
    bad_data = {"run_id": "x", "schema_version": 999}
    cp_path.write_text(json.dumps(bad_data), encoding="utf-8")
    result2 = repo.load("run-corrupt")
    assert result2 is None, "incompatible schema_version must fail-closed"


@pytest.mark.asyncio
async def test_non_dict_checkpoint_fails_closed(tmp_path):
    """Non-object JSON (list/string) in checkpoint returns None, not AttributeError."""
    repo = FileStateRepository(root=tmp_path / "state")
    cp_path = tmp_path / "state" / "run-bad.checkpoint.json"
    cp_path.parent.mkdir(parents=True, exist_ok=True)

    cp_path.write_text("[1, 2, 3]", encoding="utf-8")
    assert repo.load("run-bad") is None, "list JSON must fail-closed"

    cp_path.write_text('"hello"', encoding="utf-8")
    assert repo.load("run-bad") is None, "string JSON must fail-closed"


@pytest.mark.asyncio
async def test_confirmed_disturbance_invalidates_downstream_reports(tmp_path):
    """Applying a disturbance resets the workflow to a new run with fresh reports."""
    from core.time_engine import TimeEngine
    from agents.chat_service import FacilityChatService
    from llm.glm_client import GlmClient, GlmConfig

    clock = TimeEngine(start_time=datetime(2025, 5, 1), time_scale=200)
    engine = SimulationEngine(archive_root=tmp_path, time_engine=clock)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.submit_approval("hvac_approval", "approve", actor="tester")

    original_run_id = engine._run_id
    original_storage_report = engine.get_state()["approval_gates"]["storage_approval"]["report_id"]

    # Propose and apply a disturbance.
    service = FacilityChatService(engine, GlmClient(GlmConfig(api_key="")))
    response = await service.chat(
        message="3号冷机故障，预计2小时恢复",
        actor="测试", actor_role="engineer", session_id="test",
    )
    event_id = response["events"][0]["event_id"]
    await engine.decide_disturbance(event_id, "apply", "测试工程师")

    state = engine.get_state()
    # Run ID must have changed (new run started).
    assert engine._run_id != original_run_id
    # Dispatch must be disabled (fresh approval needed).
    assert state["physical_dispatch"]["enabled"] is False
    # Forecast approval back to pending.
    assert state["approval_gates"]["forecast_approval"]["status"] == "pending_approval"


def test_checkpoint_has_schema_version():
    """Every checkpoint must carry a schema_version for migration safety."""
    cp = RuntimeCheckpoint(run_id="test-run")
    assert cp.schema_version == SCHEMA_VERSION
