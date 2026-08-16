"""Adapters that wire MissionRuntime to the live SimulationEngine.

Full closed-loop: each specialist agent independently analyses engine state
and produces structured recommendations. The aggregator decides which
recommendations are actionable and creates PROPOSE_DISTURBANCE or
PROPOSE_CONTROL_ACTION proposals. After human approval, the bridge calls
engine.propose_disturbance / submit_control_action.

Usage:
    runtime = build_mission_runtime(engine)
    result = await runtime.start("reduce peak demand", "engineer", "engineer", "s1")
    # ... human reviews, then:
    result = await runtime.resume(result.mission_id, "approved")
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
import json
from typing import Any

from collaboration.contracts import (
    AgentContext,
    AgentResult,
    AgentRole,
    AgentTask,
    EnergyAgent,
    JointActionType,
    JointProposal,
    OperationalSnapshot,
    TaskStatus,
)
from collaboration.context_broker import ContextBroker
from collaboration.mission_runtime import MissionRuntime
from collaboration.safety_kernel import SafetyKernel
from core.state import NodeStatus


# ---------------------------------------------------------------------------
# Snapshot provider
# ---------------------------------------------------------------------------

class EngineSnapshotProvider:
    """Captures the current engine state as an OperationalSnapshot."""

    def __init__(self, engine: Any):
        self._engine = engine

    def capture(self) -> OperationalSnapshot:
        state = self._engine.get_state()
        run_id = state.get("workflow", {}).get("run_id", "unknown")
        step = state.get("time", {}).get("step", 0)
        return OperationalSnapshot(
            snapshot_id=f"{run_id}@{step}",
            as_of=datetime.now().astimezone(),
            provenance="engine",
            facts=state,
        )


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

_TASK_TEMPLATES = [
    (AgentRole.DATA, "data analysis: check load trends, data quality and anomalies"),
    (AgentRole.STORAGE, "storage analysis: assess SOC, power, temperature and dispatch"),
    (AgentRole.HVAC, "hvac analysis: assess chiller dispatch, supply temp and energy efficiency"),
    (AgentRole.MONITOR, "monitor analysis: check alerts, approval status and system health"),
]


class RoleCoordinator:
    """Creates one task per specialist agent with role-specific objectives."""

    def plan(self, objective: str, snapshot: OperationalSnapshot) -> list[AgentTask]:
        return [
            AgentTask(
                task_id=f"task-{role.value}-{uuid.uuid4().hex[:6]}",
                mission_id=snapshot.snapshot_id,
                objective=template,
                snapshot_ref=snapshot.snapshot_id,
            )
            for role, template in _TASK_TEMPLATES
        ]


# ---------------------------------------------------------------------------
# Agent adapters: each reads role-filtered facts and produces findings + recs
# ---------------------------------------------------------------------------

class EngineAgentAdapter:
    """Wraps engine state into structured AgentResults per role.

    Each role independently analyses its visible facts (filtered by
    ContextBroker) and produces:
    - findings: human-readable observations
    - recommendations: structured dicts the aggregator can turn into actions
    """

    def __init__(self, role: AgentRole):
        self._role = role

    async def run(self, task: AgentTask, context: AgentContext) -> AgentResult:
        facts = context.visible_facts
        findings: list[str] = []
        recommendations: list[dict[str, Any]] = []
        risks: list[str] = []

        handler = {
            AgentRole.DATA: self._analyze_data,
            AgentRole.STORAGE: self._analyze_storage,
            AgentRole.HVAC: self._analyze_hvac,
            AgentRole.MONITOR: self._analyze_monitor,
        }.get(self._role)

        if handler:
            handler(facts, findings, recommendations, risks)

        return AgentResult(
            task_id=task.task_id,
            agent_id=self._role.value,
            snapshot_id=context.snapshot.snapshot_id,
            status=TaskStatus.COMPLETED,
            findings=findings,
            recommendations=[
                json.dumps(rec, ensure_ascii=False) if isinstance(rec, dict) else rec
                for rec in recommendations
            ],
            risks=risks,
        )

    # -- per-role analysis ------------------------------------------------

    def _analyze_data(
        self, facts: dict, findings: list, recs: list, risks: list
    ) -> None:
        prov = facts.get("data_provenance", {}).get("load", {})
        timeline = facts.get("data_timeline", {})
        load_kw = float(facts.get("load_kw", 0))
        load_forecast = facts.get("day_ahead", {}).get("load_forecast", [])

        findings.append(
            f"load source: {prov.get('selected_date', 'unknown')}, "
            f"resolution: {timeline.get('resolution_minutes', 15)}min"
        )
        findings.append(f"current load: {load_kw:.0f} kW")

        if load_forecast:
            peak = max(load_forecast)
            mean = sum(load_forecast) / len(load_forecast)
            findings.append(f"day-ahead forecast peak: {peak:.0f} kW, mean: {mean:.0f} kW")

            # Recommend demand cap if peak is very high
            config_min = 5000.0
            if peak > 100000:
                recs.append({
                    "kind": "control_action",
                    "system": "overview",
                    "action": "set_demand_cap",
                    "target": "demand_cap_kw",
                    "value": round(peak * 0.92, 0),
                    "unit": "kW",
                    "reason": f"forecast peak {peak:.0f} kW exceeds 100 MW, "
                    f"recommend demand cap at 92%",
                    "priority": "medium",
                })

        event_impacts = prov.get("event_impacts", [])
        if event_impacts:
            findings.append(f"{len(event_impacts)} disturbance events applied")

    def _analyze_storage(
        self, facts: dict, findings: list, recs: list, risks: list
    ) -> None:
        soc = float(facts.get("storage_soc", 0.5))
        power = float(facts.get("storage_power_kw", 0))
        temp = float(facts.get("storage_temp_c", 25))
        plan = facts.get("day_ahead", {}).get("storage_plan", [])

        findings.append(f"SOC: {soc:.1%}, power: {power:.0f} kW, temp: {temp:.1f}C")
        if plan:
            findings.append(f"day-ahead peak discharge: {max(plan):.0f} kW")

        # SOC near lower bound: recommend reducing discharge
        if soc < 0.15:
            risks.append("SOC near lower bound")
            recs.append({
                "kind": "control_action",
                "system": "storage",
                "action": "limit_discharge",
                "target": "power_kw",
                "value": min(power, 3000.0),
                "unit": "kW",
                "reason": f"SOC {soc:.1%} near lower bound, "
                f"limit discharge to preserve battery",
                "priority": "high",
            })
        # SOC near upper bound: recommend charging less
        elif soc > 0.85:
            risks.append("SOC near upper bound")
            recs.append({
                "kind": "control_action",
                "system": "storage",
                "action": "limit_charge",
                "target": "power_kw",
                "value": max(power, -3000.0),
                "unit": "kW",
                "reason": f"SOC {soc:.1%} near upper bound, limit charging",
                "priority": "high",
            })

        # Temperature warning
        if temp > 40:
            risks.append(f"storage temperature {temp:.1f}C approaching limit")
            recs.append({
                "kind": "control_action",
                "system": "storage",
                "action": "reduce_power_for_thermal",
                "target": "power_kw",
                "value": max(0, power * 0.5),
                "unit": "kW",
                "reason": f"cell temperature {temp:.1f}C near 45C limit, "
                f"reduce power for thermal safety",
                "priority": "high",
            })

    def _analyze_hvac(
        self, facts: dict, findings: list, recs: list, risks: list
    ) -> None:
        power = float(facts.get("hvac_power_kw", 0))
        supply = float(facts.get("hvac_supply_temp_c", 7))
        price = float(facts.get("price", 0))
        tariff = facts.get("tariff_period", "flat")
        topology = facts.get("chiller_topology", [])

        findings.append(f"power: {power:.0f} kW, supply temp: {supply:.1f}C")
        findings.append(f"tariff: {tariff}, price: {price:.4f} CNY/kWh")
        if topology:
            total = sum(s.get("chiller_count", 0) for s in topology)
            findings.append(f"chiller topology: {total} units, {len(topology)} stations")

        # High price period: recommend raising supply temp to reduce compressor load
        if tariff in ("sharp", "peak") and supply < 8.0:
            recs.append({
                "kind": "control_action",
                "system": "hvac",
                "action": "raise_supply_temp",
                "target": "supply_temp_c",
                "value": min(supply + 1.5, 9.0),
                "unit": "C",
                "reason": f"{tariff} period price {price:.4f}, "
                f"raise supply temp to reduce compressor load",
                "priority": "medium",
            })

        if supply > 9.0:
            risks.append("supply temp above optimal range")

    def _analyze_monitor(
        self, facts: dict, findings: list, recs: list, risks: list
    ) -> None:
        gates = facts.get("approval_gates", {})
        pending = [
            gid for gid, g in gates.items()
            if isinstance(g, dict) and g.get("status") == NodeStatus.PENDING_APPROVAL
        ]
        alerts = facts.get("alerts", [])
        unacked = [a for a in alerts if isinstance(a, dict) and not a.get("acknowledged")]
        dispatch = facts.get("physical_dispatch", {})

        findings.append(f"pending approvals: {len(pending)}, unacked alerts: {len(unacked)}")
        findings.append(
            f"dispatch: {'active' if dispatch.get('enabled') else 'inactive'}, "
            f"commands: {dispatch.get('command_count', 0)}"
        )

        for alert in unacked[:3]:
            risks.append(f"alert: {alert.get('message', '')}")

        # Monitor does not propose actions; it flags issues for other agents
        # or the human operator.


# ---------------------------------------------------------------------------
# Aggregator: combines agent results into an actionable JointProposal
# ---------------------------------------------------------------------------

class ActionableAggregator:
    """Merges specialist results and creates proposals with action payloads.

    Collects structured recommendations from all agents, filters by priority,
    and packages them into action_payloads on the JointProposal. The proposal
    actions list signals whether human approval is needed.
    """

    def aggregate(
        self,
        results: list[AgentResult],
        snapshot: OperationalSnapshot,
    ) -> JointProposal:
        all_findings = [f for r in results for f in r.findings]
        failed = [r for r in results if r.status == TaskStatus.FAILED]
        conflicts = [f"agent {r.agent_id} failed" for r in failed]

        # Collect actionable recommendations from agent results.
        # AgentResult.recommendations stores dicts serialized as strings;
        # we re-parse them here.
        action_payloads: list[dict[str, Any]] = []
        for res in results:
            for rec in res.recommendations:
                if isinstance(rec, dict):
                    payload = rec
                else:
                    try:
                        payload = json.loads(rec) if isinstance(rec, str) else None
                    except (json.JSONDecodeError, TypeError):
                        payload = None
                if payload and payload.get("priority") in ("high", "medium"):
                    action_payloads.append(payload)

        # Determine action types
        actions: list[JointActionType] = []
        has_disturbance = any(p["kind"] == "disturbance" for p in action_payloads)
        has_control = any(p["kind"] == "control_action" for p in action_payloads)
        if has_disturbance:
            actions.append(JointActionType.PROPOSE_DISTURBANCE)
        if has_control:
            actions.append(JointActionType.PROPOSE_CONTROL_ACTION)
        if not actions and all_findings:
            actions.append(JointActionType.ANALYSIS_ONLY)

        summary = " | ".join(all_findings[:6])
        if len(all_findings) > 6:
            summary += f" ... ({len(all_findings)} total)"

        return JointProposal(
            proposal_id=f"prop-{uuid.uuid4().hex[:12]}",
            mission_id="",
            snapshot_id=snapshot.snapshot_id,
            actions=actions,
            source_task_ids=[r.task_id for r in results],
            conflicts=conflicts,
            summary=summary,
            action_payloads=action_payloads,
        )


# ---------------------------------------------------------------------------
# Proposal bridge: executes approved proposals through the engine
# ---------------------------------------------------------------------------

class EngineProposalBridge:
    """Translates approved proposals into engine disturbance/control actions.

    Called by MissionRuntime after human approval (resume). Each action_payload
    is routed to the appropriate engine method:
    - disturbance -> engine.propose_disturbance (creates a 'proposed' event)
    - control_action -> engine.submit_control_action (queues pending override)
    """

    def __init__(self, engine: Any):
        self._engine = engine

    async def bridge(self, proposal: JointProposal, snapshot: OperationalSnapshot) -> str:
        results: list[str] = []
        sim_time = snapshot.as_of

        for payload in proposal.action_payloads:
            kind = payload.get("kind", "")
            try:
                if kind == "disturbance":
                    event = await self._engine.propose_disturbance(
                        actor="mission_bridge",
                        actor_role="engineer",
                        source_text=payload.get("summary", proposal.summary),
                        event_type=payload.get("event_type", "operational_note"),
                        target=payload.get("target", "\u56ed\u533a"),
                        start_time=sim_time,
                        end_time=payload.get("end_time"),
                        parameters=payload.get("parameters"),
                        summary=payload.get("summary", ""),
                        confidence=float(payload.get("confidence", 0.8)),
                        parsed_by="mission_aggregator",
                    )
                    results.append(f"disturbance {event.event_id} proposed: {event.summary[:60]}")

                elif kind == "control_action":
                    record = await self._engine.submit_control_action(
                        system=payload["system"],
                        action=payload["action"],
                        target=payload["target"],
                        value=float(payload["value"]),
                        unit=payload["unit"],
                        reason=payload["reason"],
                        actor="mission_bridge",
                    )
                    results.append(
                        f"control_action {record.action_id} "
                        f"{record.status}: {record.action} -> {record.target}"
                    )
                else:
                    results.append(f"unknown action kind: {kind}")

            except Exception as exc:
                results.append(f"FAILED {kind}: {type(exc).__name__}: {exc}")

        if not proposal.action_payloads:
            return f"analysis-only proposal logged: {proposal.summary[:200]}"

        return "; ".join(results)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_mission_runtime(engine: Any) -> MissionRuntime:
    """Wire all collaboration adapters to a live SimulationEngine."""
    return MissionRuntime(
        snapshot_provider=EngineSnapshotProvider(engine),
        coordinator=RoleCoordinator(),
        agents={
            AgentRole.DATA: EngineAgentAdapter(AgentRole.DATA),
            AgentRole.STORAGE: EngineAgentAdapter(AgentRole.STORAGE),
            AgentRole.HVAC: EngineAgentAdapter(AgentRole.HVAC),
            AgentRole.MONITOR: EngineAgentAdapter(AgentRole.MONITOR),
        },
        aggregator=ActionableAggregator(),
        safety=SafetyKernel(),
        bridge=EngineProposalBridge(engine),
        context_broker=ContextBroker(),
    )
