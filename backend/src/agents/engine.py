"""LangGraph-driven industrial energy simulation and approval-safe execution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from algorithms.carbon import account_dispatch_carbon, compute_carbon_factors
from algorithms.hvac import get_chiller_topology, optimize_hvac_dispatch
from algorithms.storage import optimize_storage_dispatch
from algorithms.tariff import calculate_tariff
from core.archive import ArchiveStore
from core.config import (
    HVAC_DEFAULTS,
    POINTS_PER_DAY,
    STORAGE_DEFAULTS,
    TARIFF_PRICES,
    get_tariff_period,
)
from core.executor import ExecutionRejected, SimulationExecutor
from core.state import (
    AgentNodeStatus,
    AgentReport,
    AgentType,
    AlertItem,
    ApprovalGate,
    ControlActionRecord,
    DispatchExecution,
    DisturbanceEvent,
    EnergySystemState,
    NodeStatus,
    PhysicsConstraints,
    Severity,
)
from core.time_engine import TimeEngine, get_time_engine
from data.raw_loader import get_load_data_range, process_load_to_15min
from data.simulator import generate_day_ahead_data
from graph.workflow import build_energy_workflow
from agents.reasoning import AgentReasoningService
from llm.glm_client import GlmClient
from repositories.state_repository import (
    InMemoryStateRepository,
    StateRepository,
    RuntimeCheckpoint,
)


class ApprovalError(ValueError):
    pass


class ApprovalNotFound(ApprovalError):
    pass


class ApprovalConflict(ApprovalError):
    pass


class InvalidApprovalDecision(ApprovalError):
    pass


class ApprovalBindingMismatch(ApprovalConflict):
    pass


class InvalidControlAction(ValueError):
    pass


class SimulationEngine:
    """Single-site runtime; all mutating transitions are serialized by one lock."""

    def __init__(
        self,
        archive_root: str | Path | None = None,
        time_engine: TimeEngine | None = None,
        executor: SimulationExecutor | None = None,
        glm_client: GlmClient | None = None,
        state_repo: StateRepository | None = None,
        resume_run_id: str | None = None,
    ):
        self._rng = random.Random(123)
        self._constraints = PhysicsConstraints()
        data_start, data_end, data_range_provenance = get_load_data_range()
        self._data_start = data_start
        self._data_end = data_end
        self._data_range_provenance = data_range_provenance
        resolved_start = datetime.combine(data_start, datetime.min.time()) if data_start else None
        self._time = time_engine or get_time_engine(resolved_start)
        self._archive = ArchiveStore(archive_root)
        self._executor = executor or SimulationExecutor()
        self._state_repo = state_repo or InMemoryStateRepository()
        self._reasoning = AgentReasoningService(glm_client)
        self._transition_lock = asyncio.Lock()
        self._broadcast_lock = asyncio.Lock()
        self._ws_clients: set[Any] = set()
        self._ws_lock = threading.RLock()
        self._auto_approve = os.getenv("ENERGY_AUTO_APPROVE", "false").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        self._last_error: str | None = None
        self._last_successful_tick_at: datetime | None = None
        self._started_at = datetime.now().astimezone()

        self._graph = build_energy_workflow(
            {
                "data_agent": self._graph_data_agent,
                "prediction_agent": self._graph_prediction_agent,
                "forecast_approval": self._graph_forecast_approval,
                "storage_agent": self._graph_storage_agent,
                "hvac_agent": self._graph_hvac_agent,
                "dispatch_approvals": self._graph_dispatch_approvals,
                "physical_dispatch": self._graph_physical_dispatch,
                "monitor_agent": self._graph_monitor_agent,
            }
        )
        self._clear_all_state()
        if resume_run_id is not None:
            self._restore_from_checkpoint(resume_run_id)

    # ------------------------------------------------------------------
    # Lifecycle and graph transitions
    # ------------------------------------------------------------------

    def _clear_all_state(self) -> None:
        self._day_data: dict[str, Any] | None = None
        self._storage_plan: dict[str, Any] | None = None
        self._hvac_plan: dict[str, Any] | None = None
        self._carbon_data: dict[str, Any] | None = None
        self._tariff_data: dict[str, Any] | None = None
        self._carbon_dispatch: dict[str, Any] | None = None
        self._last_step = -1
        self._last_day = -1
        self._reports: list[AgentReport] = []
        self._alerts: list[AlertItem] = []
        self._control_actions: list[ControlActionRecord] = []
        self._disturbance_events: list[DisturbanceEvent] = []
        self._pending_overrides: dict[str, ControlActionRecord] = {}
        self._agent_nodes: dict[str, AgentNodeStatus] = {}
        self._approval_gates: dict[str, ApprovalGate] = {}
        self._daily_metrics = {"energy": 0.0, "cost": 0.0, "carbon": 0.0, "peak": 0.0}
        self._series_cache: dict[str, list[dict[str, Any]]] = {}
        self._run_id = f"run-{uuid.uuid4().hex[:12]}"
        self._workflow_state: EnergySystemState = {
            "run_id": self._run_id,
            "events": [],
            "workflow_status": "idle",
            "current_node": "idle",
            "error": None,
        }
        self._dispatch_enabled = False
        self._last_execution: DispatchExecution | None = None
        self._command_count = 0
        self._acked_commands: dict[tuple[str, int], DispatchExecution] = {}
        self._acked_steps: set[int] = set()
        self._current_values = self._empty_current_values()
        self._demand_cap_kw: float | None = None
        self._pending_overrides = {}
        self._init_nodes()

    def _restore_from_checkpoint(self, run_id: str) -> bool:
        """Restore workflow state from a saved checkpoint (G4 restart-resume).

        Called during __init__ when resume_run_id is provided. Restores
        workflow_status, current_node, dispatch_enabled, approval gate
        statuses, and acked step numbers so that G3 idempotency holds
        across process restarts.
        """
        cp = self._state_repo.load(run_id)
        if cp is None:
            return False
        self._run_id = cp.run_id
        self._last_day = cp.day
        self._last_step = cp.step
        self._workflow_state["run_id"] = cp.run_id
        self._workflow_state["workflow_status"] = cp.workflow_status
        self._workflow_state["current_node"] = cp.current_node
        self._dispatch_enabled = cp.dispatch_enabled
        # Restore approval gate statuses from checkpoint bindings.
        for gate_id, binding in cp.approval_bindings.items():
            gate = self._approval_gates.get(gate_id)
            if gate is None:
                continue
            status_str = binding.get("status", "idle")
            try:
                gate.status = NodeStatus(status_str)
            except ValueError:
                gate.status = NodeStatus.IDLE
            gate.report_id = binding.get("report_id") or None
            gate.report_hash = binding.get("report_hash") or None
        # Restore acked steps so G3 idempotency survives restart.
        self._acked_steps = set(cp.acked_steps)
        return True

    @staticmethod
    def _empty_current_values() -> dict[str, Any]:
        return {
            "load_kw": 0.0,
            "solar_kw": 0.0,
            "grid_kw": 0.0,
            "storage_power_kw": 0.0,
            "storage_soc": 0.5,
            "storage_temp_c": 25.0,
            "hvac_power_kw": 0.0,
            "hvac_supply_temp_c": 7.0,
            "hvac_return_temp_c": 12.0,
            "hvac_delta_kw": 0.0,
            "carbon_factor": 0.5,
            "price": 0.0,
            "tariff_period": "flat",
        }

    def _init_nodes(self) -> None:
        self._agent_nodes.clear()
        self._approval_gates.clear()
        for node_id, agent_type, name in [
            ("data_collect", AgentType.DATA, "数据采集"),
            ("data_archive", AgentType.DATA, "数据封存"),
            ("prediction", AgentType.PREDICTION, "负荷处理"),
            ("storage_dispatch", AgentType.STORAGE, "储能调度"),
            ("hvac_dispatch", AgentType.HVAC, "HVAC调度"),
            ("monitor", AgentType.MONITOR, "系统监察"),
        ]:
            self._agent_nodes[node_id] = AgentNodeStatus(
                agent_type=agent_type,
                node_id=node_id,
                name=name,
            )
        for gate_id, name, description in [
            ("forecast_approval", "负荷处理审批", "负荷清洗与粒度转换结果工程师审批"),
            ("storage_approval", "储能审批", "储能调度策略工程师审批"),
            ("hvac_approval", "HVAC审批", "HVAC调度策略工程师审批"),
        ]:
            self._approval_gates[gate_id] = ApprovalGate(
                gate_id=gate_id,
                name=name,
                description=description,
            )

    def _prepare_day(self, day: int) -> None:
        previous_storage_soc = float(self._current_values.get("storage_soc", 0.5))
        self._day_data = None
        self._storage_plan = None
        self._hvac_plan = None
        self._carbon_data = None
        self._tariff_data = None
        self._carbon_dispatch = None
        self._last_step = -1
        self._last_day = day
        self._daily_metrics = {"energy": 0.0, "cost": 0.0, "carbon": 0.0, "peak": 0.0}
        self._series_cache = {}
        self._dispatch_enabled = False
        self._last_execution = None
        self._command_count = 0
        self._acked_commands = {}
        self._acked_steps = set()
        self._current_values = self._empty_current_values()
        self._current_values["storage_soc"] = previous_storage_soc
        self._pending_overrides = {}
        self._run_id = f"run-{uuid.uuid4().hex[:12]}"
        self._workflow_state = {
            "run_id": self._run_id,
            "sim_time": self._time.sim_time.isoformat(),
            "day": day,
            "step": self._time.current_step,
            "events": [],
            "workflow_status": "new",
            "current_node": "start",
            "error": None,
        }
        self._init_nodes()

    async def start_day(self, day: int) -> None:
        async with self._transition_lock:
            await self._start_day_locked(day)

    async def _start_day_locked(self, day: int) -> None:
        self._prepare_day(day)
        await self._invoke_graph_locked()
        if self._auto_approve:
            await self._submit_approval_locked("forecast_approval", "approve", "自动审批", "system")
            await self._submit_approval_locked("storage_approval", "approve", "自动审批", "system")
            await self._submit_approval_locked("hvac_approval", "approve", "自动审批", "system")

    async def _invoke_graph_locked(self) -> None:
        result = await self._graph.ainvoke(self._workflow_state)
        self._workflow_state = dict(result)
        self._archive.archive_workflow(
            self._workflow_state,
            self._time.sim_time.date(),
            self._run_id,
        )
        self._save_checkpoint()

    def _save_checkpoint(self) -> None:
        """Persist a RuntimeCheckpoint after each transition (G4)."""
        bindings: dict[str, dict[str, str]] = {}
        for gate_id, gate in self._approval_gates.items():
            bindings[gate_id] = {
                "status": gate.status.value if hasattr(gate.status, 'value') else str(gate.status),
                "report_id": gate.report_id or "",
                "report_hash": gate.report_hash or "",
            }
        cp = RuntimeCheckpoint(
            run_id=self._run_id,
            sim_time=self._time.sim_time.isoformat(),
            day=self._last_day,
            step=self._last_step,
            workflow_status=self._workflow_state.get("workflow_status", "new"),
            current_node=self._workflow_state.get("current_node", "idle"),
            subgraph="realtime" if self._dispatch_enabled else "day_ahead",
            approval_bindings=bindings,
            last_completed_tick=self._last_step if self._dispatch_enabled else None,
            last_command_id=(
                self._last_execution.command.command_id
                if self._last_execution else None
            ),
            last_ack_accepted=(
                self._last_execution.ack.accepted
                if self._last_execution else None
            ),
            dispatch_enabled=self._dispatch_enabled,
            acked_steps=sorted(
                step for (rid, step) in self._acked_commands if rid == self._run_id
            ),
            pending_recovery_reason="",
        )
        self._state_repo.save(self._run_id, cp)

    async def run_tick(self) -> None:
        if self._time.is_paused:
            return
        async with self._transition_lock:
            day = self._time.day_count
            step = self._time.current_step
            if day != self._last_day:
                await self._start_day_locked(day)
            if step != self._last_step and self._day_data:
                start_step = 0 if self._last_step < 0 else self._last_step + 1
                for missed_step in range(start_step, step + 1):
                    self._last_step = missed_step
                    await self._update_realtime_locked(missed_step)
            self._last_successful_tick_at = datetime.now().astimezone()
            self._last_error = None

    async def run_step(self, step: int) -> None:
        """Deterministic public simulation seam used by tests and offline replay."""
        if not 0 <= step < POINTS_PER_DAY:
            raise ValueError("step must be between 0 and 95")
        async with self._transition_lock:
            if self._day_data is None:
                await self._start_day_locked(self._time.day_count)
            self._last_step = step
            await self._update_realtime_locked(step)
            self._last_successful_tick_at = datetime.now().astimezone()

    async def reset(self) -> None:
        """Reset time and all domain state as one serialized operation."""
        async with self._transition_lock:
            self._time.reset()
            self._clear_all_state()
            await self._start_day_locked(0)

    # ------------------------------------------------------------------
    # LangGraph node handlers
    # ------------------------------------------------------------------

    async def _graph_data_agent(self, state: EnergySystemState) -> dict[str, Any]:
        self._set_node("data_collect", NodeStatus.RUNNING)
        sim_time = self._time.sim_time
        day = int(state.get("day", 0))
        day_data = generate_day_ahead_data(day, sim_time.month, sim_time.date())

        # Correct the source generator's month/day-index rollover and tariff context.
        midnight = datetime.combine(sim_time.date(), datetime.min.time())
        day_data["timestamps"] = [midnight + timedelta(minutes=15 * i) for i in range(POINTS_PER_DAY)]
        day_data["tariff_periods"] = [get_tariff_period(i / 4.0, sim_time.month) for i in range(POINTS_PER_DAY)]
        day_data["price_cny_per_kwh"] = [TARIFF_PRICES[p] for p in day_data["tariff_periods"]]
        event_impacts = self._apply_disturbances_to_day_data(day_data, sim_time.date())
        self._day_data = day_data
        self._set_node(
            "data_collect",
            NodeStatus.COMPLETED,
            f"已采集 {len(day_data['load_kw'])} 点数据 ({day_data['season']})",
        )
        self._set_node("data_archive", NodeStatus.RUNNING)
        data_facts = {
            "season": day_data["season"],
            "load_mean_kw": round(sum(day_data["load_kw"]) / 96, 1),
            "source_date": day_data.get("data_provenance", {}).get("load", {}).get("selected_date"),
            "event_impacts": event_impacts,
        }
        data_content, data_reasoning = await self._reasoning.explain(
            "data",
            f"采集季节:{day_data['season']} 负荷均值:{sum(day_data['load_kw'])/96:.0f}kW",
            data_facts,
            "解释本日数据质量、血缘和已应用扰动",
        )
        report = self._add_report(
            AgentType.DATA,
            "日前数据采集报告",
            data_content,
            {
                "season": day_data["season"],
                "load_mean": sum(day_data["load_kw"]) / 96,
                "compressor_capability": day_data.get("compressor_capability", {"capability": "monitor_only"}),
                "data_provenance": day_data.get("data_provenance", {}),
                "disturbance_impacts": event_impacts,
                "llm_reasoning": data_reasoning,
            },
            status="approved",
        )
        self._set_node("data_archive", NodeStatus.COMPLETED, f"数据报告 {report.report_id} 已封存")
        return {
            "data_agent_result": {"report_id": report.report_id, "report_hash": report.content_hash},
            "current_node": "data_agent",
            "events": {"type": "data_archived", "report_id": report.report_id},
        }

    async def _graph_prediction_agent(self, _: EnergySystemState) -> dict[str, Any]:
        assert self._day_data is not None
        self._set_node("prediction", NodeStatus.RUNNING)
        source_resolution = int(
            self._day_data.get("data_provenance", {}).get("load", {}).get("resolution_minutes", 15)
        )
        processed, processing = process_load_to_15min(
            self._day_data["load_kw"], source_resolution
        )
        self._day_data["load_kw"] = processed
        self._day_data["load_forecast"] = list(processed)
        self._day_data.setdefault("data_provenance", {})["load_processing"] = processing
        peak_index = processed.index(max(processed))
        prediction_facts = {
            "peak_kw": round(max(processed), 1),
            "mean_kw": round(sum(processed) / 96, 1),
            "peak_step": peak_index,
            "processing": processing,
        }
        prediction_content, prediction_reasoning = await self._reasoning.explain(
            "prediction",
            f"负荷已处理为15分钟粒度，峰值{max(processed):.0f}kW，均值{sum(processed)/96:.0f}kW",
            prediction_facts,
            "说明负荷粒度处理结果及当前不做未来预测的边界",
        )
        report = self._add_report(
            AgentType.PREDICTION,
            "日前负荷处理报告",
            prediction_content,
            {
                "forecast_kw": processed,
                "peak": max(processed),
                "mean": sum(processed) / 96,
                "peak_step": peak_index,
                "processing": processing,
                "reason": "当前版本仅做负荷质量校验与时间粒度转换，不生成未来负荷预测",
                "llm_reasoning": prediction_reasoning,
                "report_detail": self._build_report_detail("forecast", {
                    "forecast_kw": processed,
                    "peak": max(processed),
                    "mean": sum(processed) / 96,
                    "peak_step": peak_index,
                    "processing": processing,
                }),
            },
        )
        self._set_node("prediction", NodeStatus.COMPLETED, f"负荷处理报告 {report.report_id} 待审批")
        return {
            "prediction_result": {"report_id": report.report_id, "report_hash": report.content_hash},
            "current_node": "prediction_agent",
            "events": {"type": "load_processed", "report_id": report.report_id},
        }

    async def _graph_forecast_approval(self, state: EnergySystemState) -> dict[str, Any]:
        report = self._find_report(state["prediction_result"]["report_id"])
        gate = self._bind_gate("forecast_approval", report, f"峰值负荷 {report.data['peak']:.0f} kW")
        return {
            "forecast_approval": gate.model_dump(mode="json"),
            "workflow_status": "awaiting_forecast_approval",
            "current_node": "forecast_approval",
            "events": {"type": "approval_required", "gate_id": gate.gate_id},
        }

    async def _graph_storage_agent(self, _: EnergySystemState) -> dict[str, Any]:
        assert self._day_data is not None
        self._carbon_data = compute_carbon_factors(self._day_data["generation_mix"])
        self._set_node("storage_dispatch", NodeStatus.RUNNING)
        self._storage_plan = optimize_storage_dispatch(
            self._day_data["load_forecast"],
            self._day_data["price_cny_per_kwh"],
            carbon_factors=self._carbon_data["c_factors"],
            objective="min_cost",
            initial_soc=float(self._current_values.get("storage_soc", 0.5)),
            solar_kw=self._day_data["solar_kw"],
            max_power_kw_series=self._day_data.get("storage_power_limit_kw"),
        )
        storage_facts = {
            "saving_cny": self._storage_plan["saving_cny"],
            "terminal_soc": self._storage_plan["terminal_soc"],
            "max_temp_c": self._storage_plan["max_temp_c"],
            "solver_status": self._storage_plan["solver_status"],
            "violations": self._storage_plan.get("violations", []),
        }
        storage_content, storage_reasoning = await self._reasoning.explain(
            "storage",
            f"MILP优化 省{self._storage_plan['saving_cny']:.0f}元，末端SOC{self._storage_plan['terminal_soc']:.2%}",
            storage_facts,
            "解释储能优化结果、约束与审批重点",
        )
        self._storage_plan["llm_reasoning"] = storage_reasoning
        report = self._add_report(
            AgentType.STORAGE,
            "日前储能调度报告",
            storage_content,
            {
                **self._storage_plan,
                "report_detail": self._build_report_detail("storage", self._storage_plan),
            },
        )
        self._set_node("storage_dispatch", NodeStatus.COMPLETED, f"储能报告 {report.report_id} 待审批")
        return {
            "storage_result": {"report_id": report.report_id, "report_hash": report.content_hash},
            "events": {"type": "storage_plan_created", "report_id": report.report_id},
        }

    async def _graph_hvac_agent(self, _: EnergySystemState) -> dict[str, Any]:
        assert self._day_data is not None
        self._set_node("hvac_dispatch", NodeStatus.RUNNING)
        self._hvac_plan = optimize_hvac_dispatch(
            self._day_data["hvac_load_kw"],
            self._day_data["weather"]["temp_c"],
            self._day_data["price_cny_per_kwh"],
            self._day_data["tariff_periods"],
            available_chillers=self._day_data.get("available_chillers"),
        )
        hvac_facts = {
            "saving_cny": self._hvac_plan["saving_cny"],
            "avg_cop": self._hvac_plan["avg_cop"],
            "violations": self._hvac_plan.get("violations", []),
            "minimum_available_chillers": min(self._hvac_plan.get("available_chillers", [0])),
        }
        hvac_content, hvac_reasoning = await self._reasoning.explain(
            "hvac",
            f"冷机调度 省{self._hvac_plan['saving_cny']:.0f}元，平均COP{self._hvac_plan['avg_cop']:.1f}",
            hvac_facts,
            "解释冷机调度结果、设备可用性和容量风险",
        )
        self._hvac_plan["llm_reasoning"] = hvac_reasoning
        report = self._add_report(
            AgentType.HVAC,
            "日前HVAC调度报告",
            hvac_content,
            {
                **self._hvac_plan,
                "report_detail": self._build_report_detail("hvac", self._hvac_plan),
            },
        )
        self._set_node("hvac_dispatch", NodeStatus.COMPLETED, f"HVAC报告 {report.report_id} 待审批")
        return {
            "hvac_result": {"report_id": report.report_id, "report_hash": report.content_hash},
            "events": {"type": "hvac_plan_created", "report_id": report.report_id},
        }

    async def _graph_dispatch_approvals(self, state: EnergySystemState) -> dict[str, Any]:
        # Only rebind gates whose report has changed; keep already-approved gates
        # whose report binding is still valid (G2 local revision).
        new_storage_id = state["storage_result"]["report_id"]
        new_hvac_id = state["hvac_result"]["report_id"]
        storage_gate = self._approval_gates["storage_approval"]
        if storage_gate.report_id != new_storage_id:
            storage_report = self._find_report(new_storage_id)
            storage_gate = self._bind_gate(
                "storage_approval", storage_report,
                f"预计节省 {self._storage_plan['saving_cny']:.0f} 元",
            )
        hvac_gate = self._approval_gates["hvac_approval"]
        if hvac_gate.report_id != new_hvac_id:
            hvac_report = self._find_report(new_hvac_id)
            hvac_gate = self._bind_gate(
                "hvac_approval", hvac_report,
                f"预计节省 {self._hvac_plan['saving_cny']:.0f} 元",
            )
        return {
            "storage_approval": storage_gate.model_dump(mode="json"),
            "hvac_approval": hvac_gate.model_dump(mode="json"),
            "workflow_status": "awaiting_dispatch_approvals",
            "current_node": "dispatch_approvals",
            "events": [
                {"type": "approval_required", "gate_id": storage_gate.gate_id},
                {"type": "approval_required", "gate_id": hvac_gate.gate_id},
            ],
        }

    async def _graph_physical_dispatch(self, _: EnergySystemState) -> dict[str, Any]:
        storage_gate = self._approval_gates["storage_approval"]
        hvac_gate = self._approval_gates["hvac_approval"]
        if storage_gate.status != NodeStatus.APPROVED or hvac_gate.status != NodeStatus.APPROVED:
            raise ApprovalConflict("物理调度要求储能和 HVAC 报告均已批准")
        self._dispatch_enabled = True
        return {
            "physical_dispatch": {
                "enabled": True,
                "storage_report_id": storage_gate.report_id,
                "storage_report_hash": storage_gate.report_hash,
                "hvac_report_id": hvac_gate.report_id,
                "hvac_report_hash": hvac_gate.report_hash,
            },
            "workflow_status": "active",
            "current_node": "physical_dispatch",
            "events": {"type": "physical_dispatch_activated"},
        }

    async def _graph_monitor_agent(self, _: EnergySystemState) -> dict[str, Any]:
        self._set_node("monitor", NodeStatus.COMPLETED, "审批链完整，物理调度已激活")
        monitor_content, monitor_reasoning = await self._reasoning.explain(
            "monitor",
            "预测、储能和 HVAC 报告绑定校验通过，允许进入模拟物理执行闭环",
            {
                "dispatch_enabled": self._dispatch_enabled,
                "storage_gate": self._approval_gates["storage_approval"].status,
                "hvac_gate": self._approval_gates["hvac_approval"].status,
                "unacknowledged_alerts": len([item for item in self._alerts if not item.acknowledged]),
            },
            "说明审批联锁、执行资格和剩余风险",
        )
        report = self._add_report(
            AgentType.MONITOR,
            "调度激活监察报告",
            monitor_content,
            {"llm_reasoning": monitor_reasoning},
            status="approved",
        )
        return {
            "monitor_result": {"report_id": report.report_id, "status": "passed"},
            "workflow_status": "active",
            "current_node": "monitor_agent",
            "events": {"type": "monitor_passed", "report_id": report.report_id},
        }

    # ------------------------------------------------------------------
    # Approval domain
    # ------------------------------------------------------------------

    async def submit_approval(
        self,
        gate_id: str,
        decision: str,
        comment: str = "",
        actor: str = "engineer",
        report_id: str | None = None,
        report_hash: str | None = None,
    ) -> ApprovalGate:
        async with self._transition_lock:
            return await self._submit_approval_locked(
                gate_id, decision, comment, actor, report_id, report_hash
            )

    async def _submit_approval_locked(
        self,
        gate_id: str,
        decision: str,
        comment: str,
        actor: str,
        report_id: str | None = None,
        report_hash: str | None = None,
    ) -> ApprovalGate:
        gate = self._approval_gates.get(gate_id)
        if gate is None:
            raise ApprovalNotFound(f"审批门不存在: {gate_id}")
        if decision not in {"approve", "reject", "revise"}:
            raise InvalidApprovalDecision(f"不支持的审批决策: {decision}")
        if gate.status != NodeStatus.PENDING_APPROVAL:
            raise ApprovalConflict(f"审批门当前不可决策: {gate.status}")
        if report_id is not None and report_id != gate.report_id:
            raise ApprovalBindingMismatch("审批报告 ID 已过期或不匹配")
        if report_hash is not None and report_hash != gate.report_hash:
            raise ApprovalBindingMismatch("审批报告哈希已过期或不匹配")

        gate.decision = decision
        gate.comment = comment
        gate.decided_by = actor
        gate.decided_at = self._time.sim_time
        gate.status = (
            NodeStatus.APPROVED if decision == "approve"
            else NodeStatus.PENDING_APPROVAL if decision == "revise"
            else NodeStatus.REJECTED
        )
        if gate.report:
            gate.report.status = (
                "approved" if decision == "approve"
                else "pending" if decision == "revise"
                else "rejected"
            )
            self._archive.archive_report(
                gate.report,
                self._time.sim_time.date(),
                self._run_id,
                event_type="report_status_changed",
            )
        self._archive.archive_approval(gate, self._time.sim_time.date(), self._run_id)

        if decision == "revise":
            self._dispatch_enabled = False
            # Branch-level revision: only re-run the affected agent, keep the
            # other branch's valid approval intact (G2 local revision).
            if gate_id == "storage_approval":
                self._workflow_state["workflow_status"] = "storage_revision"
                self._workflow_state["current_node"] = gate_id
                await self._invoke_graph_locked()
                return self._approval_gates["storage_approval"]
            if gate_id == "hvac_approval":
                self._workflow_state["workflow_status"] = "hvac_revision"
                self._workflow_state["current_node"] = gate_id
                await self._invoke_graph_locked()
                return self._approval_gates["hvac_approval"]
            if gate_id == "forecast_approval":
                self._workflow_state["workflow_status"] = "forecast_revision"
                self._workflow_state["current_node"] = gate_id
                await self._invoke_graph_locked()
                return self._approval_gates["forecast_approval"]
            self._workflow_state["workflow_status"] = "revision_requested"
            self._workflow_state["current_node"] = gate_id
            self._save_checkpoint()
            return gate

        if decision == "reject":
            self._dispatch_enabled = False
            self._workflow_state["workflow_status"] = "rejected"
            self._workflow_state["current_node"] = gate_id
            if gate_id == "forecast_approval":
                self._storage_plan = None
                self._hvac_plan = None
            self._save_checkpoint()
            return gate

        if gate_id == "forecast_approval":
            self._workflow_state["forecast_approval"] = gate.model_dump(mode="json")
            self._workflow_state["workflow_status"] = "forecast_approved"
            await self._invoke_graph_locked()
        elif all(
            self._approval_gates[item].status == NodeStatus.APPROVED
            for item in ("storage_approval", "hvac_approval")
        ):
            self._workflow_state["storage_approval"] = self._approval_gates[
                "storage_approval"
            ].model_dump(mode="json")
            self._workflow_state["hvac_approval"] = self._approval_gates[
                "hvac_approval"
            ].model_dump(mode="json")
            self._workflow_state["workflow_status"] = "dispatch_approved"
            await self._invoke_graph_locked()
            self._finalize_day_ahead_accounting()
        return gate

    def _bind_gate(self, gate_id: str, report: AgentReport, description: str) -> ApprovalGate:
        gate = self._approval_gates[gate_id]
        gate.status = NodeStatus.PENDING_APPROVAL
        gate.description = description
        gate.report = report
        gate.report_id = report.report_id
        gate.report_hash = report.content_hash
        gate.decision = None
        gate.comment = ""
        gate.decided_at = None
        gate.decided_by = None
        return gate

    async def submit_control_action(
        self,
        *,
        system: str,
        action: str,
        target: str,
        value: float,
        unit: str,
        reason: str,
        actor: str,
    ) -> ControlActionRecord:
        """Validate, archive and queue a manual action for the next physical step."""
        async with self._transition_lock:
            if system not in {"overview", "storage", "hvac"}:
                raise InvalidControlAction(f"未知控制系统: {system}")
            if not action.strip() or not target.strip() or not actor.strip():
                raise InvalidControlAction("action、target 和 actor 不得为空")
            if system in {"storage", "hvac"}:
                gate_id = f"{system}_approval"
                if self._approval_gates[gate_id].status != NodeStatus.APPROVED:
                    raise ApprovalConflict(f"{system} 调度报告尚未批准，拒绝物理设定")
                self._validate_physical_action(system, target, value)
            elif target.strip().lower() != "demand_cap_kw":
                raise InvalidControlAction("园区策略当前仅支持 demand_cap_kw")

            record = ControlActionRecord(
                action_id=f"act-{uuid.uuid4().hex[:12]}",
                run_id=self._run_id,
                system=system,
                action=action.strip(),
                target=target.strip(),
                value=value,
                unit=unit.strip(),
                reason=reason.strip(),
                actor=actor.strip(),
                submitted_at=self._time.sim_time,
                status="executed" if system == "overview" else "accepted",
                applied_step=self._time.current_step if system == "overview" else None,
            )
            self._control_actions.insert(0, record)
            self._control_actions = self._control_actions[:100]
            self._archive.archive_control_action(
                record, self._time.sim_time.date(), self._run_id
            )
            if system in {"storage", "hvac"}:
                # Latest validated operator setpoint wins for that subsystem.
                self._pending_overrides[system] = record
            else:
                self._demand_cap_kw = value if value > 0 else None
                self._archive.archive_control_action(
                    record,
                    self._time.sim_time.date(),
                    self._run_id,
                    event_type="control_action_executed",
                )
            self._save_checkpoint()
            return record

    def _validate_physical_action(self, system: str, target: str, value: float) -> None:
        key = target.strip().lower()
        if system == "storage":
            if key not in {"power", "power_kw", "storage_power_kw", "目标功率"}:
                raise InvalidControlAction("当前储能执行器仅支持 power_kw 人工设定")
            max_power = max(
                float(STORAGE_DEFAULTS["max_charge_power_kw"]),
                float(STORAGE_DEFAULTS["max_discharge_power_kw"]),
            )
            if abs(value) > max_power:
                raise InvalidControlAction(f"储能功率必须位于 ±{max_power:g} kW")
            previous = float(self._current_values.get("storage_power_kw", 0.0))
            max_ramp = float(STORAGE_DEFAULTS["max_ramp_kw_per_step"])
            if abs(value - previous) > max_ramp:
                raise InvalidControlAction(f"储能功率变化不得超过单步爬坡边界 {max_ramp:g} kW")
        else:
            power_targets = {"power", "power_kw", "hvac_power_kw", "目标功率"}
            supply_targets = {"supply_temp", "supply_temp_c", "供水温度"}
            if key in supply_targets:
                if not 5.0 <= value <= 12.0:
                    raise InvalidControlAction("HVAC 供水温度必须位于 5–12°C")
            elif key not in power_targets:
                raise InvalidControlAction("当前 HVAC 执行器仅支持 power_kw 或 supply_temp_c")
            elif value < 0 or value > float(HVAC_DEFAULTS["total_rated_cooling_kw"]) / float(HVAC_DEFAULTS["cop_min"]):
                raise InvalidControlAction("HVAC 功率越过执行器安全边界")

    def get_control_actions(self, limit: int = 30) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 100))
        return [item.model_dump(mode="json") for item in self._control_actions[:bounded]]

    # ------------------------------------------------------------------
    # Physical execution and monitoring
    # ------------------------------------------------------------------

    async def _update_realtime_locked(self, step: int) -> None:
        if self._day_data is None:
            return
        # G3 idempotency: skip re-execution if this step was already ACKed.
        acked = self._acked_commands.get((self._run_id, step))
        if acked is not None:
            self._last_execution = acked
            return
        # Cross-restart idempotency: step was ACKed before restart.
        if step in self._acked_steps:
            return
        dd = self._day_data
        ts = dd["timestamps"][step]
        load = dd["load_kw"][step] * (1 + self._rng.gauss(0, 0.01))
        solar = dd["solar_kw"][step]
        previous_storage_power = float(self._current_values.get("storage_power_kw", 0.0))
        storage_power = 0.0
        soc = float(self._current_values.get("storage_soc", 0.5))
        storage_temp = float(self._current_values.get(
            "storage_temp_c", STORAGE_DEFAULTS["ambient_temperature_c"]
        ))
        baseline_hvac = (
            self._hvac_plan.get("baseline_power_kw", [])[step]
            if self._hvac_plan and self._hvac_plan.get("baseline_power_kw")
            else dd["hvac_load_kw"][step] / max(float(HVAC_DEFAULTS["cop_nominal"]), 1.0)
        )
        # Even without approved dispatch the BAS readings are generated at this tick;
        # they are never copied wholesale from a day-ahead plan.
        hvac_power = baseline_hvac * (1 + self._rng.gauss(0, 0.004))
        hvac_supply = float(self._current_values.get("hvac_supply_temp_c", 7.0))
        hvac_return = float(self._current_values.get("hvac_return_temp_c", 12.0))
        self._last_execution = None

        if self._dispatch_enabled and self._storage_plan and self._hvac_plan:
            storage_gate = self._approval_gates["storage_approval"]
            hvac_gate = self._approval_gates["hvac_approval"]
            storage_setpoint = self._storage_plan["power_kw"][step]
            hvac_setpoint = self._hvac_plan["power_kw"][step]
            hvac_supply_setpoint = self._hvac_plan["supply_temp_c"][step]
            hvac_return_setpoint = self._hvac_plan["return_temp_c"][step]
            if self._demand_cap_kw is not None:
                planned_grid = load - solar - storage_setpoint - (baseline_hvac - hvac_setpoint)
                storage_setpoint += max(0.0, planned_grid - self._demand_cap_kw)
                storage_setpoint = min(
                    float(STORAGE_DEFAULTS["max_discharge_power_kw"]), storage_setpoint
                )
            pending_actions = list(self._pending_overrides.values())
            for override in pending_actions:
                target = override.target.lower()
                if override.system == "storage":
                    storage_setpoint = override.value
                elif "power" in target or "功率" in target:
                    hvac_setpoint = override.value
                elif "supply" in target or "供水" in target:
                    hvac_supply_setpoint = override.value
            override_payload = (
                {"actions": [item.model_dump(mode="json") for item in pending_actions]}
                if pending_actions
                else None
            )
            try:
                execution = await self._executor.execute(
                    run_id=self._run_id,
                    step=step,
                    sim_time=ts,
                    storage_power_kw=storage_setpoint,
                    hvac_power_kw=hvac_setpoint,
                    hvac_supply_temp_c=hvac_supply_setpoint,
                    hvac_return_temp_c=hvac_return_setpoint,
                    previous_storage_power_kw=previous_storage_power,
                    previous_storage_soc=soc,
                    previous_storage_temp_c=storage_temp,
                    ambient_temp_c=float(dd["weather"]["temp_c"][step]),
                    storage_report_id=storage_gate.report_id or "",
                    storage_report_hash=storage_gate.report_hash or "",
                    hvac_report_id=hvac_gate.report_id or "",
                    hvac_report_hash=hvac_gate.report_hash or "",
                    manual_override=override_payload,
                )
            except ExecutionRejected as exc:
                self._dispatch_enabled = False
                self._add_alert(Severity.CRITICAL, "executor", str(exc))
            else:
                self._last_execution = execution
                self._command_count += 1
                self._acked_commands[(self._run_id, step)] = execution
                self._acked_steps.add(step)
                storage_power = execution.feedback.measured_storage_power_kw
                hvac_power = execution.feedback.measured_hvac_power_kw
                soc = execution.feedback.measured_storage_soc
                storage_temp = execution.feedback.measured_storage_temp_c
                hvac_supply = execution.feedback.measured_hvac_supply_temp_c
                hvac_return = execution.feedback.measured_hvac_return_temp_c
                for override in pending_actions:
                    override.status = "executed"
                    override.applied_step = step
                    override.command_id = execution.command.command_id
                    self._archive.archive_control_action(
                        override,
                        ts.date(),
                        self._run_id,
                        event_type="control_action_executed",
                    )
                    self._pending_overrides.pop(override.system, None)
                self._archive.archive_command(execution.command, ts.date(), self._run_id)
                self._archive.archive_ack(execution.ack, ts.date(), self._run_id)
                self._archive.archive_feedback(execution.feedback, ts.date(), self._run_id)
                if execution.feedback.max_deviation_ratio > self._executor.deviation_tolerance_ratio:
                    self._add_alert(
                        Severity.WARNING,
                        "executor",
                        f"执行偏差 {execution.feedback.max_deviation_ratio:.1%} 超过阈值",
                    )

        hvac_delta = baseline_hvac - hvac_power if self._dispatch_enabled else 0.0
        effective_load = load - hvac_delta
        grid = max(0.0, effective_load - solar - storage_power)
        c_factor = self._carbon_data["c_factors"][step] if self._carbon_data else 0.5
        price = dd["price_cny_per_kwh"][step]
        period = dd["tariff_periods"][step]

        dt_h = 0.25
        self._daily_metrics["energy"] += effective_load * dt_h
        self._daily_metrics["cost"] += grid * price * dt_h
        self._daily_metrics["carbon"] += grid * c_factor * dt_h
        self._daily_metrics["peak"] = max(self._daily_metrics["peak"], effective_load)
        values = {
            "load": load,
            "solar": solar,
            "grid": grid,
            "storage_power": storage_power,
            "soc": soc * 100,
            "temp": storage_temp,
            "hvac_power": hvac_power,
            "hvac_supply_temp": hvac_supply,
            "hvac_return_temp": hvac_return,
            "carbon": c_factor,
            "price": price,
        }
        for key, value in values.items():
            self._series_cache.setdefault(key, []).append(
                {"time": ts.isoformat(), "step": step, "value": round(value, 2)}
            )

        self._current_values = {
            "load_kw": round(load, 1),
            "solar_kw": round(solar, 1),
            "grid_kw": round(grid, 1),
            "storage_power_kw": round(storage_power, 1),
            "storage_soc": round(soc, 4),
            "storage_temp_c": round(storage_temp, 2),
            "hvac_power_kw": round(hvac_power, 1),
            "hvac_supply_temp_c": round(hvac_supply, 1),
            "hvac_return_temp_c": round(hvac_return, 1),
            "hvac_delta_kw": round(hvac_delta, 1),
            "carbon_factor": round(c_factor, 6),
            "price": round(price, 6),
            "tariff_period": period,
        }
        self._archive.append_realtime(
            {
                "sim_time": ts.isoformat(),
                "step": step,
                **self._current_values,
                "dispatch_enabled": self._dispatch_enabled,
                "command_id": self._last_execution.command.command_id if self._last_execution else "",
                "measurement_source": "simulation_executor" if self._last_execution else "bas_simulator",
            },
            ts.date(),
            self._run_id,
        )
        await self._monitor_check(step, soc, storage_temp, hvac_supply, load)
        await self._broadcast()
        self._save_checkpoint()

    async def _monitor_check(self, step: int, soc: float, temp: float, hvac_supply: float, load: float) -> None:
        self._set_node("monitor", NodeStatus.RUNNING)
        before = len(self._alerts)
        if not self._constraints.soc_min <= soc <= self._constraints.soc_max:
            self._add_alert(Severity.CRITICAL, "storage", f"SOC {soc:.1%} 越过安全范围")
        if temp > self._constraints.temp_max_c:
            self._add_alert(Severity.CRITICAL, "storage", f"电芯温度 {temp:.1f}°C 超过上限")
        elif temp > self._constraints.temp_max_c - 5:
            self._add_alert(Severity.WARNING, "storage", f"电芯温度 {temp:.1f}°C 接近上限")
        if not self._constraints.chilled_water_min_c <= hvac_supply <= self._constraints.chilled_water_max_c:
            self._add_alert(Severity.WARNING, "hvac", f"供水温度 {hvac_supply:.1f}°C 越过安全范围")
        if not self._constraints.load_min_kw <= load <= self._constraints.load_max_kw:
            self._add_alert(Severity.WARNING, "data", f"园区负荷 {load:.0f}kW 越过可信范围")
        new_alerts = len(self._alerts) - before
        self._set_node(
            "monitor",
            NodeStatus.WARNING if new_alerts else NodeStatus.COMPLETED,
            f"第{step}步发现{new_alerts}项新告警" if new_alerts else f"第{step}步监察正常",
        )

    def _finalize_day_ahead_accounting(self) -> None:
        if not self._storage_plan or not self._hvac_plan or not self._day_data or not self._carbon_data:
            return
        baseline_hvac = self._hvac_plan.get("baseline_power_kw", self._hvac_plan["power_kw"])
        optimized_hvac = self._hvac_plan["power_kw"]
        optimized_grid = [
            max(0.0, self._storage_plan["grid_kw"][step] -
                (baseline_hvac[step] - optimized_hvac[step]))
            for step in range(POINTS_PER_DAY)
        ]
        self._storage_plan["combined_grid_kw"] = [round(value, 1) for value in optimized_grid]
        self._tariff_data = calculate_tariff(optimized_grid, self._day_data["price_cny_per_kwh"])
        self._carbon_dispatch = account_dispatch_carbon(
            self._carbon_data["c_factors"],
            self._carbon_data["cr_factors"],
            self._day_data["load_forecast"],
            optimized_grid,
            0.25,
        )

    # ------------------------------------------------------------------
    # Reports, alerts and public state
    # ------------------------------------------------------------------

    def _set_node(
        self,
        node_id: str,
        status: NodeStatus,
        summary: str = "",
        duration_ms: int | None = None,
    ) -> None:
        node = self._agent_nodes.get(node_id)
        if node is None:
            return
        now = self._time.sim_time
        node.status = status
        node.last_result_summary = summary
        if status == NodeStatus.RUNNING:
            node.started_at = now
            node.completed_at = None
        elif status in (NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.WARNING):
            node.completed_at = now
            if node.started_at and duration_ms is None:
                duration_ms = int((now - node.started_at).total_seconds() * 1000)
            node.duration_ms = duration_ms

    def _build_report_detail(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Build one stable, UI-ready report contract for all approval pages."""
        dd = self._day_data or {}
        shifts = dd.get("schedule", {}).get("shifts", [])
        timestamps = dd.get("timestamps", [])
        loads = dd.get("load_forecast", dd.get("load_kw", []))
        prices = dd.get("price_cny_per_kwh", [])
        shift_labels = {"day": "白班", "evening": "晚班", "night": "夜班"}
        schedule_table = []
        for step in range(POINTS_PER_DAY):
            shift = shifts[step] if step < len(shifts) else {"shift": "unknown", "intensity": 0}
            timestamp = timestamps[step] if step < len(timestamps) else None
            schedule_table.append({
                "step": step,
                "time": timestamp.strftime("%H:%M") if timestamp else f"{step // 4:02d}:{step % 4 * 15:02d}",
                "shift": shift_labels.get(str(shift.get("shift")), str(shift.get("shift", "—"))),
                "production_intensity": shift.get("intensity", 0),
                "load_kw": round(float(loads[step]), 1) if step < len(loads) else None,
                "price_cny_per_kwh": round(float(prices[step]), 5) if step < len(prices) else None,
            })

        if kind == "forecast":
            plan_table = [{
                "step": row["step"], "time": row["time"], "forecast_kw": row["load_kw"],
                "shift": row["shift"], "production_intensity": row["production_intensity"],
            } for row in schedule_table]
            metrics = [
                {"label": "峰值负荷", "value": round(float(payload["peak"]), 1), "unit": "kW"},
                {"label": "平均负荷", "value": round(float(payload["mean"]), 1), "unit": "kW"},
                {"label": "峰值时刻", "value": schedule_table[int(payload["peak_step"])]["time"], "unit": ""},
            ]
            summary = "基于用电负荷_1h.xlsx实际小时数据，已转换为96点日前负荷，并加入小时内零均值扰动。"
            risks = ["源文件小时值按MW平均功率解释并换算为kW", "15分钟扰动保持每小时电量不变"]
        elif kind == "storage":
            plan_table = [{
                "step": row["step"], "time": row["time"],
                "power_kw": payload["power_kw"][row["step"]],
                "soc_ratio": payload["soc_ratio"][row["step"]],
                "temperature_c": payload["temp_c"][row["step"]],
                "grid_kw": payload["grid_kw"][row["step"]],
            } for row in schedule_table]
            metrics = [
                {"label": "预计节省", "value": payload["saving_cny"], "unit": "元"},
                {"label": "峰值削减", "value": payload["peak_reduction_kw"], "unit": "kW"},
                {"label": "末端SOC", "value": payload["terminal_soc"], "unit": "ratio"},
                {"label": "最高计划温度", "value": payload["max_temp_c"], "unit": "°C"},
            ]
            summary = "储能日前计划已完成容量、SOC、温度、爬坡及末端能量约束校核；实际功率、SOC和温度由实时执行反馈确定。"
            risks = list(payload.get("violations", []))
        else:
            plan_table = [{
                "step": row["step"], "time": row["time"],
                "power_kw": payload["power_kw"][row["step"]],
                "active_chillers": payload["active_chillers"][row["step"]],
                "cop": payload["cop"][row["step"]],
                "supply_temp_c": payload["supply_temp_c"][row["step"]],
                "return_temp_c": payload["return_temp_c"][row["step"]],
            } for row in schedule_table]
            metrics = [
                {"label": "预计节省", "value": payload["saving_cny"], "unit": "元"},
                {"label": "平均COP", "value": payload["avg_cop"], "unit": ""},
                {"label": "冷机总数", "value": payload["total_chillers"], "unit": "台"},
            ]
            summary = "HVAC日前计划已完成冷机台数、负载率和供回水温度校核；实际功率与温度由BAS实时反馈确定。"
            risks = list(payload.get("violations", [])) + list(payload.get("model_assumptions", []))
        return {
            "version": "1.0",
            "kind": kind,
            "situation_summary": summary,
            "schedule_table": schedule_table,
            "key_metrics": metrics,
            "plan_table": plan_table,
            "risks": risks,
            "data_provenance": dd.get("data_provenance", {}),
        }

    def _add_report(
        self,
        agent_type: AgentType,
        title: str,
        content: str,
        data: dict[str, Any] | None = None,
        status: str = "pending",
    ) -> AgentReport:
        report = AgentReport(
            report_id=f"rpt-{uuid.uuid4().hex[:10]}",
            agent_type=agent_type,
            title=title,
            content=content,
            data=data or {},
            created_at=self._time.sim_time,
            status=status,
            run_id=self._run_id,
        )
        canonical = json.dumps(
            report.model_dump(mode="json", exclude={"content_hash"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        report.content_hash = hashlib.sha256(canonical).hexdigest()
        self._reports.append(report)
        self._archive.archive_report(report, self._time.sim_time.date(), self._run_id)
        return report

    def _find_report(self, report_id: str) -> AgentReport:
        for report in reversed(self._reports):
            if report.report_id == report_id:
                return report
        raise LookupError(f"report not found: {report_id}")

    def get_report(self, report_id: str) -> dict[str, Any]:
        """Public immutable report view used by the dedicated report page."""
        return self._find_report(report_id).model_dump(mode="json")

    def _add_alert(self, severity: Severity, source: str, message: str) -> None:
        existing = [alert for alert in self._alerts if alert.message == message and not alert.acknowledged]
        if existing:
            return
        self._alerts.append(
            AlertItem(
                alert_id=f"alt-{uuid.uuid4().hex[:10]}",
                severity=severity,
                source=source,
                message=message,
                timestamp=self._time.sim_time,
            )
        )

    async def propose_disturbance(
        self,
        *,
        actor: str,
        actor_role: str,
        source_text: str,
        event_type: str,
        target: str,
        start_time: datetime,
        end_time: datetime | None,
        parameters: dict[str, Any] | None,
        summary: str,
        confidence: float = 1.0,
        parsed_by: str = "rule_fallback",
    ) -> DisturbanceEvent:
        async with self._transition_lock:
            event = DisturbanceEvent(
                event_id=f"evt-{uuid.uuid4().hex[:10]}",
                run_id=self._run_id,
                actor=actor,
                actor_role="facility" if actor_role == "facility" else "engineer",
                source_text=source_text,
                event_type=event_type,
                target=target or "园区",
                start_time=start_time,
                end_time=end_time,
                parameters=parameters or {},
                summary=summary,
                confidence=confidence,
                parsed_by=parsed_by,
                created_at=self._time.sim_time,
            )
            self._disturbance_events.insert(0, event)
            self._disturbance_events = self._disturbance_events[:200]
            self._archive.archive_disturbance(
                event, self._time.sim_time.date(), self._run_id
            )
            return event

    async def decide_disturbance(
        self,
        event_id: str,
        decision: str,
        actor: str,
    ) -> DisturbanceEvent:
        if decision not in {"apply", "cancel"}:
            raise ValueError("decision must be apply or cancel")
        async with self._transition_lock:
            event = next(
                (item for item in self._disturbance_events if item.event_id == event_id),
                None,
            )
            if event is None:
                raise LookupError(f"disturbance not found: {event_id}")
            if event.status != "proposed":
                raise ApprovalConflict(f"事件 {event_id} 已处理，当前状态为 {event.status}")
            event.decided_at = self._time.sim_time
            event.decided_by = actor
            if decision == "cancel":
                event.status = "cancelled"
                self._archive.archive_disturbance(
                    event,
                    self._time.sim_time.date(),
                    self._run_id,
                    event_type="disturbance_cancelled",
                )
                return event

            event.status = "applied"
            saved_metrics = dict(self._daily_metrics)
            saved_series = {key: list(value) for key, value in self._series_cache.items()}
            saved_values = dict(self._current_values)
            saved_last_step = self._last_step
            current_day = self._time.day_count
            self._prepare_day(current_day)
            self._daily_metrics = saved_metrics
            self._series_cache = saved_series
            self._current_values = saved_values
            self._last_step = saved_last_step
            await self._invoke_graph_locked()
            self._add_alert(
                Severity.WARNING,
                "disturbance",
                f"已应用事件并重算负荷处理链：{event.summary}",
            )
            self._archive.archive_disturbance(
                event,
                self._time.sim_time.date(),
                self._run_id,
                event_type="disturbance_applied_and_replanned",
            )
            return event

    def get_disturbance_events(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        return [item.model_dump(mode="json") for item in self._disturbance_events[:bounded]]

    def coordinate_agents(
        self,
        objective: str,
        requested_agents: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Run deterministic specialist assessments selected from a facility objective."""
        allowed = ["data_agent", "storage_agent", "hvac_agent", "monitor_agent"]
        requested = [item for item in (requested_agents or []) if item in allowed]
        selected = requested or allowed
        state = self.get_state()
        pending = [key for key, gate in state["approval_gates"].items()
                    if gate["status"] == NodeStatus.PENDING_APPROVAL]
        source = state.get("data_provenance", {}).get("load", {})
        findings = {
            "data_agent": (
                "数据Agent", f"核对负荷源{Path(str(source.get('path', ''))).name or '未知'}；"
                f"当前样本日{source.get('selected_date', '未知')}，目标粒度15分钟。"
            ),
            "storage_agent": (
                "储能Agent", f"当前SOC {state['storage_soc']:.1%}、功率{state['storage_power_kw']:.0f}kW；"
                "建议依据实时SOC/温度反馈滚动修正，禁止直接采用日前状态轨迹。"
            ),
            "hvac_agent": (
                "HVAC Agent", f"当前功率{state['hvac_power_kw']:.0f}kW、供水{state['hvac_supply_temp_c']:.1f}°C；"
                "建议结合冷机可用数、实时供回水温和电价执行温度重置。"
            ),
            "monitor_agent": (
                "监察Agent", f"待审批{len(pending)}项、未确认告警"
                f"{len([item for item in state['alerts'] if not item['acknowledged']])}项；"
                "物理动作继续受审批绑定和安全边界联锁。"
            ),
        }
        return [
            {"agent": agent, "label": findings[agent][0], "objective": objective,
             "finding": findings[agent][1], "status": "completed"}
            for agent in selected
        ]

    def _apply_disturbances_to_day_data(
        self,
        day_data: dict[str, Any],
        sim_date: date,
    ) -> list[dict[str, Any]]:
        original_load = [float(value) for value in day_data["load_kw"]]
        impacts: list[dict[str, Any]] = []
        day_start = datetime.combine(sim_date, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        day_data.setdefault(
            "available_chillers",
            [int(HVAC_DEFAULTS["chiller_count"])] * POINTS_PER_DAY,
        )
        day_data.setdefault(
            "storage_power_limit_kw",
            [float(STORAGE_DEFAULTS["max_discharge_power_kw"])] * POINTS_PER_DAY,
        )

        for event in reversed(self._disturbance_events):
            if event.status != "applied":
                continue
            event_end = event.end_time or (event.start_time + timedelta(minutes=15))
            if event.start_time >= day_end or event_end <= day_start:
                continue
            start = max(event.start_time, day_start)
            end = min(event_end, day_end)
            start_step = max(0, int((start - day_start).total_seconds() // 900))
            end_step = min(
                POINTS_PER_DAY,
                max(start_step + 1, int(((end - day_start).total_seconds() + 899) // 900)),
            )
            params = event.parameters
            target_key = event.target.lower()
            labels: list[str] = []

            if event.event_type == "equipment_failure":
                unavailable_units = max(1, int(float(params.get("unavailable_units", 1))))
                if "冷机" in target_key or "hvac" in target_key or "制冷" in target_key:
                    for step in range(start_step, end_step):
                        day_data["available_chillers"][step] = max(
                            0, int(day_data["available_chillers"][step]) - unavailable_units
                        )
                    labels.append(f"冷机可用数 -{unavailable_units}")
                if "储能" in target_key or "电池" in target_key:
                    residual = max(0.0, float(params.get("remaining_power_kw", 0.0)))
                    for step in range(start_step, end_step):
                        day_data["storage_power_limit_kw"][step] = min(
                            day_data["storage_power_limit_kw"][step], residual
                        )
                    labels.append(f"储能功率上限 {residual:.0f}kW")

            load_delta = float(params.get("load_delta_kw", 0.0))
            load_multiplier = float(params.get("load_multiplier", 1.0))
            if load_delta or load_multiplier != 1.0:
                for step in range(start_step, end_step):
                    day_data["load_kw"][step] = max(
                        0.0, day_data["load_kw"][step] * load_multiplier + load_delta
                    )
                labels.append(f"负荷 ×{load_multiplier:.3g} {load_delta:+.0f}kW")

            temperature_delta = float(params.get("temperature_delta_c", 0.0))
            if temperature_delta:
                for step in range(start_step, end_step):
                    day_data["weather"]["temp_c"][step] += temperature_delta
                labels.append(f"室外温度 {temperature_delta:+.1f}°C")

            price_multiplier = float(params.get("price_multiplier", 1.0))
            if price_multiplier != 1.0:
                for step in range(start_step, end_step):
                    day_data["price_cny_per_kwh"][step] *= price_multiplier
                labels.append(f"电价 ×{price_multiplier:.3g}")

            impact = {
                "event_id": event.event_id,
                "steps": [start_step, end_step - 1],
                "summary": "；".join(labels) if labels else "已记录运行事件，缺少定量影响参数",
            }
            event.impact_summary = impact["summary"]
            impacts.append(impact)

        # Keep the thermal-load share consistent when a production-load event changes demand.
        for step, before in enumerate(original_load):
            if before > 0 and day_data["load_kw"][step] != before:
                day_data["hvac_load_kw"][step] *= day_data["load_kw"][step] / before
        day_data["event_impacts"] = impacts
        return impacts

    def get_state(self) -> dict[str, Any]:
        dd = self._day_data or {}
        values = self._current_values
        return {
            "time": self._time.tick_info(),
            **values,
            "daily": {
                "energy_kwh": round(self._daily_metrics["energy"], 1),
                "cost_cny": round(self._daily_metrics["cost"], 2),
                "carbon_kg": round(self._daily_metrics["carbon"], 1),
                "peak_kw": round(self._daily_metrics["peak"], 1),
            },
            "agent_nodes": {key: value.model_dump(mode="json") for key, value in self._agent_nodes.items()},
            "approval_gates": {key: value.model_dump(mode="json") for key, value in self._approval_gates.items()},
            "reports": [report.model_dump(mode="json") for report in self._reports[-20:]],
            "alerts": [alert.model_dump(mode="json") for alert in self._alerts[-50:]],
            "series": {key: list(value) for key, value in self._series_cache.items()},
            "day_ahead": {
                "load_forecast": dd.get("load_forecast", dd.get("load_kw", [])),
                "storage_plan": self._storage_plan["power_kw"] if self._storage_plan else [],
                "soc_plan": self._storage_plan["soc_ratio"] if self._storage_plan else [],
                "hvac_plan": self._hvac_plan["power_kw"] if self._hvac_plan else [],
                "price": dd.get("price_cny_per_kwh", []),
                "carbon_c": self._carbon_data["c_factors"] if self._carbon_data else [],
                "carbon_cr": self._carbon_data["cr_factors"] if self._carbon_data else [],
            },
            "storage_summary": self._storage_plan,
            "hvac_summary": self._hvac_plan,
            "tariff_summary": self._tariff_data,
            "carbon_dispatch": self._carbon_dispatch,
            "chiller_topology": get_chiller_topology(),
            "weather": self._current_weather(),
            "data_provenance": dd.get("data_provenance", {}),
            "data_timeline": {
                "start": self._data_start.isoformat() if self._data_start else None,
                "end": self._data_end.isoformat() if self._data_end else None,
                "current_source_date": dd.get("data_provenance", {}).get("load", {}).get("selected_date"),
                "resolution_minutes": 15,
                "provenance": self._data_range_provenance,
            },
            "disturbances": self.get_disturbance_events(),
            "agent_llm": self._reasoning.status(),
            "workflow": {
                "run_id": self._run_id,
                "status": self._workflow_state.get("workflow_status", "idle"),
                "current_node": self._workflow_state.get("current_node", "idle"),
            },
            "checkpoint": self._get_checkpoint_view(),
            "physical_dispatch": {
                "enabled": self._dispatch_enabled,
                "last_execution": self._last_execution.model_dump(mode="json") if self._last_execution else None,
                "command_count": self._command_count,
            },
            "control_actions": self.get_control_actions(),
            "active_strategy": {
                "demand_cap_kw": self._demand_cap_kw,
                "enabled": self._demand_cap_kw is not None,
            },
        }

    def _get_checkpoint_view(self) -> dict[str, Any]:
        """Expose checkpoint/recovery state for the frontend (G4/§12.5)."""
        cp = self._state_repo.load(self._run_id)
        if cp is None:
            return {"available": False, "subgraph": "day_ahead", "last_tick": None}
        return {
            "available": True,
            "run_id": cp.run_id,
            "schema_version": cp.schema_version,
            "subgraph": cp.subgraph,
            "workflow_status": cp.workflow_status,
            "current_node": cp.current_node,
            "day": cp.day,
            "step": cp.step,
            "last_completed_tick": cp.last_completed_tick,
            "dispatch_enabled": cp.dispatch_enabled,
            "last_command_id": cp.last_command_id,
            "last_ack_accepted": cp.last_ack_accepted,
            "updated_at": cp.updated_at.isoformat(),
            "approval_bindings": cp.approval_bindings,
        }

    def _current_weather(self) -> dict[str, float]:
        if not self._day_data:
            return {"temp_c": 25.0, "humidity": 60.0, "wind": 3.0}
        step = min(self._last_step if self._last_step >= 0 else self._time.current_step, POINTS_PER_DAY - 1)
        weather = self._day_data["weather"]
        return {
            "temp_c": weather["temp_c"][step],
            "humidity": weather["humidity_pct"][step],
            "wind": weather["wind_speed_ms"][step],
        }

    def acknowledge_alert(self, alert_id: str) -> bool:
        for alert in self._alerts:
            if alert.alert_id == alert_id:
                alert.acknowledged = True
                return True
        return False

    def set_auto_approve(self, enabled: bool) -> None:
        self._auto_approve = enabled

    def pause(self) -> None:
        self._time.pause()

    def resume(self) -> None:
        self._time.resume()

    def record_runtime_error(self, exc: BaseException) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"

    def health(self) -> dict[str, Any]:
        if self._last_error:
            status = "degraded"
        elif self._day_data is None:
            status = "starting"
        else:
            status = "healthy"
        return {
            "status": status,
            "ready": self._day_data is not None,
            "last_error": self._last_error,
            "last_successful_tick_at": self._last_successful_tick_at.isoformat()
            if self._last_successful_tick_at
            else None,
            "started_at": self._started_at.isoformat(),
            "workflow_status": self._workflow_state.get("workflow_status", "idle"),
            "agent_llm": self._reasoning.status(),
        }

    async def _broadcast_once(self) -> None:
        state = self.get_state()
        message = json.dumps({"type": "state_update", "data": state}, ensure_ascii=False, default=str)
        with self._ws_lock:
            clients = list(self._ws_clients)
        if not clients:
            return

        async def send_one(ws: Any) -> tuple[Any, bool]:
            try:
                await asyncio.wait_for(ws.send_text(message), timeout=2.0)
                return ws, True
            except Exception:
                return ws, False

        results = await asyncio.gather(*(send_one(ws) for ws in clients))
        with self._ws_lock:
            for ws, ok in results:
                if not ok:
                    self._ws_clients.discard(ws)

    async def _broadcast(self) -> None:
        async with self._broadcast_lock:
            await self._broadcast_once()

    async def broadcast_state(self) -> None:
        """Push a control/approval mutation immediately instead of waiting for a tick."""
        await self._broadcast()

    def add_ws_client(self, ws: Any) -> None:
        with self._ws_lock:
            self._ws_clients.add(ws)

    def remove_ws_client(self, ws: Any) -> None:
        with self._ws_lock:
            self._ws_clients.discard(ws)


_engine: SimulationEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> SimulationEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = SimulationEngine()
    return _engine
