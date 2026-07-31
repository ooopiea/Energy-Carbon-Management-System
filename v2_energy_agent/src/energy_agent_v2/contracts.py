from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# 基础枚举与通用类型
# ---------------------------------------------------------------------------


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class DispatchObjective(StrEnum):
    MIN_COST = "min_cost"
    LIMIT_PEAK_DEMAND = "limit_peak_demand"
    MIN_CARBON = "min_carbon"
    WEIGHTED = "weighted"  # min(alpha*电费 + beta*碳排)，电费含电度+需量


class CarbonMethod(StrEnum):
    """电碳核算方法枚举，禁止相互覆盖。"""

    DIRECT = "direct"            # 直接排放因子 C(τ)：基于发电结构的物理平均强度
    RESPONSIBILITY = "responsibility"  # 江亿动态责任因子 Cr(τ)：源荷责任再分配


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False
    field: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class MetricValue(BaseModel):
    name: str
    value: float
    unit: str


class EvidenceRef(BaseModel):
    evidence_type: Literal["data", "algorithm", "constraint", "approval", "report"]
    ref_id: str
    version: str
    description: str
    uri: str | None = None


# ---------------------------------------------------------------------------
# 储能模型 V2（含爬坡、温度安全约束）
# ---------------------------------------------------------------------------


class BatteryConfigV2(BaseModel):
    """储能设备参数，V2 增加爬坡率与热安全约束。"""

    battery_id: str = "battery-huanghua-01"
    capacity_kwh: float = Field(gt=0)
    max_charge_power_kw: float = Field(gt=0)
    max_discharge_power_kw: float = Field(gt=0)
    min_soc_ratio: float = Field(ge=0, le=1)
    max_soc_ratio: float = Field(ge=0, le=1)
    charge_efficiency_ratio: float = Field(gt=0, le=1)
    discharge_efficiency_ratio: float = Field(gt=0, le=1)
    # V2 新增：爬坡约束（相邻时步最大功率变化）
    max_ramp_kw_per_step: float = Field(gt=0, description="相邻时步最大功率变化绝对值")
    # V2 新增：热安全约束
    max_cell_temperature_c: float = Field(gt=0, description="电芯温度安全上限")
    thermal_resistance_c_per_kw: float = Field(ge=0, description="单位功率稳态温升系数")
    thermal_time_constant_min: float = Field(gt=0, description="热时间常数")
    ambient_temperature_c: float = Field(description="环境温度")

    @model_validator(mode="after")
    def validate_bounds(self) -> BatteryConfigV2:
        if self.min_soc_ratio >= self.max_soc_ratio:
            raise ValueError("min_soc_ratio 必须小于 max_soc_ratio")
        if self.max_ramp_kw_per_step < max(self.max_charge_power_kw, self.max_discharge_power_kw):
            pass  # 爬坡约束通常小于额定功率，合法
        return self


class TimeInterval(BaseModel):
    start_index: int = Field(ge=0)
    end_index: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> TimeInterval:
        if self.end_index <= self.start_index:
            raise ValueError("end_index 必须大于 start_index")
        return self


class DispatchRevision(BaseModel):
    terminal_soc_min_ratio: float | None = Field(default=None, ge=0, le=1)
    reserve_soc_min_ratio: float | None = Field(default=None, ge=0, le=1)
    max_discharge_power_kw: float | None = Field(default=None, gt=0)
    max_charge_power_kw: float | None = Field(default=None, gt=0)
    blocked_intervals: list[TimeInterval] = Field(default_factory=list)
    objective: DispatchObjective | None = None

    # V2.2: 充放电循环次数约束。"一充一放"=1, "两充两放"=2
    max_cycles_per_day: int | None = Field(default=None, ge=1, le=8)

    # V2.3: 温度安全上限约束（工程师可收紧，不可放宽）
    max_cell_temperature_c: float | None = Field(default=None, gt=0)


# ---------------------------------------------------------------------------
# 电碳计量：发电结构与排放因子库
# ---------------------------------------------------------------------------


class GenerationSource(StrEnum):
    COAL = "coal"
    GAS = "gas"
    OIL = "oil"
    HYDRO = "hydro"
    WIND = "wind"
    SOLAR = "solar"
    NUCLEAR = "nuclear"
    BIOMASS = "biomass"
    OTHER_RENEWABLE = "other_renewable"
    PURCHASE = "purchase"  # 外购电


class EmissionFactorLibrary(BaseModel):
    """各能源类型的直接排放因子库 (kgCO2/kWh)，可按区域校准。"""

    factors_kg_per_kwh: dict[str, float] = Field(
        default_factory=lambda: {
            GenerationSource.COAL.value: 0.85,
            GenerationSource.GAS.value: 0.40,
            GenerationSource.OIL.value: 0.75,
            GenerationSource.HYDRO.value: 0.0,
            GenerationSource.WIND.value: 0.0,
            GenerationSource.SOLAR.value: 0.0,
            GenerationSource.NUCLEAR.value: 0.0,
            GenerationSource.BIOMASS.value: 0.0,
            GenerationSource.OTHER_RENEWABLE.value: 0.0,
            # 外购电按全国电网平均排放因子计（生态环境部 2022 年度公告，0.5366 tCO2/MWh）
            GenerationSource.PURCHASE.value: 0.5366,
        }
    )
    source_label: str = "ef-hunan-2022-v1"
    calibrated_region: str | None = None


class GenerationMixPoint(BaseModel):
    """单一时步的发电结构。"""

    timestamp: datetime
    generation_by_source_kw: dict[str, float] = Field(
        default_factory=dict, description="各能源类型发电功率 kW"
    )

    @property
    def total_generation_kw(self) -> float:
        return sum(self.generation_by_source_kw.values())


class CarbonAccountingResult(BaseModel):
    """电碳计量结果：同时输出 C(τ) 直接因子与 Cr(τ) 动态责任因子。"""

    method: CarbonMethod
    timestamps: list[datetime]
    # 直接碳排放因子 C(τ)：基于发电结构加权平均
    direct_factor_c_kg_per_kwh: list[float]
    # 江亿动态碳排放责任因子 Cr(τ)：源荷责任再分配后的激励信号
    responsibility_factor_cr_kg_per_kwh: list[float]
    # 责任调节系数（Cr/C 的比值，反映供需匹配激励强度）
    responsibility_adjustment_ratio: list[float]
    emission_factor_library_version: str
    region: str
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 电费模型：分时电价 + 需量 + 基本电费
# ---------------------------------------------------------------------------


class TariffPeriod(StrEnum):
    SHARP = "sharp"   # 尖
    PEAK = "peak"     # 峰
    FLAT = "flat"     # 平
    VALLEY = "valley"  # 谷


class TariffSchedule(BaseModel):
    """分时电价 + 需量电价 + 政府基金 + 力调 + 绿电环境价值规则。

    电费口径（对齐黄花厂务实际账单结构）：
    - 电度电费：Σ grid(t) × price(t) × Δt，所有电量含绿电统一按分时计费
    - 需量电费：max(实际最大需量, 申报需量) × 需量单价（元/kW/月）
    - 政府基金：总电量 × gov_fund_rate（固定 0.04625 元/kWh）
    - 力调电费：(电度+需量) × reactive_adjust_ratio（默认 -0.75% 奖励）
    - 绿电环境价值：绿电电量 × green_power_price（额外附加，绿电电度已含在分时电度中）
    """

    # 各时段电价 (元/kWh)，按 15 分钟粒度给出整日序列；若为 None 则由 period_map 推导
    price_cny_per_kwh_by_step: list[float] | None = None
    # 时段映射：index -> TariffPeriod（96 点），price_cny_per_kwh_by_step 为空时用此推导
    period_map: list[TariffPeriod] | None = None
    # 各时段单价
    price_sharp_cny_per_kwh: float = Field(default=0.0, ge=0)
    price_peak_cny_per_kwh: float = Field(default=0.0, ge=0)
    price_flat_cny_per_kwh: float = Field(default=0.0, ge=0)
    price_valley_cny_per_kwh: float = Field(default=0.0, ge=0)
    # 需量电价 (元/kW/月)
    demand_price_cny_per_kw_month: float = Field(default=0.0, ge=0)
    # 基本电费容许需量申报值 (kW)，None 表示按实际最大需量计
    declared_demand_kw: float | None = None
    # 绿电环境价值单价（元/kWh），黄花实际 0.00001；None 表示无绿电
    green_power_price_cny_per_kwh: float | None = None
    green_power_ratio: float = Field(default=0.0, ge=0, le=1)
    # V2.1 新增：政府基金及附加（元/kWh，固定 0.04625）
    gov_fund_rate_cny_per_kwh: float = Field(default=0.04625, ge=0)
    # V2.1 新增：力调电费系数（默认 -0.75%，负值即奖励）
    reactive_adjust_ratio: float = -0.0075
    # 月份标签（用于区分月度电价表），None 表示不区分
    tariff_month: int | None = None

    def resolve_price_series(self, point_count: int) -> list[float]:
        """根据 period_map 或直接序列生成完整价格序列。"""
        if self.price_cny_per_kwh_by_step is not None:
            if len(self.price_cny_per_kwh_by_step) != point_count:
                raise ValueError("price_cny_per_kwh_by_step 长度与时间轴不一致")
            return list(self.price_cny_per_kwh_by_step)
        if self.period_map is None or len(self.period_map) != point_count:
            raise ValueError("period_map 长度与时间轴不一致")
        price_map = {
            TariffPeriod.SHARP: self.price_sharp_cny_per_kwh,
            TariffPeriod.PEAK: self.price_peak_cny_per_kwh,
            TariffPeriod.FLAT: self.price_flat_cny_per_kwh,
            TariffPeriod.VALLEY: self.price_valley_cny_per_kwh,
        }
        return [price_map[p] for p in self.period_map]


class TariffResult(BaseModel):
    """电费计算结果。"""

    timestamps: list[datetime]
    price_cny_per_kwh: list[float]
    period_labels: list[str]
    energy_cost_cny: float
    demand_cost_cny: float
    basic_fee_cny: float
    # V2.1 新增：政府基金及附加（月度口径）
    gov_fund_cost_cny: float = 0.0
    # V2.1 新增：力调电费（月度口径，负值即奖励）
    reactive_adjustment_cny: float = 0.0
    # 绿电环境价值费用（绿电电量 × 环境价值单价）
    green_power_cost_cny: float
    total_cost_cny: float
    peak_demand_kw: float
    effective_price_cny_per_kwh: float


# ---------------------------------------------------------------------------
# 统一输入 bundle
# ---------------------------------------------------------------------------


class DispatchInputBundleV2(BaseModel):
    schema_version: str = "2.0"
    data_version: str
    site_id: str
    target_date: date
    time_step_minutes: int = Field(default=15, gt=0)
    timestamps: list[datetime]
    load_forecast_kw: list[float]
    electricity_price_cny_per_kwh: list[float]
    initial_soc_ratio: float = Field(ge=0, le=1)
    battery: BatteryConfigV2
    # 电碳输入
    generation_mix: list[GenerationMixPoint] = Field(default_factory=list)
    emission_factors: EmissionFactorLibrary = Field(default_factory=EmissionFactorLibrary)
    region: str = "cn-hunan"
    # 电费规则（可选，用于精细电费核算）
    tariff: TariffSchedule | None = None
    # 数据溯源：实际使用的数据日期与覆盖范围（透明化 nearest-day 替代）
    data_source_info: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_series(self) -> DispatchInputBundleV2:
        point_count = len(self.timestamps)
        if point_count == 0:
            raise ValueError("时间序列不能为空")
        for name in ("load_forecast_kw", "electricity_price_cny_per_kwh"):
            if len(getattr(self, name)) != point_count:
                raise ValueError(f"{name} 长度({len(getattr(self, name))})与时间轴({point_count})不一致")
        if point_count * self.time_step_minutes != 24 * 60:
            raise ValueError("输入必须覆盖完整一天")
        if len(self.generation_mix) not in (0, point_count):
            raise ValueError("generation_mix 长度须为 0 或等于时间轴长度")
        return self


# ---------------------------------------------------------------------------
# 约束校验
# ---------------------------------------------------------------------------


class ConstraintViolation(BaseModel):
    code: str
    severity: Literal["warning", "error", "critical"]
    message: str
    point_index: int | None = None
    actual_value: float | None = None
    limit_value: float | None = None
    unit: str | None = None


class ConstraintCheckResult(BaseModel):
    passed: bool
    violations: list[ConstraintViolation] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 储能优化结果 V2（含双碳账、温度序列）
# ---------------------------------------------------------------------------


class StorageOptimizationResult(BaseModel):
    """MILP 储能优化器输出。"""

    plan_id: str
    plan_version: int
    site_id: str
    target_date: date
    start_at: datetime
    time_step_minutes: int
    point_count: int
    timestamps: list[datetime]
    load_forecast_kw: list[float]
    battery_power_kw: list[float]
    soc_ratio: list[float]
    cell_temperature_c: list[float]
    baseline_grid_import_power_kw: list[float]
    grid_import_power_kw: list[float]
    electricity_price_cny_per_kwh: list[float]
    baseline_energy_cost_cny: float
    optimized_energy_cost_cny: float
    energy_cost_saving_cny: float
    baseline_peak_demand_kw: float
    optimized_peak_demand_kw: float
    peak_reduction_kw: float
    terminal_soc_ratio: float
    max_cell_temperature_c: float
    constraint_check: ConstraintCheckResult
    solver_status: str
    solve_duration_ms: int
    algorithm_version: str
    agent_version: str
    data_version: str
    objective: DispatchObjective


class CarbonDispatchResult(BaseModel):
    """储能方案对应的碳排放结果（直接 + 责任两套）。"""

    carbon_accounting: CarbonAccountingResult
    baseline_direct_carbon_kg: float
    optimized_direct_carbon_kg: float
    direct_carbon_reduction_kg: float
    baseline_responsibility_carbon_kg: float
    optimized_responsibility_carbon_kg: float
    responsibility_carbon_reduction_kg: float


# ---------------------------------------------------------------------------
# 审批与编排
# ---------------------------------------------------------------------------


class StorageDispatchRequestV2(BaseModel):
    dispatch_run_id: str
    plan_id: str
    plan_version: int = Field(ge=1)
    objective: DispatchObjective
    inputs: DispatchInputBundleV2
    revision: DispatchRevision | None = None
    # 加权目标权重（仅 objective=WEIGHTED 时生效）
    # alpha: 电费权重（含电度+需量），beta: 碳排放权重
   # 默认 alpha=1, beta=0.01 表示 1 元电费 ≈ 0.01 kg 碳排
    weight_alpha_cost: float = Field(default=1.0, ge=0)
    weight_beta_carbon: float = Field(default=0.01, ge=0)
    carbon_factors_override: list[float] | None = None
    # 预计算的碳因子（来自 compute_carbon_factors 节点），供优化器直接使用
    # 传入 Cr(τ) 责任因子；为 None 时优化器回退到内部 _resolve_carbon_factors()


class AgentContext(BaseModel):
    dispatch_run_id: str
    thread_id: str
    site_id: str
    requested_by: str
    correlation_id: str


class AgentMetadata(BaseModel):
    agent_id: str
    agent_type: str
    name: str
    version: str
    contract_version: str
    capabilities: list[str]


class AgentResult(BaseModel):
    dispatch_run_id: str
    agent_id: str
    agent_version: str
    status: Literal["succeeded", "failed", "needs_input"]
    result: dict[str, Any] | None = None
    metrics: list[MetricValue] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    violations: list[ConstraintViolation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: ErrorDetail | None = None
    started_at: datetime
    completed_at: datetime


class ApprovalDecision(BaseModel):
    dispatch_run_id: str
    decision: Literal["approve", "reject", "revise"]
    comment: str = ""
    revision: DispatchRevision | None = None
    decided_by: str = Field(min_length=1)
    decided_at: datetime | None = None
    expected_plan_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def validate_revision(self) -> ApprovalDecision:
        if self.decision == "revise" and self.revision is None and not self.comment:
            raise ValueError("revise 决定必须提供 revision 或 comment（自然语言）")
        if self.decision != "revise" and self.revision is not None:
            raise ValueError("仅 revise 决定允许提供 revision")
        return self


class DomainEvent(BaseModel):
    event_id: str
    event_type: str
    occurred_at: datetime
    dispatch_run_id: str
    node_id: str | None = None
    status: str
    summary: str
    duration_ms: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class FactoryDispatchSummary(BaseModel):
    recommended_plan_id: str
    recommended_plan_version: int
    executive_summary: str
    expected_benefits: list[str]
    risks: list[str]
    engineer_checks: list[str]
    evidence: list[EvidenceRef]
    ready_for_approval: bool
    llm_mode: Literal["glm", "fallback"]
    llm_model: str


class RunRecord(BaseModel):
    dispatch_run_id: str
    thread_id: str
    site_id: str
    target_date: date
    objective: DispatchObjective
    status: RunStatus
    current_plan_id: str
    current_plan_version: int
    requested_by: str
    created_at: datetime
    updated_at: datetime
    error: ErrorDetail | None = None


# ---------------------------------------------------------------------------
# V2.2 新增：四个 LLM agent 的数据契约
# ---------------------------------------------------------------------------


class RawDataFile(BaseModel):
    """工程师上传的原始数据文件描述。"""
    file_name: str
    file_path: str
    file_format: Literal["csv", "xlsx", "json", "pdf", "txt"]
    uploaded_by: str
    uploaded_at: datetime
    raw_content_preview: str = Field(default="", description="前 N 行预览文本")
    file_size_bytes: int = Field(ge=0)


class FieldMapping(BaseModel):
    raw_column: str
    standard_field: str
    confidence: float = Field(ge=0, le=1)


class DataQualityIssue(BaseModel):
    issue_type: Literal["missing_data", "outlier", "format_error", "time_gap", "unit_mismatch"]
    description: str
    severity: Literal["warning", "error"]


class DataIngestResult(BaseModel):
    """数据清洗 agent 的输出。"""
    source_file: RawDataFile
    file_type: Literal["schedule", "load_forecast", "tariff", "battery_params", "generation_mix", "unknown"]
    site_id: str
    target_date: date | None = None
    time_granularity_minutes: int = 15
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    quality_issues: list[DataQualityIssue] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0, le=1)
    llm_model: str = "unknown"
    parsed_data: dict[str, Any] = Field(default_factory=dict, description="清洗后的结构化数据")


class DataProvenance(BaseModel):
    """数据血缘记录。"""
    source_file: RawDataFile
    data_version: str
    provenance_description: str
    transformations_applied: list[dict[str, Any]] = Field(default_factory=list)
    compliance_tags: list[str] = Field(default_factory=list)
    retention_recommendation: str = ""
    archived_path: str = ""
    archived_at: datetime | None = None


class DataArchiveResult(BaseModel):
    """数据封存 agent 的输出。"""
    provenance: DataProvenance
    archive_path: str
    archive_version: str
    checksum: str = ""
    success: bool = True


class AnomalySignal(BaseModel):
    """异常信号（来自 SCADA / 天气 / 电网等）。"""
    signal_id: str
    source: Literal["scada", "weather", "grid", "battery_bms", "manual"]
    severity: Literal["info", "warning", "critical"]
    timestamp: datetime
    description: str
    raw_data: dict[str, Any] = Field(default_factory=dict)


class AnomalyAlert(BaseModel):
    """异常工况 agent 的研判输出。"""
    alert_id: str
    alert_level: Literal["info", "warning", "critical"]
    alert_summary: str
    root_cause_analysis: str = ""
    affected_assets: list[str] = Field(default_factory=list)
    signals: list[AnomalySignal] = Field(default_factory=list)
    recommended_actions: list[dict[str, Any]] = Field(default_factory=list)
    should_reoptimize: bool = False
    confidence: float = Field(ge=0, le=1)
    llm_model: str = "unknown"
    created_at: datetime


class DistillationInsight(BaseModel):
    """复盘蒸馏 agent 的输出。"""
    summary: str
    patterns: list[dict[str, Any]] = Field(default_factory=list)
    prompt_improvement_suggestions: list[dict[str, Any]] = Field(default_factory=list)
    new_few_shot_candidates: list[dict[str, str]] = Field(default_factory=list)
    review_period: str = ""
    total_records_analyzed: int = 0
    llm_model: str = "unknown"
    generated_at: datetime
