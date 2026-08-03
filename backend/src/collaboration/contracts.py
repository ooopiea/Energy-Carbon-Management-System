"""Core contracts for the controlled multi-agent collaboration layer (MA-1).

All structured data between Coordinator, Agents, Aggregator, Safety and the
human bridge uses these typed contracts.  Natural language never carries
machine-decision fields.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class AgentRole(StrEnum):
    DATA = "data"
    STORAGE = "storage"
    HVAC = "hvac"
    RISK = "risk"
    MONITOR = "monitor"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class SafetyOutcome(StrEnum):
    PASS = "pass"
    REJECT = "reject"
    STALE_SNAPSHOT = "stale_snapshot"


class JointActionType(StrEnum):
    ANALYSIS_ONLY = "analysis_only"
    REQUEST_HUMAN_INFORMATION = "request_human_information"
    PROPOSE_DISTURBANCE = "propose_disturbance"
    PROPOSE_CONTROL_ACTION = "propose_control_action"
    REQUEST_REPLAN = "request_replan"


class MissionStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    EXECUTING = "executing"
    AWAITING_SAFETY = "awaiting_safety"
    AWAITING_HUMAN = "awaiting_human"
    REVALIDATING = "revalidating"
    BRIDGING = "bridging"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class StopReason(StrEnum):
    COMPLETED = "completed"
    AWAITING_HUMAN = "awaiting_human"
    MISSING_INFO = "missing_info"
    UNAUTHORIZED = "unauthorized"
    STALE_SNAPSHOT = "stale_snapshot"
    REPEAT_CALL = "repeat_call"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    TOOL_FAILURES = "tool_failures"


class OperationalSnapshot(BaseModel):
    snapshot_id: str
    as_of: datetime
    schema_version: int = 1
    provenance: str = "engine"
    facts: dict[str, Any] = Field(default_factory=dict)


class AgentTask(BaseModel):
    task_id: str
    mission_id: str
    objective: str
    snapshot_ref: str
    required_outputs: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    budget: dict[str, int] = Field(default_factory=dict)


class AgentContext(BaseModel):
    agent_id: str
    task: AgentTask
    snapshot: OperationalSnapshot
    visible_facts: dict[str, Any] = Field(default_factory=dict)
    peer_results: list[dict[str, Any]] = Field(default_factory=list)


class AgentResult(BaseModel):
    task_id: str
    agent_id: str
    snapshot_id: str
    status: TaskStatus = TaskStatus.COMPLETED
    findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)


class JointProposal(BaseModel):
    proposal_id: str
    mission_id: str
    snapshot_id: str
    actions: list[JointActionType] = Field(default_factory=list)
    source_task_ids: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    summary: str = ""


class SafetyVerdict(BaseModel):
    outcome: SafetyOutcome
    violations: list[str] = Field(default_factory=list)
    validated_snapshot_id: str = ""
    details: str = ""


class RiskAssessment(BaseModel):
    risk_summary: str = ""
    missing_evidence: list[str] = Field(default_factory=list)
    review_recommendations: list[str] = Field(default_factory=list)


class MissionState(BaseModel):
    mission_id: str
    session_id: str
    actor: str
    role: str
    objective: str
    status: MissionStatus = MissionStatus.CREATED
    stop_reason: StopReason | None = None
    snapshot_id: str = ""
    linked_run_ids: list[str] = Field(default_factory=list)
    task_plan: list[AgentTask] = Field(default_factory=list)
    agent_results: list[AgentResult] = Field(default_factory=list)
    joint_proposal: JointProposal | None = None
    risk_assessment: RiskAssessment | None = None
    safety_verdict: SafetyVerdict | None = None
    human_decision: str | None = None
    budget: dict[str, int] = Field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    schema_version: int = 1


class MissionResult(BaseModel):
    mission_id: str
    status: MissionStatus
    stop_reason: StopReason | None = None
    summary: str = ""
    joint_proposal: JointProposal | None = None
    risk_assessment: RiskAssessment | None = None
    safety_verdict: SafetyVerdict | None = None
    agent_findings: list[str] = Field(default_factory=list)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)


class MissionView(BaseModel):
    mission_id: str
    status: MissionStatus
    stop_reason: StopReason | None = None
    objective: str = ""
    snapshot_id: str = ""
    task_plan_count: int = 0
    agent_result_count: int = 0
    has_joint_proposal: bool = False
    has_safety_verdict: bool = False
    human_decision: str | None = None


class EnergyAgent(Protocol):
    """Protocol every professional agent must satisfy."""

    async def run(self, task: AgentTask, context: AgentContext) -> AgentResult: ...
