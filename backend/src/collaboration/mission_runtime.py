"""Mission runtime: orchestrates controlled multi-agent collaboration.

Execution graph (revise_guide 10.6):
    START -> capture_snapshot -> coordinator_plan -> fan-out agents
    -> aggregate -> risk_review -> safety_precheck
    -> [pass] interrupt for human -> revalidate -> proposal_bridge -> END
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol

from collaboration.contracts import (
    AgentContext,
    AgentResult,
    AgentRole,
    AgentTask,
    EnergyAgent,
    JointProposal,
    MissionResult,
    MissionState,
    MissionStatus,
    MissionView,
    OperationalSnapshot,
    RiskAssessment,
    SafetyOutcome,
    SafetyVerdict,
    StopReason,
    TaskStatus,
)
from collaboration.context_broker import ContextBroker
from collaboration.safety_kernel import SafetyKernel


class SnapshotProvider(Protocol):
    def capture(self) -> OperationalSnapshot: ...


class Coordinator(Protocol):
    def plan(self, objective: str, snapshot: OperationalSnapshot) -> list[AgentTask]: ...


class Aggregator(Protocol):
    def aggregate(self, results: list[AgentResult], snapshot: OperationalSnapshot) -> JointProposal: ...


class ProposalBridge(Protocol):
    def bridge(self, proposal: JointProposal, snapshot: OperationalSnapshot) -> str: ...


class MissionRuntime:
    """Public seam: start / resume / get.  The ONLY multi-agent entry point."""

    def __init__(
        self,
        snapshot_provider: SnapshotProvider,
        coordinator: Coordinator,
        agents: dict[str, EnergyAgent],
        aggregator: Aggregator,
        safety: SafetyKernel,
        bridge: ProposalBridge | None = None,
        context_broker: ContextBroker | None = None,
    ):
        self._snapshot_provider = snapshot_provider
        self._coordinator = coordinator
        self._agents = agents
        self._aggregator = aggregator
        self._safety = safety
        self._bridge = bridge
        self._context_broker = context_broker or ContextBroker()
        self._missions: dict[str, MissionState] = {}

    async def start(self, goal: str, actor: str, role: str, session_id: str) -> MissionResult:
        mission_id = f"mis-{uuid.uuid4().hex[:12]}"
        state = MissionState(
            mission_id=mission_id, session_id=session_id,
            actor=actor, role=role, objective=goal,
            status=MissionStatus.PLANNING,
        )
        self._missions[mission_id] = state
        return await self._run_until_interrupt(state)

    async def resume(self, mission_id: str, human_input: str) -> MissionResult:
        state = self._missions.get(mission_id)
        if state is None:
            return MissionResult(
                mission_id=mission_id, status=MissionStatus.BLOCKED,
                stop_reason=StopReason.MISSING_INFO, summary="mission not found",
            )
        if state.status == MissionStatus.CANCELLED:
            return MissionResult(
                mission_id=mission_id, status=MissionStatus.CANCELLED,
                stop_reason=StopReason.CANCELLED, summary="mission was cancelled",
            )
        state.human_decision = human_input
        state.status = MissionStatus.REVALIDATING
        return await self._run_after_human(state)

    def get(self, mission_id: str) -> MissionView:
        state = self._missions.get(mission_id)
        if state is None:
            return MissionView(mission_id=mission_id, status=MissionStatus.BLOCKED)
        return MissionView(
            mission_id=state.mission_id, status=state.status,
            stop_reason=state.stop_reason, objective=state.objective,
            snapshot_id=state.snapshot_id,
            task_plan_count=len(state.task_plan),
            agent_result_count=len(state.agent_results),
            has_joint_proposal=state.joint_proposal is not None,
            has_safety_verdict=state.safety_verdict is not None,
            human_decision=state.human_decision,
        )

    def cancel(self, mission_id: str) -> None:
        state = self._missions.get(mission_id)
        if state:
            state.status = MissionStatus.CANCELLED
            state.stop_reason = StopReason.CANCELLED

    async def _run_until_interrupt(self, state: MissionState) -> MissionResult:
        snapshot = self._snapshot_provider.capture()
        state.snapshot_id = snapshot.snapshot_id
        state.task_plan = self._coordinator.plan(state.objective, snapshot)
        state.status = MissionStatus.EXECUTING

        for task in state.task_plan:
            agent_id = self._infer_agent_id(task)
            agent = self._agents.get(agent_id)
            if agent is None:
                state.agent_results.append(AgentResult(
                    task_id=task.task_id, agent_id=agent_id or "unknown",
                    snapshot_id=snapshot.snapshot_id, status=TaskStatus.FAILED,
                    findings=["no agent registered for this task"],
                ))
                continue
            context = self._context_broker.build(agent_id, task, snapshot)
            result = await agent.run(task, context)
            state.agent_results.append(result)

        state.joint_proposal = self._aggregator.aggregate(state.agent_results, snapshot)
        state.risk_assessment = self._review_risks(state.agent_results)
        state.safety_verdict = self._safety.validate(state.joint_proposal, snapshot)

        if state.safety_verdict.outcome in (SafetyOutcome.REJECT, SafetyOutcome.STALE_SNAPSHOT):
            state.status = MissionStatus.BLOCKED
            state.stop_reason = (
                StopReason.STALE_SNAPSHOT
                if state.safety_verdict.outcome == SafetyOutcome.STALE_SNAPSHOT
                else StopReason.UNAUTHORIZED
            )
            return self._to_result(state)

        if state.joint_proposal.actions:
            state.status = MissionStatus.AWAITING_HUMAN
            state.stop_reason = StopReason.AWAITING_HUMAN
            return self._to_result(state)

        state.status = MissionStatus.COMPLETED
        state.stop_reason = StopReason.COMPLETED
        return self._to_result(state)

    async def _run_after_human(self, state: MissionState) -> MissionResult:
        fresh = self._snapshot_provider.capture()
        if state.joint_proposal:
            proposal = state.joint_proposal.model_copy(update={"snapshot_id": fresh.snapshot_id})
            state.safety_verdict = self._safety.validate(proposal, fresh)

        if state.safety_verdict and state.safety_verdict.outcome != SafetyOutcome.PASS:
            state.status = MissionStatus.BLOCKED
            state.stop_reason = (
                StopReason.STALE_SNAPSHOT
                if state.safety_verdict.outcome == SafetyOutcome.STALE_SNAPSHOT
                else StopReason.UNAUTHORIZED
            )
            return self._to_result(state)

        if self._bridge and state.joint_proposal:
            msg = self._bridge.bridge(state.joint_proposal, fresh)
            state.tool_trace.append({"name": "proposal_bridge", "result": msg})

        state.status = MissionStatus.COMPLETED
        state.stop_reason = StopReason.COMPLETED
        return self._to_result(state)

    def _infer_agent_id(self, task: AgentTask) -> str:
        obj = (task.objective or "").lower()
        if "data" in obj or "数据" in task.objective:
            return AgentRole.DATA
        if "storage" in obj or "储能" in task.objective:
            return AgentRole.STORAGE
        if "hvac" in obj or "冷机" in task.objective or "暖通" in task.objective:
            return AgentRole.HVAC
        if "risk" in obj or "风险" in task.objective:
            return AgentRole.RISK
        return ""

    def _review_risks(self, results: list[AgentResult]) -> RiskAssessment:
        all_risks = [r for res in results for r in res.risks]
        missing = [f"agent {r.agent_id} failed" for r in results if r.status == TaskStatus.FAILED]
        return RiskAssessment(
            risk_summary="; ".join(all_risks) if all_risks else "no significant risks",
            missing_evidence=missing,
        )

    def _to_result(self, state: MissionState) -> MissionResult:
        return MissionResult(
            mission_id=state.mission_id, status=state.status,
            stop_reason=state.stop_reason, summary=state.objective,
            joint_proposal=state.joint_proposal,
            risk_assessment=state.risk_assessment,
            safety_verdict=state.safety_verdict,
            agent_findings=[f for r in state.agent_results for f in r.findings],
            tool_trace=state.tool_trace,
        )
