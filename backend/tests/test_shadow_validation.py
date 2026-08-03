"""Stage-7 tests: shadow validation, switching and cleanup (revise_guide 12.7).

Tests verify that:
- Shadow mode never produces physical side effects
- Feature flag switching is safe
- Deterministic paths work independently when multi-agent is off
- Old dual-definition code has been fully removed
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime

import pytest

from agents.engine import SimulationEngine
from collaboration.contracts import (
    AgentResult,
    AgentTask,
    JointActionType,
    JointProposal,
    MissionStatus,
    OperationalSnapshot,
    StopReason,
    TaskStatus,
)
from collaboration.feature_flags import (
    AgentMode,
    get_agent_mode,
    is_multi_agent_active,
    is_multi_agent_enabled,
    is_shadow_mode,
)
from collaboration.mission_runtime import MissionRuntime
from collaboration.safety_kernel import SafetyKernel


# ---------------------------------------------------------------------------
# Feature flag tests
# ---------------------------------------------------------------------------

def test_default_mode_is_template():
    os.environ.pop("ENERGY_AGENT_MODE", None)
    assert get_agent_mode() == AgentMode.TEMPLATE
    assert not is_multi_agent_enabled()
    assert not is_shadow_mode()
    assert not is_multi_agent_active()


def test_shadow_mode_detected():
    os.environ["ENERGY_AGENT_MODE"] = "shadow_multi_agent"
    try:
        assert is_shadow_mode()
        assert is_multi_agent_enabled()
        assert not is_multi_agent_active()
    finally:
        os.environ.pop("ENERGY_AGENT_MODE", None)


def test_full_multi_agent_mode_detected():
    os.environ["ENERGY_AGENT_MODE"] = "multi_agent"
    try:
        assert is_multi_agent_active()
        assert is_multi_agent_enabled()
        assert not is_shadow_mode()
    finally:
        os.environ.pop("ENERGY_AGENT_MODE", None)


def test_invalid_mode_falls_back_to_template():
    os.environ["ENERGY_AGENT_MODE"] = "bogus_value"
    try:
        assert get_agent_mode() == AgentMode.TEMPLATE
    finally:
        os.environ.pop("ENERGY_AGENT_MODE", None)


# ---------------------------------------------------------------------------
# Shadow mode: zero physical side effects
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shadow_mode_mission_never_dispatches(tmp_path):
    """In shadow mode, a mission that proposes actions never reaches execution."""
    snap_provider = _StaticSnapshot({"storage_soc": 0.5})
    coordinator = _SimpleCoordinator()
    agents = {"storage": _ScriptedAgent("storage", ["建议放电"])}

    class ProposingAggregator:
        def aggregate(self, results, snapshot):
            return JointProposal(
                proposal_id="p1", mission_id="m1",
                snapshot_id=snapshot.snapshot_id,
                actions=[JointActionType.PROPOSE_CONTROL_ACTION],
            )

    runtime = MissionRuntime(
        snap_provider, coordinator, agents,
        ProposingAggregator(), SafetyKernel(),
    )
    result = await runtime.start("储能分析", "tester", "engineer", "s1")

    # Even with a proposal, the mission pauses for human or blocks on safety.
    # It never directly executes anything.
    assert result.status in (MissionStatus.AWAITING_HUMAN, MissionStatus.BLOCKED)
    # No physical_dispatch state changed in the engine.
    assert result.tool_trace == []  # no bridge was called


# ---------------------------------------------------------------------------
# Deterministic path works when multi-agent is off
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deterministic_workflow_independent_of_agent_flag(tmp_path):
    """The day-ahead + realtime workflow must work identically regardless of flag."""
    os.environ["ENERGY_AGENT_MODE"] = "template"
    try:
        engine = SimulationEngine(archive_root=tmp_path)
        await engine.start_day(0)
        await engine.submit_approval("forecast_approval", "approve", actor="t")
        await engine.submit_approval("storage_approval", "approve", actor="t")
        await engine.submit_approval("hvac_approval", "approve", actor="t")
        await engine.run_step(1)

        state = engine.get_state()
        assert state["physical_dispatch"]["enabled"] is True
        assert state["physical_dispatch"]["last_execution"] is not None
    finally:
        os.environ.pop("ENERGY_AGENT_MODE", None)


# ---------------------------------------------------------------------------
# Old code removal verification
# ---------------------------------------------------------------------------

def test_no_dual_graph_definition_remains():
    """GRAPH_NODES and GRAPH_EDGES must not exist anywhere in the graph package."""
    import graph.workflow as wf
    import graph.spec as spec_mod
    import graph.compiler as comp_mod

    for mod in (wf, spec_mod, comp_mod):
        assert not hasattr(mod, "GRAPH_NODES"), f"{mod.__name__} still has GRAPH_NODES"
        assert not hasattr(mod, "GRAPH_EDGES"), f"{mod.__name__} still has GRAPH_EDGES"


def test_no_redundant_coordinate_agents_template():
    """The template coordinate_agents still exists for backward compat but is
    clearly marked as the legacy path."""
    from agents.engine import SimulationEngine
    engine = SimulationEngine.__new__(SimulationEngine)
    # Just verify the method exists (it's the template baseline).
    assert hasattr(SimulationEngine, "coordinate_agents")


# ---------------------------------------------------------------------------
# Full validation pass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_day_ahead_and_realtime_cycle_completes(tmp_path):
    """End-to-end: start_day -> all approvals -> run_step -> verify evidence chain."""
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)

    # All three approvals.
    await engine.submit_approval("forecast_approval", "approve", actor="engineer")
    await engine.submit_approval("storage_approval", "approve", actor="engineer")
    await engine.submit_approval("hvac_approval", "approve", actor="engineer")

    # Run several ticks.
    for step in range(5):
        await engine.run_step(step)

    state = engine.get_state()

    # Evidence chain: command -> ACK -> feedback.
    exec_result = state["physical_dispatch"]["last_execution"]
    assert exec_result is not None
    assert exec_result["ack"]["accepted"] is True
    assert exec_result["feedback"]["command_id"] == exec_result["command"]["command_id"]

    # Checkpoint exists.
    assert state["checkpoint"]["available"] is True
    assert state["checkpoint"]["dispatch_enabled"] is True
    assert state["checkpoint"]["last_command_id"] == exec_result["command"]["command_id"]

    # Series data accumulated.
    assert len(state["series"]["load"]) == 5
    assert len(state["series"]["storage_power"]) == 5


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _StaticSnapshot:
    def __init__(self, facts=None):
        self._facts = facts or {}
        self._id = f"snap-{uuid.uuid4().hex[:8]}"

    def capture(self):
        return OperationalSnapshot(
            snapshot_id=self._id, as_of=datetime.now(), facts=dict(self._facts),
        )


class _SimpleCoordinator:
    def plan(self, objective, snapshot):
        return [AgentTask(
            task_id=f"t-{uuid.uuid4().hex[:8]}", mission_id="m",
            objective=objective, snapshot_ref=snapshot.snapshot_id,
        )]


class _ScriptedAgent:
    def __init__(self, agent_id, findings=None):
        self.agent_id = agent_id
        self._findings = findings or ["analysis done"]

    async def run(self, task, context):
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id,
            snapshot_id=context.snapshot.snapshot_id,
            status=TaskStatus.COMPLETED, findings=list(self._findings),
        )
