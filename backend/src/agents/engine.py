"""LangGraph-driven industrial energy simulation and approval-safe execution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import threading
import uuid
from datetime import datetime, timedelta
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
    EnergySystemState,
    NodeStatus,
    PhysicsConstraints,
    Severity,
)
from core.time_engine import TimeEngine, get_time_engine
from data.simulator import generate_day_ahead_data
from graph.workflow import build_energy_workflow


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
    ):
        self._rng = random.Random(123)
        self._constraints = PhysicsConstraints()
        self._time = time_engine or get_time_engine()
        self._archive = ArchiveStore(archive_root)
        self._executor = executor or SimulationExecutor()
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
        self._current_values = self._empty_current_values()
        self._pending_overrides = {}
        self._init_nodes()

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
            ("prediction", AgentType.PREDICTION, "负荷预测"),
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
            ("forecast_approval", "预测审批", "负荷预测报告工程师审批"),
            ("storage_approval", "储能审批", "储能调度策略工程师审批"),
            ("hvac_approval", "HVAC审批", "HVAC调度策略工程师审批"),
        ]:
            self._approval_gates[gate_id] = ApprovalGate(
                gate_id=gate_id,
                name=name,
                description=description,
            )

    def _prepare_day(self, day: int) -> None:
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
        self._current_values = self._empty_current_values()
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

    async def run_tick(self) -> None:
        if self._time.is_paused:
            return
        async with self._transition_lock:
            day = self._time.day_count
            step = self._time.current_step
            if day != self._last_day:
                await self._start_day_locked(day)
            if step != self._last_step and self._day_data:
                self._last_step = step
                await self._update_realtime_locked(step)
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
        day_data = generate_day_ahead_data(day, sim_time.month)

        # Correct the source generator's month/day-index rollover and tariff context.
        midnight = datetime.combine(sim_time.date(), datetime.min.time())
        day_data["timestamps"] = [midnight + timedelta(minutes=15 * i) for i in range(POINTS_PER_DAY)]
        day_data["tariff_periods"] = [get_tariff_period(i / 4.0, sim_time.month) for i in range(POINTS_PER_DAY)]
        day_data["price_cny_per_kwh"] = [TARIFF_PRICES[p] for p in day_data["tariff_periods"]]
        self._day_data = day_data
        self._set_node(
            "data_collect",
            NodeStatus.COMPLETED,
            f"已采集 {len(day_data['load_kw'])} 点数据 ({day_data['season']})",
        )
        self._set_node("data_archive", NodeStatus.RUNNING)
        report = self._add_report(
            AgentType.DATA,
            "日前数据采集报告",
            f"采集季节:{day_data['season']} 负荷均值:{sum(day_data['load_kw'])/96:.0f}kW",
            {
                "season": day_data["season"],
                "load_mean": sum(day_data["load_kw"]) / 96,
                "compressor_capability": day_data.get("compressor_capability", {"capability": "monitor_only"}),
                "data_provenance": day_data.get("data_provenance", {}),
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
        forecast = self._gen_forecast(self._day_data["load_kw"])
        self._day_data["load_forecast"] = forecast
        peak_index = forecast.index(max(forecast))
        report = self._add_report(
            AgentType.PREDICTION,
            "日前负荷预测报告",
            f"预测峰值{max(forecast):.0f}kW，均值{sum(forecast)/96:.0f}kW",
            {
                "forecast_kw": forecast,
                "peak": max(forecast),
                "mean": sum(forecast) / 96,
                "peak_step": peak_index,
                "reason": "基于校准负荷基线、排班、气象修正与受控随机误差",
            },
        )
        self._set_node("prediction", NodeStatus.COMPLETED, f"负荷预测报告 {report.report_id} 待审批")
        return {
            "prediction_result": {"report_id": report.report_id, "report_hash": report.content_hash},
            "current_node": "prediction_agent",
            "events": {"type": "forecast_created", "report_id": report.report_id},
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
            solar_kw=self._day_data["solar_kw"],
        )
        report = self._add_report(
            AgentType.STORAGE,
            "日前储能调度报告",
            f"MILP优化 省{self._storage_plan['saving_cny']:.0f}元，末端SOC{self._storage_plan['terminal_soc']:.2%}",
            self._storage_plan,
        )
        self._set_node("storage_dispatch", NodeStatus.COMPLETED, f"储能报告 {report.report_id} 待审批")
        return {
            "storage_result": {"report_id": report.report_id, "report_hash": report.content_hash},
            "current_node": "storage_agent",
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
        )
        report = self._add_report(
            AgentType.HVAC,
            "日前HVAC调度报告",
            f"冷机调度 省{self._hvac_plan['saving_cny']:.0f}元，平均COP{self._hvac_plan['avg_cop']:.1f}",
            self._hvac_plan,
        )
        self._set_node("hvac_dispatch", NodeStatus.COMPLETED, f"HVAC报告 {report.report_id} 待审批")
        return {
            "hvac_result": {"report_id": report.report_id, "report_hash": report.content_hash},
            "current_node": "hvac_agent",
            "events": {"type": "hvac_plan_created", "report_id": report.report_id},
        }

    async def _graph_dispatch_approvals(self, state: EnergySystemState) -> dict[str, Any]:
        storage_report = self._find_report(state["storage_result"]["report_id"])
        hvac_report = self._find_report(state["hvac_result"]["report_id"])
        storage_gate = self._bind_gate(
            "storage_approval", storage_report, f"预计节省 {self._storage_plan['saving_cny']:.0f} 元"
        )
        hvac_gate = self._bind_gate(
            "hvac_approval", hvac_report, f"预计节省 {self._hvac_plan['saving_cny']:.0f} 元"
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
        report = self._add_report(
            AgentType.MONITOR,
            "调度激活监察报告",
            "预测、储能和 HVAC 报告绑定校验通过，允许进入模拟物理执行闭环",
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
        gate.status = NodeStatus.APPROVED if decision == "approve" else NodeStatus.REJECTED
        if gate.report:
            gate.report.status = "approved" if decision == "approve" else "rejected"
            self._archive.archive_report(
                gate.report,
                self._time.sim_time.date(),
                self._run_id,
                event_type="report_status_changed",
            )
        self._archive.archive_approval(gate, self._time.sim_time.date(), self._run_id)

        if decision != "approve":
            self._dispatch_enabled = False
            self._workflow_state["workflow_status"] = "rejected"
            self._workflow_state["current_node"] = gate_id
            if gate_id == "forecast_approval":
                self._storage_plan = None
                self._hvac_plan = None
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
                self._archive.archive_control_action(
                    record,
                    self._time.sim_time.date(),
                    self._run_id,
                    event_type="control_action_executed",
                )
            return record

    @staticmethod
    def _validate_physical_action(system: str, target: str, value: float) -> None:
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
        else:
            power_targets = {"power", "power_kw", "hvac_power_kw", "目标功率"}
            supply_targets = {"supply_temp", "supply_temp_c", "供水温度"}
            if key in supply_targets:
                if not 5.0 <= value <= 12.0:
                    raise InvalidControlAction("HVAC 供水温度必须位于 5–12°C")
            elif key not in power_targets:
                raise InvalidControlAction("当前 HVAC 执行器仅支持 power_kw 或 supply_temp_c")

    def get_control_actions(self, limit: int = 30) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 100))
        return [item.model_dump(mode="json") for item in self._control_actions[:bounded]]

    # ------------------------------------------------------------------
    # Physical execution and monitoring
    # ------------------------------------------------------------------

    async def _update_realtime_locked(self, step: int) -> None:
        if self._day_data is None:
            return
        dd = self._day_data
        ts = dd["timestamps"][step]
        load = dd["load_kw"][step] * (1 + self._rng.gauss(0, 0.01))
        solar = dd["solar_kw"][step]
        storage_power = 0.0
        soc = 0.5
        storage_temp = float(STORAGE_DEFAULTS["ambient_temperature_c"])
        baseline_hvac = (
            self._hvac_plan.get("baseline_power_kw", [])[step]
            if self._hvac_plan and self._hvac_plan.get("baseline_power_kw")
            else dd["hvac_load_kw"][step] / max(float(HVAC_DEFAULTS["cop_nominal"]), 1.0)
        )
        hvac_power = baseline_hvac
        hvac_supply = 7.0
        self._last_execution = None

        if self._dispatch_enabled and self._storage_plan and self._hvac_plan:
            storage_gate = self._approval_gates["storage_approval"]
            hvac_gate = self._approval_gates["hvac_approval"]
            storage_setpoint = self._storage_plan["power_kw"][step]
            hvac_setpoint = self._hvac_plan["power_kw"][step]
            pending_actions = list(self._pending_overrides.values())
            for override in pending_actions:
                target = override.target.lower()
                if override.system == "storage":
                    storage_setpoint = override.value
                elif "power" in target or "功率" in target:
                    hvac_setpoint = override.value
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
                storage_power = execution.feedback.measured_storage_power_kw
                hvac_power = execution.feedback.measured_hvac_power_kw
                soc = self._storage_plan["soc_ratio"][step]
                storage_temp = self._storage_plan["temp_c"][step]
                hvac_supply = self._hvac_plan["supply_temp_c"][step]
                for override in pending_actions:
                    if override.system == "hvac" and (
                        "supply" in override.target.lower() or "供水" in override.target
                    ):
                        hvac_supply = override.value
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
            },
            ts.date(),
            self._run_id,
        )
        await self._monitor_check(step, soc, storage_temp, hvac_supply, load)
        await self._broadcast()

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
        if not self._storage_plan or not self._day_data or not self._carbon_data:
            return
        optimized_grid = self._storage_plan["grid_kw"]
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

    def _gen_forecast(self, actual_load: list[float]) -> list[float]:
        return [max(0.0, load * (1 + self._rng.gauss(0, 0.02))) for load in actual_load]

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
            "workflow": {
                "run_id": self._run_id,
                "status": self._workflow_state.get("workflow_status", "idle"),
                "current_node": self._workflow_state.get("current_node", "idle"),
            },
            "physical_dispatch": {
                "enabled": self._dispatch_enabled,
                "last_execution": self._last_execution.model_dump(mode="json") if self._last_execution else None,
                "command_count": self._command_count,
            },
            "control_actions": self.get_control_actions(),
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
