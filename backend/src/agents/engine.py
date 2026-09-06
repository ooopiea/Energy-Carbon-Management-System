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
    SIM_STEP_MINUTES,
    STORAGE_DEFAULTS,
    TARIFF_PRICES,
    get_env,
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
    FacilityAction,
    NodeStatus,
    PhysicsConstraints,
    Severity,
)
from core.state import PendingDayPlan
from core.time_engine import TimeEngine, get_time_engine
from data.raw_loader import get_load_data_range, process_load_to_15min
from data.simulator import generate_day_ahead_data
from graph.workflow import build_energy_workflow
from agents.reasoning import AgentReasoningService
from workflows.phase import TickInput
from workflows.realtime import RealtimeTickEngine
from llm.glm_client import GlmClient
from repositories.state_repository import (
    InMemoryStateRepository,
    StateRepository,
    RuntimeCheckpoint,
)


class ApprovalError(ValueError):
    pass

# Storage SOC daily boundary defaults (R5): each day starts and ends at min safe level.
# Single source of truth lives in config.STORAGE_DEFAULTS; these aliases keep
# existing imports working while reading from the same configuration dict.
STORAGE_DEFAULT_INITIAL_SOC = float(STORAGE_DEFAULTS["default_initial_soc_ratio"])
STORAGE_DEFAULT_TERMINAL_SOC = float(STORAGE_DEFAULTS["default_terminal_soc_ratio"])


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
        simulation_loop: bool | None = None,
    ):
        self._rng = random.Random(123)
        self._constraints = PhysicsConstraints()
        data_start, data_end, data_range_provenance = get_load_data_range()
        self._data_start = data_start
        self._data_end = data_end
        self._data_range_provenance = data_range_provenance
        if simulation_loop is None:
            simulation_loop = get_env("ENERGY_SIMULATION_LOOP", "false").strip().lower() in {
                "1",
                "true",
                "yes",
            }
        self._simulation_loop = bool(simulation_loop)
        self._simulation_day_count = (
            (data_end - data_start).days + 1
            if data_start is not None and data_end is not None and data_end >= data_start
            else 0
        )
        loop_end = (
            datetime.combine(data_end + timedelta(days=1), datetime.min.time())
            if self._simulation_loop and data_end is not None
            else None
        )
        resolved_start = datetime.combine(data_start, datetime.min.time()) if data_start else None
        self._time = time_engine or get_time_engine(resolved_start, loop_end_time=loop_end)
        if time_engine is not None and loop_end is not None and hasattr(time_engine, "configure_loop"):
            time_engine.configure_loop(loop_end)
        self._archive = ArchiveStore(archive_root)
        self._executor = executor or SimulationExecutor()
        self._tick_engine = RealtimeTickEngine(executor=self._executor, rng=self._rng)
        self._state_repo = state_repo or InMemoryStateRepository()
        self._reasoning = AgentReasoningService(glm_client)
        self._transition_lock = asyncio.Lock()
        self._broadcast_lock = asyncio.Lock()
        self._ws_clients: set[Any] = set()
        self._ws_lock = threading.RLock()
        self._auto_approve = get_env("ENERGY_AUTO_APPROVE", "false").strip().lower() in {
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
        self._facility_actions: list[FacilityAction] = []
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
        self._monthly_peak_kw: float = 0.0
        self._monthly_peak_month: int | None = None
        self._daily_soc_override: dict[str, float] | None = None
        self._objective_mode: str = "weighted"
        self._pending_overrides = {}
        self._init_nodes()
        self._pending_day_plan: PendingDayPlan | None = None
        self._last_cycle = getattr(self._time, "cycle_count", 0)

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
            "storage_soc": STORAGE_DEFAULT_INITIAL_SOC,
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
        previous_storage_soc = float(
            self._current_values.get("storage_soc", STORAGE_DEFAULT_INITIAL_SOC)
        )
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
        self._objective_mode = "weighted"
        self._daily_soc_override = None
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

    def _sync_realtime_soc_to_plan(self) -> None:
        """Align realtime SOC to the day-ahead plan's initial_soc.

        Called after the day-ahead workflow (or facility-driven re-optimization)
        produces a storage plan, but only before any realtime tick has run for
        the day. This guarantees the realtime feedback curve shares the same
        origin as the SOC plan, so they overlap on the frontend chart.
        """
        if self._storage_plan and self._last_step < 0:
            self._current_values["storage_soc"] = float(
                self._storage_plan["initial_soc"]
            )

    async def start_day(self, day: int) -> None:
        async with self._transition_lock:
            await self._start_day_locked(day)

    async def _start_day_locked(self, day: int) -> None:
        promoted = False
        if (
            day > 0
            and self._pending_day_plan is not None
            and self._pending_day_plan.target_day == day
            and self._pending_day_plan.status == "approved"
        ):
            promoted = await self._promote_pending_day_locked(day)
        if not promoted:
            self._prepare_day(day)
            await self._invoke_graph_locked()
            self._sync_realtime_soc_to_plan()
            if self._auto_approve:
                await self._auto_approve_all_gates_locked()
        self._generate_next_day_plan_locked(day + 1)

    async def _auto_approve_all_gates_locked(self) -> None:
        """Auto-approve all three gates (day 0 and unapproved-next-day fallback)."""
        await self._submit_approval_locked("forecast_approval", "approve", "自动审批", "system")
        await self._submit_approval_locked("storage_approval", "approve", "自动审批", "system")
        await self._submit_approval_locked("hvac_approval", "approve", "自动审批", "system")

    async def _promote_pending_day_locked(self, day: int) -> bool:
        """Promote an approved PendingDayPlan to the live execution state."""
        plan = self._pending_day_plan
        if plan is None or not plan.storage_plan or not plan.hvac_plan:
            return False
        self._prepare_day(day)
        self._day_data = plan.day_data
        self._carbon_data = plan.carbon_data
        self._storage_plan = dict(plan.storage_plan)
        self._hvac_plan = dict(plan.hvac_plan)
        self._objective_mode = plan.objective_mode
        self._daily_soc_override = dict(plan.daily_soc_override) if plan.daily_soc_override else None
        if self._day_data:
            self._apply_disturbances_to_day_data(self._day_data, self._time.sim_time.date())
        self._sync_realtime_soc_to_plan()
        storage_report = self._add_report(
            AgentType.STORAGE, "次日储能调度报告（审批通过）",
            f"已审批方案提升为执行方案，省{self._storage_plan['saving_cny']:.0f}元",
            {**self._storage_plan, "report_detail": self._build_report_detail("storage", self._storage_plan)},
        )
        hvac_report = self._add_report(
            AgentType.HVAC, "次日HVAC调度报告（审批通过）",
            f"已审批方案提升为执行方案，省{self._hvac_plan['saving_cny']:.0f}元",
            {**self._hvac_plan, "report_detail": self._build_report_detail("hvac", self._hvac_plan)},
        )
        forecast = self._day_data.get("load_forecast", [])
        forecast_report = self._add_report(
            AgentType.PREDICTION, "次日负荷预测（审批通过）",
            f"已审批预测方案，峰值{max(forecast) if forecast else 0:.0f} kW",
            {"forecast_kw": forecast, "peak": max(forecast) if forecast else 0,
             "mean": sum(forecast) / len(forecast) if forecast else 0,
             "peak_step": forecast.index(max(forecast)) if forecast else 0,
             "report_detail": self._build_report_detail("forecast", {
                 "forecast_kw": forecast, "peak": max(forecast) if forecast else 0,
                 "mean": sum(forecast) / len(forecast) if forecast else 0,
                 "peak_step": forecast.index(max(forecast)) if forecast else 0})},
        )
        self._bind_gate("forecast_approval", forecast_report, "已审批")
        self._bind_gate("storage_approval", storage_report, f"省{self._storage_plan['saving_cny']:.0f}元")
        self._bind_gate("hvac_approval", hvac_report, f"省{self._hvac_plan['saving_cny']:.0f}元")
        self._approval_gates["forecast_approval"].status = NodeStatus.APPROVED
        self._approval_gates["storage_approval"].status = NodeStatus.APPROVED
        self._approval_gates["hvac_approval"].status = NodeStatus.APPROVED
        self._dispatch_enabled = True
        self._finalize_day_ahead_accounting()
        plan.status = "executed"
        self._pending_day_plan = None
        self._workflow_state["workflow_status"] = "active"
        self._workflow_state["current_node"] = "physical_dispatch"
        self._set_node("monitor", NodeStatus.COMPLETED, "已审批次日方案直接提升为执行方案")
        self._save_checkpoint()
        return True

    def _generate_next_day_plan_locked(self, next_day: int) -> None:
        """Pre-generate D+1 day-ahead plan for review and approval during D-day."""
        if self._simulation_loop and next_day >= self._simulation_day_count:
            return
        sim_time = self._time.sim_time
        next_date = sim_time.date() + timedelta(days=1)
        try:
            next_data = generate_day_ahead_data(next_day, next_date.month, next_date)
        except Exception:
            return
        midnight = datetime.combine(next_date, datetime.min.time())
        next_data["timestamps"] = [
            midnight + timedelta(minutes=15 * i) for i in range(POINTS_PER_DAY)
        ]
        next_data["tariff_periods"] = [
            get_tariff_period(i / 4.0, next_date.month) for i in range(POINTS_PER_DAY)
        ]
        next_data["price_cny_per_kwh"] = [TARIFF_PRICES[p] for p in next_data["tariff_periods"]]
        # Process load data to 15-min resolution (same as prediction agent).
        source_res = int(next_data.get("data_provenance", {}).get("load", {}).get("resolution_minutes", 15))
        processed_load, _ = process_load_to_15min(next_data["load_kw"], source_res)
        next_data["load_kw"] = processed_load
        next_data["load_forecast"] = list(processed_load)
        carbon = compute_carbon_factors(
            next_data["generation_mix"],
            external_cr_factors=next_data.get("cr_factors"),
        )
        storage_plan = optimize_storage_dispatch(
            next_data["load_forecast"],
            next_data["price_cny_per_kwh"],
            carbon_factors=carbon["c_factors"],
            objective="weighted",
            carbon_price_cny_per_ton=80.0,
            initial_soc=STORAGE_DEFAULT_INITIAL_SOC,
            terminal_soc=STORAGE_DEFAULT_TERMINAL_SOC,
            solar_kw=next_data["solar_kw"],
            max_power_kw_series=next_data.get("storage_power_limit_kw"),
            ambient_temp_c_series=next_data["weather"]["temp_c"],
        )
        hvac_plan = optimize_hvac_dispatch(
            next_data["hvac_load_kw"],
            next_data["weather"]["temp_c"],
            next_data["price_cny_per_kwh"],
            next_data["tariff_periods"],
            available_chillers=next_data.get("available_chillers"),
        )
        self._pending_day_plan = PendingDayPlan(
            target_day=next_day,
            target_date=next_date.isoformat(),
            day_data=next_data,
            storage_plan=storage_plan,
            hvac_plan=hvac_plan,
            carbon_data=carbon,
            objective_mode="weighted",
            gate_status={
                "forecast_approval": "approved",
                "storage_approval": "approved",
                "hvac_approval": "approved",
            },
            created_at=sim_time,
        )
        self._pending_day_plan.status = "approved"

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
            cycle = getattr(self._time, "cycle_count", 0)
            if cycle != self._last_cycle:
                self._clear_all_state()
                await self._start_day_locked(day)
            elif day != self._last_day:
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
        self._carbon_data = compute_carbon_factors(
            self._day_data["generation_mix"],
            external_cr_factors=self._day_data.get("cr_factors"),
        )
        self._set_node("storage_dispatch", NodeStatus.RUNNING)
        soc_override = self._daily_soc_override or {}
        initial_soc = float(soc_override.get("initial_soc", STORAGE_DEFAULT_INITIAL_SOC))
        terminal_soc = float(soc_override.get("terminal_soc", STORAGE_DEFAULT_TERMINAL_SOC))
        self._storage_plan = optimize_storage_dispatch(
            self._day_data["load_forecast"],
            self._day_data["price_cny_per_kwh"],
            carbon_factors=self._carbon_data["c_factors"],
            objective=self._objective_mode,
            carbon_price_cny_per_ton=80.0,
            initial_soc=initial_soc,
            terminal_soc=terminal_soc,
            solar_kw=self._day_data["solar_kw"],
            max_power_kw_series=self._day_data.get("storage_power_limit_kw"),
            ambient_temp_c_series=self._day_data["weather"]["temp_c"],
        )
        # Align realtime SOC origin to the plan's initial_soc so the two
        # curves (day-ahead plan vs realtime feedback) share the same start.
        self._sync_realtime_soc_to_plan()
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
            return await self._submit_control_action_locked(
                system=system, action=action, target=target, value=value,
                unit=unit, reason=reason, actor=actor,
            )

    async def _submit_control_action_locked(
        self, *, system: str, action: str, target: str, value: float,
        unit: str, reason: str, actor: str,
    ) -> ControlActionRecord:
        """Core control-action logic; caller must already hold _transition_lock."""
        if system not in {"overview", "storage", "hvac"}:
            raise InvalidControlAction(f"未知控制系统: {system}")
        if not action.strip() or not target.strip() or not actor.strip():
            raise InvalidControlAction("action、target 和 actor 不得为空")
        if system in {"storage", "hvac"}:
            gate_id = f"{system}_approval"
            if self._approval_gates[gate_id].status != NodeStatus.APPROVED:
                raise ApprovalConflict(f"{system} 调度报告尚未批准，拒绝物理设定")
            self._validate_physical_action(system, target, value)
            existing = self._pending_overrides.get(system)
            if existing is not None:
                existing.status = "rejected"
                existing.applied_step = self._time.current_step
                self._archive.archive_control_action(
                    existing, self._time.sim_time.date(), self._run_id,
                    event_type="control_action_superseded",
                )
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
            self._pending_overrides[system] = record
        else:
            self._demand_cap_kw = value if value > 0 else None
            self._objective_mode = self._parse_objective_mode(reason)
            self._archive.archive_control_action(
                record,
                self._time.sim_time.date(),
                self._run_id,
                event_type="control_action_executed",
            )
        self._save_checkpoint()
        return record

    @staticmethod
    def _parse_objective_mode(reason: str) -> str:
        for token in reason.split("="):
            token = token.strip()
            if token in ("cost", "carbon", "weighted"):
                return "min_cost" if token == "cost" else "min_carbon" if token == "carbon" else "weighted"
        return "weighted"

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
        if step in self._acked_steps:
            return

        dd = self._day_data
        ts = dd["timestamps"][step]

        # Inject carbon factors so the tick engine can access them.
        if self._carbon_data and "carbon_c_factors" not in dd:
            dd["carbon_c_factors"] = self._carbon_data["c_factors"]

        storage_gate = self._approval_gates["storage_approval"]
        hvac_gate = self._approval_gates["hvac_approval"]

        tick_input = TickInput(
            run_id=self._run_id,
            step=step,
            sim_time=ts.isoformat(),
            day_data=dd,
            storage_plan=self._storage_plan if self._dispatch_enabled else None,
            hvac_plan=self._hvac_plan if self._dispatch_enabled else None,
            storage_report_id=storage_gate.report_id or "",
            storage_report_hash=storage_gate.report_hash or "",
            hvac_report_id=hvac_gate.report_id or "",
            hvac_report_hash=hvac_gate.report_hash or "",
            previous_values=self._current_values,
            demand_cap_kw=self._demand_cap_kw,
            monthly_peak_kw=self._monthly_peak_kw,
            pending_overrides=self._pending_overrides,
        )

        self._last_execution = None
        result = await self._tick_engine.run_tick(tick_input)

        # Process alerts raised by the tick engine.
        for alert in result.alerts:
            severity = (
                Severity.CRITICAL
                if alert.get("severity") == "critical"
                else Severity.WARNING
            )
            self._add_alert(severity, alert.get("source", "executor"), alert.get("message", ""))

        # Handle execution result and archiving.
        pending_actions = list(self._pending_overrides.values())
        if result.dispatched and result.execution:
            execution = result.execution
            self._last_execution = execution
            self._command_count += 1
            self._acked_commands[(self._run_id, step)] = execution
            self._acked_steps.add(step)
            for override in pending_actions:
                override.status = "executed"
                override.applied_step = step
                override.command_id = execution.command.command_id
                self._archive.archive_control_action(
                    override, ts.date(), self._run_id,
                    event_type="control_action_executed",
                )
                self._pending_overrides.pop(override.system, None)
            self._archive.archive_command(execution.command, ts.date(), self._run_id)
            self._archive.archive_ack(execution.ack, ts.date(), self._run_id)
            self._archive.archive_feedback(execution.feedback, ts.date(), self._run_id)
        elif self._dispatch_enabled and self._storage_plan and self._hvac_plan:
            # Execution was active but failed/rejected: fail-closed.
            self._dispatch_enabled = False

        # Apply metrics delta.
        for key, delta in result.metrics_delta.items():
            if key == "peak":
                self._daily_metrics["peak"] = max(self._daily_metrics["peak"], delta)
            elif key in self._daily_metrics:
                self._daily_metrics[key] += delta

        # Apply measurements to current values.
        m = result.measurements
        period = dd["tariff_periods"][step]
        self._current_values = {
            "load_kw": m["load_kw"],
            "solar_kw": m["solar_kw"],
            "grid_kw": m["grid_kw"],
            "storage_power_kw": m["storage_power_kw"],
            "storage_power_setpoint_kw": m.get("storage_power_setpoint_kw", 0.0),
            "storage_soc": m["storage_soc"],
            "storage_temp_c": m["storage_temp_c"],
            "hvac_power_kw": m["hvac_power_kw"],
            "hvac_supply_temp_c": m["hvac_supply_temp_c"],
            "hvac_return_temp_c": m["hvac_return_temp_c"],
            "hvac_delta_kw": m["hvac_delta_kw"],
            "carbon_factor": m["carbon_factor"],
            "price": m["price"],
            "tariff_period": period,
        }

        # Update series cache (key names match existing frontend contract).
        series_values = {
            "load": m["load_kw"],
            "solar": m["solar_kw"],
            "grid": m["grid_kw"],
            "storage_power": m["storage_power_kw"],
            "soc": m["storage_soc"] * 100,
            "temp": m["storage_temp_c"],
            "hvac_power": m["hvac_power_kw"],
            "hvac_supply_temp": m["hvac_supply_temp_c"],
            "hvac_return_temp": m["hvac_return_temp_c"],
            "carbon": m["carbon_factor"],
            "price": m["price"],
        }
        for key, value in series_values.items():
            self._series_cache.setdefault(key, []).append(
                {"time": ts.isoformat(), "step": step, "value": round(value, 2)}
            )

        # Archive realtime record.
        self._archive.append_realtime(
            {
                "sim_time": ts.isoformat(),
                "step": step,
                **self._current_values,
                "dispatch_enabled": self._dispatch_enabled,
                "command_id": (
                    self._last_execution.command.command_id
                    if self._last_execution
                    else ""
                ),
                "measurement_source": (
                    "simulation_executor" if self._last_execution else "bas_simulator"
                ),
            },
            ts.date(),
            self._run_id,
        )

        # Track monthly peak demand for smart peak-shaving (R3).
        current_month = self._time.sim_time.month
        if self._monthly_peak_month != current_month:
            self._monthly_peak_kw = 0.0
            self._monthly_peak_month = current_month
        if m["grid_kw"] > self._monthly_peak_kw:
            self._monthly_peak_kw = m["grid_kw"]

        await self._monitor_check(
            step, m["storage_soc"], m["storage_temp_c"],
            m["hvac_supply_temp_c"], m["load_kw"],
        )
        self._check_facility_action_monitoring(m)
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


    # ------------------------------------------------------------------
    # Facility action management (phase-aware structured actions)
    # ------------------------------------------------------------------

    def get_current_phase(self) -> str:
        """Return current scheduling phase: day_ahead or realtime."""
        return "realtime" if self._dispatch_enabled else "day_ahead"

    def get_facility_actions(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        return [item.model_dump(mode="json") for item in self._facility_actions[:bounded]]

    def get_pending_day_plan(self) -> dict[str, Any] | None:
        """Return the D+1 pending plan snapshot for the frontend / chat service."""
        if self._pending_day_plan is None:
            return None
        p = self._pending_day_plan
        return {
            "target_day": p.target_day,
            "target_date": p.target_date,
            "status": p.status,
            "gate_status": p.gate_status,
            "objective_mode": p.objective_mode,
            "daily_soc_override": p.daily_soc_override,
            "modifications": p.modifications,
            "metrics": p.metric_summary(),
        }

    def preview_next_day_modification(
        self,
        *,
        target_system: str = "storage",
        objective_mode: str = "weighted",
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Shadow-optimise on the pending D+1 day_data and return before/after metrics."""
        plan = self._pending_day_plan
        if plan is None or not plan.day_data:
            return {"before": {}, "after": {}, "diff": {}, "error": "no pending day plan"}
        params = parameters or {}
        before = plan.metric_summary()
        after_storage: dict[str, Any] = {}
        after_hvac: dict[str, Any] = {}
        soc_ov = plan.daily_soc_override or {}
        if target_system in ("storage", "overview"):
            initial_soc = float(params.get("initial_soc", soc_ov.get("initial_soc", STORAGE_DEFAULT_INITIAL_SOC)))
            terminal_soc = float(params.get("terminal_soc", soc_ov.get("terminal_soc", STORAGE_DEFAULT_TERMINAL_SOC)))
            sp = optimize_storage_dispatch(
                plan.day_data["load_forecast"],
                plan.day_data["price_cny_per_kwh"],
                carbon_factors=plan.carbon_data["c_factors"] if plan.carbon_data else None,
                objective=objective_mode,
                carbon_price_cny_per_ton=params.get("carbon_price_cny_per_ton", 80.0),
                initial_soc=initial_soc,
                terminal_soc=terminal_soc,
                solar_kw=plan.day_data["solar_kw"],
                max_power_kw_series=self._resolve_power_limit_series(params),
                ambient_temp_c_series=plan.day_data["weather"]["temp_c"],
            )
            after_storage = {
                "storage_saving_cny": sp.get("saving_cny", 0),
                "storage_terminal_soc": sp.get("terminal_soc", 0),
                "peak_reduction_kw": sp.get("peak_reduction_kw", 0),
            }
        if target_system in ("hvac", "overview"):
            hp = optimize_hvac_dispatch(
                plan.day_data["hvac_load_kw"],
                plan.day_data["weather"]["temp_c"],
                plan.day_data["price_cny_per_kwh"],
                plan.day_data["tariff_periods"],
                available_chillers=self._resolve_chiller_override(params),
            )
            after_hvac = {
                "hvac_saving_cny": hp.get("saving_cny", 0),
                "hvac_avg_cop": hp.get("avg_cop", 0),
            }
        after = {**after_storage, **after_hvac}
        diff = {k: round(after.get(k, 0) - before.get(k, 0), 2) for k in set(list(before) + list(after))}
        return {"before": before, "after": after, "diff": diff}

    async def apply_next_day_modification(
        self,
        *,
        target_system: str = "storage",
        objective_mode: str = "weighted",
        parameters: dict[str, Any] | None = None,
        reasoning: str = "",
        confidence: float = 0.8,
        actor: str = "facility",
    ) -> FacilityAction:
        """Write a modification into the pending D+1 plan and create a FacilityAction."""
        async with self._transition_lock:
            return await self._apply_next_day_modification_locked(
                target_system=target_system, objective_mode=objective_mode,
                parameters=parameters, reasoning=reasoning,
                confidence=confidence, actor=actor,
            )

    async def _apply_next_day_modification_locked(
        self, *, target_system: str = "storage", objective_mode: str = "weighted",
        parameters: dict[str, Any] | None = None, reasoning: str = "",
        confidence: float = 0.8, actor: str = "facility",
    ) -> FacilityAction:
        """Core D+1 modification logic; caller must already hold _transition_lock."""
        plan = self._pending_day_plan
        if plan is None:
            raise ValueError("no pending day plan available")
        params = parameters or {}
        full_params: dict[str, Any] = {**params}
        if objective_mode:
            full_params["objective_mode"] = objective_mode
        soc_ov = dict(plan.daily_soc_override or {})
        if "initial_soc" in params:
            soc_ov["initial_soc"] = float(params["initial_soc"])
        if "terminal_soc" in params:
            soc_ov["terminal_soc"] = float(params["terminal_soc"])
        plan.daily_soc_override = soc_ov or None
        if "power_limit_windows" in params:
            plan.day_data["storage_power_limit_kw"] = self._resolve_power_limit_series(params)
        if "available_chillers_override" in params:
            plan.day_data["available_chillers"] = self._resolve_chiller_override(params)
        if target_system in ("storage", "overview"):
            sp = optimize_storage_dispatch(
                plan.day_data["load_forecast"],
                plan.day_data["price_cny_per_kwh"],
                carbon_factors=plan.carbon_data["c_factors"] if plan.carbon_data else None,
                objective=objective_mode,
                carbon_price_cny_per_ton=params.get("carbon_price_cny_per_ton", 80.0),
                initial_soc=float(soc_ov.get("initial_soc", STORAGE_DEFAULT_INITIAL_SOC)),
                terminal_soc=float(soc_ov.get("terminal_soc", STORAGE_DEFAULT_TERMINAL_SOC)),
                solar_kw=plan.day_data["solar_kw"],
                max_power_kw_series=plan.day_data.get("storage_power_limit_kw"),
                ambient_temp_c_series=plan.day_data["weather"]["temp_c"],
            )
            plan.storage_plan = sp
            plan.objective_mode = objective_mode
        if target_system in ("hvac", "overview"):
            hp = optimize_hvac_dispatch(
                plan.day_data["hvac_load_kw"],
                plan.day_data["weather"]["temp_c"],
                plan.day_data["price_cny_per_kwh"],
                plan.day_data["tariff_periods"],
                available_chillers=plan.day_data.get("available_chillers"),
            )
            plan.hvac_plan = hp
        preview = self.preview_next_day_modification(
            target_system=target_system, objective_mode=objective_mode, parameters=params)
        action = FacilityAction(
            proposal_id=f"fac-{uuid.uuid4().hex[:10]}",
            action_type="day_ahead_modification",
            target_system=target_system,
            parameters=full_params,
            impact_preview=preview,
            reasoning=reasoning,
            confidence=confidence,
            target_day=plan.target_day,
            created_at=self._time.sim_time,
        )
        self._facility_actions.insert(0, action)
        self._facility_actions = self._facility_actions[:200]
        plan.modifications.append({
            "target_system": target_system,
            "objective_mode": objective_mode,
            "parameters": full_params,
            "reasoning": reasoning,
            "proposal_id": action.proposal_id,
            "at": self._time.sim_time.isoformat(),
        })
        return action

    async def confirm_next_day_modification(self, proposal_id: str, actor: str) -> FacilityAction:
        """Confirm a pending D+1 modification action."""
        action = next((a for a in self._facility_actions if a.proposal_id == proposal_id), None)
        if action is None:
            raise LookupError(f"facility action not found: {proposal_id}")
        if action.status != "proposed":
            raise ApprovalConflict(f"action {proposal_id} already processed: {action.status}")
        action.status = "applied"
        action.decided_at = self._time.sim_time
        action.decided_by = actor
        action.monitor_steps_remaining = 24
        if self._pending_day_plan and self._pending_day_plan.target_day == action.target_day:
            p = self._pending_day_plan
            if "objective_mode" in action.parameters:
                p.objective_mode = action.parameters["objective_mode"]
            p.status = "approved"
        self._save_checkpoint()
        return action

    async def revise_facility_action(
        self, proposal_id: str, parameters: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        """Cancel an old proposal and create a revised one with new parameters + fresh preview."""
        old = next((a for a in self._facility_actions if a.proposal_id == proposal_id), None)
        if old is None:
            raise LookupError(f"facility action not found: {proposal_id}")
        if old.status != "proposed":
            raise ApprovalConflict(f"action {proposal_id} already processed: {old.status}")
        old.status = "cancelled"
        old.decided_at = self._time.sim_time
        old.decided_by = actor
        new_params = {**old.parameters, **parameters}
        objective_mode = str(new_params.get("objective_mode", old.parameters.get("objective_mode", "weighted")))
        target_system = old.target_system
        preview = self.preview_next_day_modification(
            target_system=target_system, objective_mode=objective_mode,
            parameters={k: v for k, v in new_params.items() if k != "objective_mode"})
        action = FacilityAction(
            proposal_id=f"fac-{uuid.uuid4().hex[:10]}",
            action_type=old.action_type,
            target_system=target_system,
            parameters=new_params,
            impact_preview=preview,
            reasoning=old.reasoning,
            confidence=old.confidence,
            target_day=old.target_day,
            created_at=self._time.sim_time,
        )
        self._facility_actions.insert(0, action)
        self._facility_actions = self._facility_actions[:200]
        return {"proposal_id": action.proposal_id, "action": action.model_dump(mode="json"),
                "preview": preview}

    async def propose_facility_action(
        self,
        *,
        action_type: str,
        target_system: str,
        parameters: dict[str, Any] | None = None,
        reasoning: str = "",
        confidence: float = 0.8,
        impact_preview: dict[str, Any] | None = None,
        actor: str = "facility",
    ) -> FacilityAction:
        async with self._transition_lock:
            action = FacilityAction(
                proposal_id=f"fac-{uuid.uuid4().hex[:10]}",
                action_type=action_type,
                target_system=target_system,
                parameters=parameters or {},
                impact_preview=impact_preview or {},
                reasoning=reasoning,
                confidence=confidence,
                created_at=self._time.sim_time,
            )
            self._facility_actions.insert(0, action)
            self._facility_actions = self._facility_actions[:200]
            return action

    async def confirm_facility_action(
        self,
        proposal_id: str,
        actor: str,
    ) -> FacilityAction:
        async with self._transition_lock:
            action = next(
                (item for item in self._facility_actions if item.proposal_id == proposal_id),
                None,
            )
            if action is None:
                raise LookupError(f"facility action not found: {proposal_id}")
            if action.status != "proposed":
                raise ApprovalConflict(f"action {proposal_id} already processed: {action.status}")

            action.decided_at = self._time.sim_time
            action.decided_by = actor

            if action.action_type == "day_ahead_modification":
                if action.target_day is not None:
                    # D+1 next-day modification: already applied to pending plan
                    # at proposal time; just mark confirmed and update plan status.
                    if self._pending_day_plan and self._pending_day_plan.target_day == action.target_day:
                        self._pending_day_plan.status = "approved"
                else:
                    await self._apply_day_ahead_modification_locked(action)
            elif action.action_type == "realtime_override":
                await self._apply_realtime_override_locked(action, actor)
            elif action.action_type == "demand_cap":
                self._apply_demand_cap_locked(action)
            elif action.action_type == "mission":
                pass  # Mission actions are executed by the bridge
            else:
                raise ValueError(f"unknown action type: {action.action_type}")

            action.status = "applied"
            action.monitor_steps_remaining = 24
            self._save_checkpoint()
            return action

    async def cancel_facility_action(
        self,
        proposal_id: str,
        actor: str,
    ) -> FacilityAction:
        async with self._transition_lock:
            action = next(
                (item for item in self._facility_actions if item.proposal_id == proposal_id),
                None,
            )
            if action is None:
                raise LookupError(f"facility action not found: {proposal_id}")
            if action.status != "proposed":
                raise ApprovalConflict(f"action {proposal_id} already processed: {action.status}")
            action.status = "cancelled"
            action.decided_at = self._time.sim_time
            action.decided_by = actor
            return action

    async def preview_day_ahead_modification(
        self,
        *,
        target_system: str,
        objective_mode: str = "weighted",
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Preview impact of day-ahead parameter changes without applying them."""
        params = parameters or {}
        before = self._extract_plan_metrics()

        if not self._day_data:
            return {"before": before, "after": {}, "diff": {}, "error": "no day data"}

        try:
            after_storage: dict[str, Any] = {}
            after_hvac: dict[str, Any] = {}
            if target_system in ("storage", "overview"):
                after_storage = self._preview_storage_optimization(objective_mode, params)
            if target_system in ("hvac", "overview"):
                after_hvac = self._preview_hvac_optimization(params)
        except Exception as exc:
            return {"before": before, "after": {}, "diff": {}, "error": str(exc)}

        after = {**after_storage, **after_hvac}
        return {
            "before": before,
            "after": after,
            "diff": self._compute_metric_diff(before, after),
        }

    def _preview_storage_optimization(
        self, objective_mode: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        assert self._day_data is not None
        soc_override = self._daily_soc_override or {}
        initial_soc = float(params.get("initial_soc", soc_override.get("initial_soc", STORAGE_DEFAULT_INITIAL_SOC)))
        terminal_soc = float(params.get("terminal_soc", soc_override.get("terminal_soc", STORAGE_DEFAULT_TERMINAL_SOC)))
        kwargs: dict[str, Any] = dict(
            load_kw=self._day_data["load_forecast"],
            price_cny_per_kwh=self._day_data["price_cny_per_kwh"],
            carbon_factors=self._carbon_data["c_factors"] if self._carbon_data else None,
            objective=objective_mode,
            carbon_price_cny_per_ton=params.get("carbon_price_cny_per_ton", 80.0),
            initial_soc=initial_soc,
            terminal_soc=terminal_soc,
            solar_kw=self._day_data["solar_kw"],
            max_power_kw_series=self._resolve_power_limit_series(params),
            ambient_temp_c_series=self._day_data["weather"]["temp_c"],
        )
        plan = optimize_storage_dispatch(**kwargs)
        return {
            "storage_saving_cny": plan.get("saving_cny", 0),
            "storage_terminal_soc": plan.get("terminal_soc", 0),
            "storage_max_temp_c": plan.get("max_temp_c", 0),
            "peak_reduction_kw": plan.get("peak_reduction_kw", 0),
            "solver_status": plan.get("solver_status", "unknown"),
        }

    def _preview_hvac_optimization(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._day_data is not None
        available = self._resolve_chiller_override(params)
        plan = optimize_hvac_dispatch(
            self._day_data["hvac_load_kw"],
            self._day_data["weather"]["temp_c"],
            self._day_data["price_cny_per_kwh"],
            self._day_data["tariff_periods"],
            available_chillers=available,
        )
        return {
            "hvac_saving_cny": plan.get("saving_cny", 0),
            "hvac_avg_cop": plan.get("avg_cop", 0),
        }

    def _resolve_power_limit_series(self, params: dict[str, Any]) -> list[float] | None:
        windows = params.get("power_limit_windows")
        if not windows:
            return self._day_data.get("storage_power_limit_kw") if self._day_data else None
        max_power = float(STORAGE_DEFAULTS["max_discharge_power_kw"])
        series = [max_power] * POINTS_PER_DAY
        for window in windows:
            start = int(window.get("start_step", 0))
            end = int(window.get("end_step", POINTS_PER_DAY))
            limit = float(window.get("max_kw", max_power))
            for i in range(max(0, start), min(POINTS_PER_DAY, end)):
                series[i] = min(series[i], limit)
        return series

    def _resolve_chiller_override(self, params: dict[str, Any]) -> list[int] | None:
        overrides = params.get("available_chillers_override")
        if not overrides:
            return self._day_data.get("available_chillers") if self._day_data else None
        series = [int(HVAC_DEFAULTS["chiller_count"])] * POINTS_PER_DAY
        for item in overrides:
            start = int(item.get("start_step", 0))
            end = int(item.get("end_step", POINTS_PER_DAY))
            count = int(item.get("count", HVAC_DEFAULTS["chiller_count"]))
            for i in range(max(0, start), min(POINTS_PER_DAY, end)):
                series[i] = count
        return series

    def _extract_plan_metrics(self) -> dict[str, Any]:
        m: dict[str, Any] = {}
        if self._storage_plan:
            m["storage_saving_cny"] = self._storage_plan.get("saving_cny", 0)
            m["storage_terminal_soc"] = self._storage_plan.get("terminal_soc", 0)
            m["storage_max_temp_c"] = self._storage_plan.get("max_temp_c", 0)
            m["peak_reduction_kw"] = self._storage_plan.get("peak_reduction_kw", 0)
        if self._hvac_plan:
            m["hvac_saving_cny"] = self._hvac_plan.get("saving_cny", 0)
            m["hvac_avg_cop"] = self._hvac_plan.get("avg_cop", 0)
        m["daily_cost_cny"] = round(self._daily_metrics["cost"], 1)
        m["daily_peak_kw"] = round(self._daily_metrics["peak"], 1)
        m["daily_carbon_kg"] = round(self._daily_metrics["carbon"], 1)
        return m

    @staticmethod
    def _compute_metric_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        diff: dict[str, Any] = {}
        for key in set(list(before.keys()) + list(after.keys())):
            b = before.get(key, 0)
            a = after.get(key, 0)
            if isinstance(b, (int, float)) and isinstance(a, (int, float)):
                diff[key] = round(a - b, 2)
        return diff

    async def _apply_day_ahead_modification_locked(self, action: FacilityAction) -> None:
        """Apply day-ahead parameter changes and re-run the affected agents."""
        params = action.parameters
        target = action.target_system
        if target in ("storage", "overview"):
            if "objective_mode" in params:
                self._objective_mode = params["objective_mode"]
            if "power_limit_windows" in params and self._day_data:
                self._day_data["storage_power_limit_kw"] = self._resolve_power_limit_series(params)
            if "carbon_price_cny_per_ton" in params and self._day_data:
                self._day_data["_facility_carbon_price"] = float(params["carbon_price_cny_per_ton"])
        if target in ("hvac", "overview") and "available_chillers_override" in params and self._day_data:
            self._day_data["available_chillers"] = self._resolve_chiller_override(params)

        if self._day_data and self._carbon_data:
            terminal_soc = params.get("terminal_soc")
            initial_soc_override = params.get("initial_soc")
            soc_override = self._daily_soc_override or {}
            if initial_soc_override is not None or terminal_soc is not None:
                override = dict(soc_override)
                if initial_soc_override is not None:
                    override["initial_soc"] = float(initial_soc_override)
                if terminal_soc is not None:
                    override["terminal_soc"] = float(terminal_soc)
                self._daily_soc_override = override
            if target in ("storage", "overview"):
                soc_ov = self._daily_soc_override or {}
                self._storage_plan = optimize_storage_dispatch(
                    self._day_data["load_forecast"],
                    self._day_data["price_cny_per_kwh"],
                    carbon_factors=self._carbon_data["c_factors"],
                    objective=self._objective_mode,
                    carbon_price_cny_per_ton=params.get("carbon_price_cny_per_ton", 80.0),
                    initial_soc=float(soc_ov.get("initial_soc", STORAGE_DEFAULT_INITIAL_SOC)),
                    solar_kw=self._day_data["solar_kw"],
                    max_power_kw_series=self._day_data.get("storage_power_limit_kw"),
                    ambient_temp_c_series=self._day_data["weather"]["temp_c"],
                    terminal_soc=float(soc_ov.get("terminal_soc", STORAGE_DEFAULT_TERMINAL_SOC)),
                )
                report = self._add_report(
                    AgentType.STORAGE,
                    "facility-revised storage dispatch",
                    f"day-ahead storage re-optimized: {action.reasoning}",
                    {**self._storage_plan, "report_detail": self._build_report_detail("storage", self._storage_plan)},
                )
                self._bind_gate("storage_approval", report, f"revised saving {self._storage_plan['saving_cny']:.0f}")
                self._sync_realtime_soc_to_plan()
            if target in ("hvac", "overview"):
                self._hvac_plan = optimize_hvac_dispatch(
                    self._day_data["hvac_load_kw"],
                    self._day_data["weather"]["temp_c"],
                    self._day_data["price_cny_per_kwh"],
                    self._day_data["tariff_periods"],
                    available_chillers=self._day_data.get("available_chillers"),
                )
                report = self._add_report(
                    AgentType.HVAC,
                    "facility-revised hvac dispatch",
                    f"day-ahead hvac re-optimized: {action.reasoning}",
                    {**self._hvac_plan, "report_detail": self._build_report_detail("hvac", self._hvac_plan)},
                )
                self._bind_gate("hvac_approval", report, f"revised saving {self._hvac_plan['saving_cny']:.0f}")
            # In realtime phase, facility modifications reset the approval
            # gates. Re-approve them so dispatch stays consistent and the
            # operator can still submit realtime overrides afterwards.
            if self._dispatch_enabled:
                if target in ("storage", "overview"):
                    await self._submit_approval_locked("storage_approval", "approve", "facility-revised", action.decided_by or "facility")
                if target in ("hvac", "overview"):
                    await self._submit_approval_locked("hvac_approval", "approve", "facility-revised", action.decided_by or "facility")

    async def _apply_realtime_override_locked(self, action: FacilityAction, actor: str) -> None:
        """Apply a real-time setpoint override through the control-action path."""
        setpoints = action.parameters.get("setpoints", {})
        system = action.target_system
        if system == "storage":
            await self._submit_control_action_locked(
                system="storage",
                action="manual_override",
                target="power_kw",
                value=float(setpoints.get("power_kw", 0)),
                unit="kW",
                reason=action.reasoning,
                actor=actor,
            )
        elif system == "hvac":
            if "supply_temp_c" in setpoints:
                await self._submit_control_action_locked(
                    system="hvac",
                    action="manual_override",
                    target="supply_temp_c",
                    value=float(setpoints["supply_temp_c"]),
                    unit="C",
                    reason=action.reasoning,
                    actor=actor,
                )
            if "power_kw" in setpoints:
                await self._submit_control_action_locked(
                    system="hvac",
                    action="manual_override",
                    target="power_kw",
                    value=float(setpoints["power_kw"]),
                    unit="kW",
                    reason=action.reasoning,
                    actor=actor,
                )

    def _apply_demand_cap_locked(self, action: FacilityAction) -> None:
        """Apply demand cap and objective mode immediately."""
        params = action.parameters
        cap = params.get("demand_cap_kw")
        self._demand_cap_kw = float(cap) if cap and float(cap) > 0 else None
        if "objective_mode" in params:
            self._objective_mode = params["objective_mode"]

    async def preview_realtime_override(
        self,
        *,
        target_system: str,
        setpoints: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate setpoints against physical constraints and project SOC."""
        validation: list[dict[str, Any]] = []
        if target_system == "storage":
            power = float(setpoints.get("power_kw", 0))
            max_power = max(float(STORAGE_DEFAULTS["max_charge_power_kw"]),
                           float(STORAGE_DEFAULTS["max_discharge_power_kw"]))
            if abs(power) > max_power:
                validation.append({"field": "power_kw", "value": power, "limit": max_power, "status": "exceeded"})
            else:
                validation.append({"field": "power_kw", "value": power, "limit": max_power, "status": "ok"})
            soc = float(self._current_values.get("storage_soc", 0.5))
            dt_h = SIM_STEP_MINUTES / 60.0
            capacity = float(STORAGE_DEFAULTS["capacity_kwh"])
            eff = (float(STORAGE_DEFAULTS["discharge_efficiency_ratio"]) if power > 0
                   else float(STORAGE_DEFAULTS["charge_efficiency_ratio"]))
            soc_projection = []
            current_soc = soc
            for i in range(4):
                energy_delta = power * dt_h * eff / capacity
                current_soc = max(0.1, min(0.9, current_soc - energy_delta))
                soc_projection.append({"step": i + 1, "soc": round(current_soc, 4)})
            return {"validation": validation, "soc_projection": soc_projection}
        elif target_system == "hvac":
            if "supply_temp_c" in setpoints:
                supply = float(setpoints["supply_temp_c"])
                if not 5.0 <= supply <= 12.0:
                    validation.append({"field": "supply_temp_c", "value": supply, "limit": "5-12", "status": "exceeded"})
                else:
                    validation.append({"field": "supply_temp_c", "value": supply, "limit": "5-12", "status": "ok"})
            if "power_kw" in setpoints:
                power = float(setpoints["power_kw"])
                max_power = float(HVAC_DEFAULTS["total_rated_cooling_kw"]) / float(HVAC_DEFAULTS["cop_min"])
                if power < 0 or power > max_power:
                    validation.append({"field": "power_kw", "value": power, "limit": max_power, "status": "exceeded"})
                else:
                    validation.append({"field": "power_kw", "value": power, "limit": max_power, "status": "ok"})
            return {"validation": validation}
        return {"validation": validation, "error": f"unknown system: {target_system}"}

    def _check_facility_action_monitoring(self, step_measurements: dict[str, Any]) -> None:
        """Post-execution monitoring: compare actual metrics with predicted impact."""
        for action in self._facility_actions:
            if action.status != "applied" or action.monitor_steps_remaining <= 0:
                continue
            action.monitor_steps_remaining -= 1
            preview_after = action.impact_preview.get("after", {})
            actual_cost = round(self._daily_metrics["cost"], 1)
            predicted_cost = preview_after.get("daily_cost_cny")
            deviation: dict[str, Any] = {}
            if predicted_cost and isinstance(predicted_cost, (int, float)) and predicted_cost > 0:
                ratio = abs(actual_cost - predicted_cost) / predicted_cost
                deviation["cost_deviation_ratio"] = round(ratio, 3)
                if ratio > 0.10:
                    self._add_alert(
                        Severity.WARNING,
                        "facility_monitor",
                        f"facility action {action.proposal_id} cost deviation {ratio:.0%}",
                    )
            action.post_execution = {
                **(action.post_execution or {}),
                "step": self._time.current_step,
                "actual": {
                    "daily_cost_cny": actual_cost,
                    "daily_peak_kw": round(self._daily_metrics["peak"], 1),
                    "daily_carbon_kg": round(self._daily_metrics["carbon"], 1),
                    "storage_soc": step_measurements.get("storage_soc"),
                },
                "deviation": deviation,
                "monitor_steps_remaining": action.monitor_steps_remaining,
            }

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
               "tariff_periods": dd.get("tariff_periods", []),
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
            "current_phase": self.get_current_phase(),
            "facility_actions": self.get_facility_actions(),
            "active_strategy": {
                "demand_cap_kw": self._demand_cap_kw,
                "enabled": self._demand_cap_kw is not None,
                "monthly_peak_kw": self._monthly_peak_kw,
                "objective_mode": self._objective_mode,
            },
            "storage_soc_config": {
                "default_initial_soc": STORAGE_DEFAULT_INITIAL_SOC,
                "default_terminal_soc": STORAGE_DEFAULT_TERMINAL_SOC,
                "daily_override": self._daily_soc_override,
            },
            "pending_day_plan": self.get_pending_day_plan(),
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
            "simulation_loop": {
                "enabled": self._simulation_loop,
                "data_start": self._data_start.isoformat() if self._data_start else None,
                "data_end": self._data_end.isoformat() if self._data_end else None,
                "days_per_cycle": self._simulation_day_count,
                "cycle": getattr(self._time, "cycle_count", 0),
            },
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
