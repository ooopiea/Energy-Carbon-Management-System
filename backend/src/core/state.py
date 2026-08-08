"""LangGraph 全局状态与 Agent 状态模型。

v3 的状态分为两部分：
1. LangGraph 编排状态 (EnergySystemState)：日前调度的工作流状态
2. 实时运行状态 (RuntimeState)：每个 tick 的实时数据，供前端展示
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class AgentType(StrEnum):
    DATA = "data"
    PREDICTION = "prediction"
    STORAGE = "storage"
    HVAC = "hvac"
    MONITOR = "monitor"


class NodeStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    WARNING = "warning"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Agent 节点运行记录
# ---------------------------------------------------------------------------

class AgentNodeStatus(BaseModel):
    """单个 Agent 节点的实时状态。"""
    agent_type: AgentType
    node_id: str
    name: str
    status: NodeStatus = NodeStatus.IDLE
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    last_result_summary: str = ""
    message: str = ""


class AgentReport(BaseModel):
    """Agent 生成的报告。"""
    report_id: str
    agent_type: AgentType
    title: str
    content: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    status: Literal["pending", "approved", "rejected"] = "pending"
    severity: Severity = Severity.INFO
    content_hash: str = ""
    run_id: str = ""


class ApprovalGate(BaseModel):
    """工程师审批门。"""
    gate_id: str
    name: str
    description: str
    status: NodeStatus = NodeStatus.IDLE
    report: AgentReport | None = None
    report_id: str | None = None
    report_hash: str | None = None
    decision: Literal["approve", "reject", "revise"] | None = None
    comment: str = ""
    decided_at: datetime | None = None
    decided_by: str | None = None


class AlertItem(BaseModel):
    """告警项。"""
    alert_id: str
    severity: Severity
    source: str
    message: str
    timestamp: datetime
    acknowledged: bool = False


class DispatchCommand(BaseModel):
    """发送给物理执行端（当前为模拟执行器）的单步命令。"""

    command_id: str
    run_id: str
    step: int
    sim_time: datetime
    storage_power_kw: float
    hvac_power_kw: float
    hvac_supply_temp_c: float
    storage_report_id: str
    storage_report_hash: str
    hvac_report_id: str
    hvac_report_hash: str
    created_at: datetime
    manual_override: dict[str, Any] | None = None


class ExecutionAck(BaseModel):
    """执行端对命令的明确回执。"""

    command_id: str
    accepted: bool
    status: Literal["executed", "rejected", "failed"]
    message: str = ""
    acknowledged_at: datetime


class DispatchFeedback(BaseModel):
    """设备侧测量反馈，用于计划—执行偏差闭环。"""

    command_id: str
    measured_storage_power_kw: float
    measured_hvac_power_kw: float
    measured_storage_soc: float
    measured_storage_temp_c: float
    measured_hvac_supply_temp_c: float
    measured_hvac_return_temp_c: float
    storage_deviation_kw: float
    hvac_deviation_kw: float
    max_deviation_ratio: float
    measured_at: datetime


class DispatchExecution(BaseModel):
    command: DispatchCommand
    ack: ExecutionAck
    feedback: DispatchFeedback


class ControlActionRecord(BaseModel):
    """Auditable operator action, optionally consumed by the next physical step."""

    action_id: str
    run_id: str
    system: Literal["overview", "storage", "hvac"]
    action: str
    target: str
    value: float
    unit: str
    reason: str
    actor: str
    submitted_at: datetime
    status: Literal["accepted", "executed", "rejected"] = "accepted"
    applied_step: int | None = None
    command_id: str | None = None


class DisturbanceEvent(BaseModel):
    """Natural-language operating disturbance awaiting an explicit human decision."""

    event_id: str
    run_id: str
    actor: str
    actor_role: Literal["engineer", "facility"]
    source_text: str
    event_type: Literal[
        "equipment_failure",
        "equipment_recovery",
        "load_adjustment",
        "weather_override",
        "price_override",
        "schedule_change",
        "operational_note",
    ]
    target: str
    start_time: datetime
    end_time: datetime | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    summary: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    parsed_by: str = "rule_fallback"
    status: Literal["proposed", "applied", "cancelled", "failed"] = "proposed"
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    impact_summary: str = ""


class FacilityAction(BaseModel):
    """Structured facility-side action proposal with preview and post-monitoring."""

    proposal_id: str
    action_type: Literal[
        "day_ahead_modification",
        "realtime_override",
        "demand_cap",
        "mission",
    ]
    target_system: Literal["overview", "storage", "hvac"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    impact_preview: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    status: Literal["proposed", "confirmed", "applied", "cancelled", "failed"] = "proposed"
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    post_execution: dict[str, Any] | None = None
    monitor_steps_remaining: int = 0


class TimeSeriesPoint(BaseModel):
    """单个时间序列数据点。"""
    timestamp: datetime
    step: int
    value: float


# ---------------------------------------------------------------------------
# LangGraph 编排状态 (日前调度工作流)
# ---------------------------------------------------------------------------

class EnergySystemState(TypedDict, total=False):
    """LangGraph StateGraph 的状态定义。"""
    run_id: str
    sim_time: str
    day: int
    step: int
    # 各阶段数据
    data_agent_result: dict[str, Any]
    prediction_result: dict[str, Any]
    storage_result: dict[str, Any]
    hvac_result: dict[str, Any]
    monitor_result: dict[str, Any]
    # 审批门
    forecast_approval: dict[str, Any]
    storage_approval: dict[str, Any]
    hvac_approval: dict[str, Any]
    # 物理执行
    physical_dispatch: dict[str, Any]
    # 事件链
    events: Annotated[list[dict[str, Any]], _add_events]
    error: dict[str, Any] | None
    current_node: str
    workflow_status: str


def _add_events(left: list, right: list | dict) -> list:
    """events 使用 add reducer 保留完整事件链。"""
    if isinstance(right, dict):
        return left + [right]
    return left + right


# ---------------------------------------------------------------------------
# 实时运行状态 (供前端展示)
# ---------------------------------------------------------------------------

class RuntimeState(BaseModel):
    """全局实时运行状态，每个 tick 更新。"""
    sim_time: datetime
    day: int
    step: int
    progress: float
    paused: bool

    # Agent 节点状态
    agent_nodes: dict[str, AgentNodeStatus] = Field(default_factory=dict)

    # 审批门
    approval_gates: dict[str, ApprovalGate] = Field(default_factory=dict)

    # 实时数据
    current_load_kw: float = 0.0
    current_solar_kw: float = 0.0
    current_grid_kw: float = 0.0
    current_storage_power_kw: float = 0.0
    current_storage_soc: float = 0.0
    current_storage_temp_c: float = 25.0
    current_hvac_power_kw: float = 0.0
    current_hvac_chilled_water_temp_c: float = 7.0
    current_carbon_factor: float = 0.0
    current_price: float = 0.0
    current_tariff_period: str = "flat"

    # 告警
    alerts: list[AlertItem] = Field(default_factory=list)

    # 报告
    reports: list[AgentReport] = Field(default_factory=list)

    # 日内累计
    daily_energy_kwh: float = 0.0
    daily_cost_cny: float = 0.0
    daily_carbon_kg: float = 0.0
    daily_peak_kw: float = 0.0

    # 时间序列缓存（当日）
    load_series: list[dict[str, Any]] = Field(default_factory=list)
    solar_series: list[dict[str, Any]] = Field(default_factory=list)
    grid_series: list[dict[str, Any]] = Field(default_factory=list)
    storage_power_series: list[dict[str, Any]] = Field(default_factory=list)
    soc_series: list[dict[str, Any]] = Field(default_factory=list)
    temp_series: list[dict[str, Any]] = Field(default_factory=list)
    hvac_power_series: list[dict[str, Any]] = Field(default_factory=list)
    carbon_series: list[dict[str, Any]] = Field(default_factory=list)
    price_series: list[dict[str, Any]] = Field(default_factory=list)

    # 日前计划曲线
    day_ahead_load_forecast: list[float] = Field(default_factory=list)
    day_ahead_storage_plan: list[float] = Field(default_factory=list)
    day_ahead_hvac_plan: list[float] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 物理约束配置
# ---------------------------------------------------------------------------

class PhysicsConstraints(BaseModel):
    """每个 Agent 的物理知识约束（防幻觉/越限）。"""
    # 储能
    soc_min: float = 0.10
    soc_max: float = 0.90
    temp_max_c: float = 45.0
    ramp_max_kw: float = 300.0
    # HVAC
    chilled_water_min_c: float = 5.0
    chilled_water_max_c: float = 12.0
    cop_min: float = 3.0
    cop_max: float = 5.5
    # 负荷
    load_min_kw: float = 5000.0
    load_max_kw: float = 120000.0
