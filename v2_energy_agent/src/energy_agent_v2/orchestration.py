# V2 LangGraph orchestration: storage optimize + tariff + carbon dual-factor + approval loop.
# Path: ingest_raw_data -> initialize_run -> load_inputs -> compute_tariff -> compute_carbon_factors
# -> optimize_storage -> compute_carbon_dispatch -> factory_summary -> storage_approval
# -> [approve]freeze_plan -> archive_data | [reject]close | [revise]apply_revision -> optimize_storage
# Any node failure -> fail_run. events uses add reducer so the full event chain is preserved.
from __future__ import annotations

import time
from datetime import UTC, date as _date, datetime
import json as _json
from operator import add
from typing import Annotated, Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from typing_extensions import TypedDict

from energy_agent_v2.contracts import (
    ApprovalDecision,
    CarbonAccountingResult,
    CarbonDispatchResult,
    DispatchInputBundleV2,
    DispatchObjective,
    DispatchRevision,
    DomainEvent,
    ErrorDetail,
    EvidenceRef,
    FactoryDispatchSummary,
    RunRecord,
    RunStatus,
    StorageDispatchRequestV2,
    StorageOptimizationResult,
    TariffResult,
)
from energy_agent_v2.errors import AppError


from pathlib import Path as _Path
_DISTILLATION_DIR = _Path(__file__).resolve().parents[2] / "data" / "distillation"
_PLAN_ARCHIVE_DIR = _Path(__file__).resolve().parents[2] / "data" / "plan_archive"


def _save_plan_version(
    *,
    state: EnergyDispatchStateV2,
    storage_result: dict[str, Any],
    is_final: bool = False,
) -> None:
    """把一个方案版本落盘为独立 JSON，确保中间版本不被后续覆盖。

    存储路径: cases/plan_archive/{target_date}/{plan_id}_v{version}.json
    最终版本额外存一份 {plan_id}_final.json
    """
    try:
        plan_id = state["current_plan_id"]
        version = state["current_plan_version"]
        target = state.get("target_date", "unknown")
        day_dir = _PLAN_ARCHIVE_DIR / target
        day_dir.mkdir(parents=True, exist_ok=True)

        record = {
            "plan_id": plan_id,
            "plan_version": version,
            "dispatch_run_id": state["dispatch_run_id"],
            "target_date": target,
            "objective": state["objective"],
            "saved_at": datetime.now(UTC).isoformat(),
            "revision": state.get("revision_request"),
            "storage_result": storage_result,
            "tariff_result": state.get("tariff_result"),
            "carbon_result": state.get("carbon_result"),
        }

        fname = f"{plan_id}_v{version}.json"
        (day_dir / fname).write_text(
            _json.dumps(record, ensure_ascii=False, default=str), encoding="utf-8"
        )

        if is_final:
            record["is_final"] = True
            record["events"] = state.get("events", [])
            (day_dir / f"{plan_id}_final.json").write_text(
                _json.dumps(record, ensure_ascii=False, default=str), encoding="utf-8"
            )
    except Exception:
        pass  # 持久化失败不阻塞调度

class EnergyDispatchStateV2(TypedDict, total=False):
    dispatch_run_id: str
    thread_id: str
    site_id: str
    target_date: str
    objective: str
    requested_by: str
    run_status: str
    current_plan_id: str
    current_plan_version: int
    input_bundle: dict[str, Any]
    revision_request: dict[str, Any] | None
    raw_file: dict[str, Any] | None
    price_mode: str
    battery_override: dict[str, Any] | None
    ingest_result: dict[str, Any] | None
    archive_result: dict[str, Any] | None
    tariff_result: dict[str, Any] | None
    storage_result: dict[str, Any] | None
    carbon_result: dict[str, Any] | None
    carbon_factors: dict[str, Any] | None
    factory_summary: dict[str, Any] | None
    approval_decision: dict[str, Any] | None
    events: Annotated[list[dict[str, Any]], add]
    error: dict[str, Any] | None
    # parse_revision 子状态 (V2.1 spec section 2.3)
    parse_round: int
    parsed_slots: dict[str, Any] | None
    clarification_history: list[dict[str, Any]] | None
    revision_resolved: bool
    fallback_form: bool


class AppContextV2:
    def __init__(self, data_provider: Any, storage_optimizer: Any, carbon_accountant: Any, tariff_calculator: Any, revision_parser: Any = None, ingest_agent: Any = None, archive_agent: Any = None, approval_agent: Any = None, anomaly_agent: Any = None, distillation_agent: Any = None) -> None:
        self.data_provider = data_provider
        self.storage_optimizer = storage_optimizer
        self.carbon_accountant = carbon_accountant
        self.tariff_calculator = tariff_calculator
        self.revision_parser = revision_parser
        self.ingest_agent = ingest_agent
        self.archive_agent = archive_agent
        self.approval_agent = approval_agent
        self.anomaly_agent = anomaly_agent
        self.distillation_agent = distillation_agent


def now_utc() -> datetime:
    return datetime.now(UTC)


async def record_node(state: EnergyDispatchStateV2, node_id: str, summary: str, *, status: str = "succeeded", duration_ms: int | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    version = state.get("current_plan_version", 1)
    run_id = state["dispatch_run_id"]
    event = {"event_id": f"{run_id}:{node_id}:v{version}:{status}", "event_type": f"node.{status}", "occurred_at": now_utc().isoformat(), "dispatch_run_id": run_id, "node_id": node_id, "status": status, "summary": summary, "duration_ms": duration_ms, "payload": payload or {}}
    return {"events": [event]}


async def ingest_raw_data_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        raw_file_dict = state.get("raw_file")
        if raw_file_dict is None:
            rec = await record_node(state, "ingest_raw_data", "无原始文件，跳过数据清洗")
            return {"error": None, **rec}
        from energy_agent_v2.contracts import RawDataFile
        raw_file = RawDataFile.model_validate(raw_file_dict)
        agent = runtime.context.ingest_agent
        if agent is None:
            rec = await record_node(state, "ingest_raw_data", "未配置 ingest_agent，跳过")
            return {"error": None, **rec}
        result = agent.ingest(raw_file)
        duration = round((time.perf_counter() - started) * 1000)
        rec = await record_node(state, "ingest_raw_data", f"识别文件类型: {result.file_type}, 置信度 {result.overall_confidence:.2f}", duration_ms=duration)
        return {"ingest_result": result.model_dump(mode="json"), "error": None, **rec}
    except Exception as exc:
        error = ErrorDetail(code="DATA_INGEST_FAILED", message=str(exc), retryable=False)
        rec = await record_node(state, "ingest_raw_data", error.message, status="failed")
        return {"error": error.model_dump(mode="json"), **rec}


async def archive_data_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        ingest_dict = state.get("ingest_result")
        if ingest_dict is None:
            rec = await record_node(state, "archive_data", "无清洗结果，跳过封存")
            return {"error": None, **rec}
        from energy_agent_v2.contracts import DataIngestResult
        ingest_result = DataIngestResult.model_validate(ingest_dict)
        agent = runtime.context.archive_agent
        if agent is None:
            rec = await record_node(state, "archive_data", "未配置 archive_agent，跳过")
            return {"error": None, **rec}
        result = agent.archive(ingest_result)
        duration = round((time.perf_counter() - started) * 1000)
        rec = await record_node(state, "archive_data", f"数据已封存: {result.archive_version}", duration_ms=duration)
        return {"archive_result": result.model_dump(mode="json"), "error": None, **rec}
    except Exception as exc:
        error = ErrorDetail(code="DATA_ARCHIVE_FAILED", message=str(exc), retryable=False)
        rec = await record_node(state, "archive_data", error.message, status="failed")
        return {"error": error.model_dump(mode="json"), **rec}


async def initialize_run_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    rec = await record_node(state, "initialize_run", "任务与版本上下文已初始化")
    return {"run_status": RunStatus.RUNNING.value, "error": None, **rec}


async def load_inputs_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        bundle = runtime.context.data_provider.load_dispatch_inputs(
            state["site_id"],
            _date.fromisoformat(state["target_date"]),
            price_mode=state.get("price_mode", "auto"),
            battery_override=state.get("battery_override"),
        )
        duration = round((time.perf_counter() - started) * 1000)
        rec = await record_node(state, "load_inputs", f"已加载 {len(bundle.timestamps)} 点日前数据(版本{bundle.data_version})", duration_ms=duration, payload={"data_version": bundle.data_version, "point_count": len(bundle.timestamps)})
        return {"input_bundle": bundle.model_dump(mode="json"), "error": None, **rec}
    except Exception as exc:
        error = ErrorDetail(code="DATA_LOAD_FAILED", message=str(exc), retryable=False)
        rec = await record_node(state, "load_inputs", error.message, status="failed")
        return {"error": error.model_dump(mode="json"), **rec}


def route_ok_or_fail(state: EnergyDispatchStateV2) -> Literal["ok", "failed"]:
    return "failed" if state.get("error") else "ok"


async def compute_tariff_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        bundle = DispatchInputBundleV2.model_validate(state["input_bundle"])
        baseline_grid = [max(l, 0.0) for l in bundle.load_forecast_kw]
        tc = runtime.context.tariff_calculator
        if bundle.tariff is not None:
            result = tc.calculate(bundle.tariff, baseline_grid, bundle.timestamps, bundle.time_step_minutes)
        else:
            from energy_agent_v2.contracts import TariffSchedule
            fallback_tariff = TariffSchedule(price_cny_per_kwh_by_step=bundle.electricity_price_cny_per_kwh, demand_price_cny_per_kw_month=0.0)
            result = tc.calculate(fallback_tariff, baseline_grid, bundle.timestamps, bundle.time_step_minutes)
        duration = round((time.perf_counter() - started) * 1000)
        # V2.1 fix S1-residual: display daily energy cost separately from monthly demand charge
        rec = await record_node(state, "compute_tariff", f"日电度电费 {result.energy_cost_cny:.0f} 元 | 需量电费(月) {result.demand_cost_cny:.0f} 元", duration_ms=duration, payload={"daily_energy_cost": result.energy_cost_cny, "monthly_demand_cost": result.demand_cost_cny, "monthly_gov_fund": result.gov_fund_cost_cny, "monthly_reactive_adj": result.reactive_adjustment_cny, "total_cost_cny": result.total_cost_cny, "peak_demand_kw": result.peak_demand_kw})
        return {"tariff_result": result.model_dump(mode="json"), "error": None, **rec}
    except Exception as exc:
        error = ErrorDetail(code="TARIFF_FAILED", message=str(exc), retryable=False)
        rec = await record_node(state, "compute_tariff", error.message, status="failed")
        return {"error": error.model_dump(mode="json"), **rec}


async def optimize_storage_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        bundle = DispatchInputBundleV2.model_validate(state["input_bundle"])
        # 传递预计算碳因子 Cr(τ) 给优化器（来自 compute_carbon_factors 节点）
        carbon_override = None
        cf_dict = state.get("carbon_factors")
        if cf_dict:
            try:
                cf = CarbonAccountingResult.model_validate(cf_dict)
                if len(cf.responsibility_factor_cr_kg_per_kwh) == len(bundle.timestamps):
                    carbon_override = cf.responsibility_factor_cr_kg_per_kwh
            except Exception:
                pass
        request = StorageDispatchRequestV2(dispatch_run_id=state["dispatch_run_id"], plan_id=state["current_plan_id"], plan_version=state["current_plan_version"], objective=DispatchObjective(state["objective"]), inputs=bundle, revision=(DispatchRevision.model_validate(state["revision_request"]) if state.get("revision_request") else None), carbon_factors_override=carbon_override)
        result = runtime.context.storage_optimizer.optimize(request)
        duration = round((time.perf_counter() - started) * 1000)
        if not result.constraint_check.passed:
            error = ErrorDetail(code="STORAGE_CONSTRAINT_VIOLATION", message="候选方案未通过硬约束校验", details={"violations": [v.model_dump() for v in result.constraint_check.violations]})
            rec = await record_node(state, "optimize_storage", error.message, status="failed")
            return {"storage_result": result.model_dump(mode="json"), "error": error.model_dump(mode="json"), **rec}
        rec = await record_node(state, "optimize_storage", f"储能方案 V{state['current_plan_version']} 生成(solver={result.solver_status}, 省{result.energy_cost_saving_cny:.0f}元)", duration_ms=duration, payload={"algorithm": result.algorithm_version, "saving_cny": result.energy_cost_saving_cny, "peak_reduction_kw": result.peak_reduction_kw, "max_temp_c": result.max_cell_temperature_c})
        _save_plan_version(state=state, storage_result=result.model_dump(mode="json"))
        return {"storage_result": result.model_dump(mode="json"), "error": None, **rec}
    except Exception as exc:
        error = ErrorDetail(code="STORAGE_FAILED", message=str(exc), retryable=False)
        rec = await record_node(state, "optimize_storage", error.message, status="failed")
        return {"error": error.model_dump(mode="json"), **rec}


async def compute_carbon_factors_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    """在 optimize 之前计算 C(tau)/Cr(tau) 碳因子，供 MILP 优化器使用。"""
    started = time.perf_counter()
    try:
        bundle = DispatchInputBundleV2.model_validate(state["input_bundle"])
        acc = runtime.context.carbon_accountant
        if not bundle.generation_mix:
            rec = await record_node(state, "compute_carbon_factors", "无发电结构数据，碳因子计算跳过")
            return {"carbon_factors": None, "error": None, **rec}
        factors = acc.compute_factors(bundle.generation_mix, bundle.emission_factors, bundle.region)
        duration = round((time.perf_counter() - started) * 1000)
        n = len(factors.direct_factor_c_kg_per_kwh)
        rec = await record_node(state, "compute_carbon_factors", f"碳因子已计算 C均值 {sum(factors.direct_factor_c_kg_per_kwh) / n:.4f}, Cr均值 {sum(factors.responsibility_factor_cr_kg_per_kwh) / n:.4f}", duration_ms=duration, payload={"C_mean": round(sum(factors.direct_factor_c_kg_per_kwh) / n, 4), "Cr_mean": round(sum(factors.responsibility_factor_cr_kg_per_kwh) / n, 4)})
        return {"carbon_factors": factors.model_dump(mode="json"), "error": None, **rec}
    except Exception as exc:
        error = ErrorDetail(code="CARBON_FACTORS_FAILED", message=str(exc), retryable=False)
        rec = await record_node(state, "compute_carbon_factors", error.message, status="failed")
        return {"error": error.model_dump(mode="json"), **rec}


async def compute_carbon_dispatch_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    """在 optimize 之后用预计算碳因子核算方案碳排放（直接 C + 责任 Cr）。"""
    started = time.perf_counter()
    try:
        bundle = DispatchInputBundleV2.model_validate(state["input_bundle"])
        storage = StorageOptimizationResult.model_validate(state["storage_result"])
        step_hours = bundle.time_step_minutes / 60
        acc = runtime.context.carbon_accountant
        cf_dict = state.get("carbon_factors")
        if not cf_dict:
            rec = await record_node(state, "compute_carbon_dispatch", "无预计算碳因子，电碳计量跳过")
            return {"carbon_result": None, "error": None, **rec}
        factors = CarbonAccountingResult.model_validate(cf_dict)
        dispatch = acc.account_dispatch(factors, storage.baseline_grid_import_power_kw, storage.grid_import_power_kw, step_hours)
        duration = round((time.perf_counter() - started) * 1000)
        n = len(factors.direct_factor_c_kg_per_kwh)
        rec = await record_node(state, "compute_carbon_dispatch", f"直接碳减排 {dispatch.direct_carbon_reduction_kg:.1f} kg, 责任碳减排 {dispatch.responsibility_carbon_reduction_kg:.1f} kg", duration_ms=duration, payload={"C_mean": round(sum(factors.direct_factor_c_kg_per_kwh) / n, 4), "Cr_mean": round(sum(factors.responsibility_factor_cr_kg_per_kwh) / n, 4)})
        return {"carbon_result": dispatch.model_dump(mode="json"), "error": None, **rec}
    except Exception as exc:
        error = ErrorDetail(code="CARBON_DISPATCH_FAILED", message=str(exc), retryable=False)
        rec = await record_node(state, "compute_carbon_dispatch", error.message, status="failed")
        return {"error": error.model_dump(mode="json"), **rec}


async def factory_summary_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        storage = StorageOptimizationResult.model_validate(state["storage_result"])
        benefits = [f"日电度电费节省 {storage.energy_cost_saving_cny:.0f} 元", f"峰值削减 {storage.peak_reduction_kw:.0f} kW"]
        risks: list[str] = []
        if storage.max_cell_temperature_c > 40:
            risks.append(f"最高电芯温度 {storage.max_cell_temperature_c:.1f}C, 接近安全上限")
        checks = ["SOC 全程在安全范围内", "爬坡率约束已满足", "温度约束已满足", "末端 SOC 达标"]
        if state.get("carbon_result"):
            cd = CarbonDispatchResult.model_validate(state["carbon_result"])
            benefits.append(f"直接碳减排 {cd.direct_carbon_reduction_kg:.0f} kg")
            benefits.append(f"责任碳减排(江亿Cr) {cd.responsibility_carbon_reduction_kg:.0f} kg")
        summary = FactoryDispatchSummary(recommended_plan_id=storage.plan_id, recommended_plan_version=storage.plan_version, executive_summary=f"方案 V{storage.plan_version} 通过 MILP 优化生成, 预计日省 {storage.energy_cost_saving_cny:.0f} 元电费。", expected_benefits=benefits, risks=risks, engineer_checks=checks, evidence=[EvidenceRef(evidence_type="algorithm", ref_id=storage.algorithm_version, version=storage.algorithm_version, description="MILP 储能优化器")], ready_for_approval=True, llm_mode="fallback", llm_model="deterministic-summary")
        duration = round((time.perf_counter() - started) * 1000)
        rec = await record_node(state, "factory_summary", f"审批摘要已生成({summary.llm_mode})")
        return {"factory_summary": summary.model_dump(mode="json"), "run_status": RunStatus.PENDING_APPROVAL.value, "error": None, **rec}
    except Exception as exc:
        error = ErrorDetail(code="SUMMARY_FAILED", message=str(exc), retryable=False)
        rec = await record_node(state, "factory_summary", error.message, status="failed")
        return {"error": error.model_dump(mode="json"), **rec}


async def storage_approval_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    raw = interrupt({"dispatch_run_id": state["dispatch_run_id"], "plan_id": state["current_plan_id"], "plan_version": state["current_plan_version"], "summary": state.get("factory_summary"), "allowed_decisions": ["approve", "reject", "revise"]})
    decision = ApprovalDecision.model_validate(raw)
    rec = await record_node(state, "storage_approval", f"审批决策: {decision.decision}", payload={"decision": decision.decision, "decided_by": decision.decided_by, "comment": decision.comment})
    return {"approval_decision": decision.model_dump(mode="json"), **rec}


def route_after_approval(state: EnergyDispatchStateV2) -> Literal["approve", "reject", "revise"]:
    return state["approval_decision"]["decision"]


async def parse_revision_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    """LLM-powered revise intent parsing (V2.1 spec section 2.2).

    Inline interrupt design: up to 2 clarification rounds + 1 fallback form.
    Falls back to direct structured revision when no parser is configured.
    """
    from pathlib import Path
    from energy_agent_v2.llm.parse_revision import build_distillation_record
    from energy_agent_v2.llm.slot_validator import build_revision

    decision = ApprovalDecision.model_validate(state["approval_decision"])
    # 优先使用 approval_agent（包装了 RevisionParser + 物理含义解读），回退到 revision_parser
    approval_agent = runtime.context.approval_agent
    parser = approval_agent or runtime.context.revision_parser
    comment = decision.comment
    storage_result = state.get("storage_result")
    run_id = state["dispatch_run_id"]
    plan_ver = state.get("current_plan_version", 1)

    # Backward compat: no parser or structured revision already provided
    if parser is None or decision.revision is not None:
        revision = decision.revision or DispatchRevision()
        rec = await record_node(state, "parse_revision", "无 LLM 解析，直接使用结构化 revision")
        return {"revision_resolved": True, "revision_request": revision.model_dump(mode="json"), "parse_round": 0, **rec}

    llm_model = getattr(parser.client, "model", "unknown")
    llm_rounds: list[dict[str, Any]] = []

    def _try_round(label: str, **kw) -> Any:
        result = parser.parse(comment, storage_result, **kw)
        llm_rounds.append({"round": len(llm_rounds) + 1, "low_conf": result.low_confidence_slots, "errors": result.errors})
        return result

    async def _finalize(result: Any, via: str, rounds: int, **extra) -> dict[str, Any]:
        revision = build_revision(result)
        rec = await record_node(state, "parse_revision", f"解析完成 via {via} ({rounds}轮)")
        distill = build_distillation_record(
            dispatch_run_id=run_id, plan_version=plan_ver, engineer_comment=comment,
            storage_result=storage_result, llm_rounds=llm_rounds,
            final_revision=revision.model_dump(mode="json"),
            resolved_via=via, total_rounds=rounds, llm_model=llm_model,
        )
        try:
            parser.write_distillation(_DISTILLATION_DIR, distill)
        except Exception:
            pass  # distillation failure should not block dispatch
        return {"revision_resolved": True, "revision_request": revision.model_dump(mode="json"), "parse_round": rounds, **rec, **extra}

    # Round 1
    result = _try_round("round1")
    if result.all_resolved:
        return await _finalize(result, "direct", 1)

    # Retry on validation errors (no interrupt)
    if not result.valid:
        result = _try_round("retry1", validation_errors=result.errors)
        if result.all_resolved:
            return await _finalize(result, "retry", 1)

    # Clarification round 1
    clarification = parser.generate_clarification(result)
    if clarification:
        raw = interrupt({"type": "clarify", "dispatch_run_id": run_id, **clarification})
        answer = raw.get("answer", "") if isinstance(raw, dict) else str(raw)
        history = [{"question": clarification["question"], "answer": answer}]
        result = _try_round("round2", clarification_history=history)
        if result.all_resolved:
            return await _finalize(result, "clarify", 2, clarification_history=history)

        # Clarification round 2
        clarification2 = parser.generate_clarification(result)
        if clarification2:
            raw2 = interrupt({"type": "clarify", "dispatch_run_id": run_id, **clarification2})
            answer2 = raw2.get("answer", "") if isinstance(raw2, dict) else str(raw2)
            history.append({"question": clarification2["question"], "answer": answer2})
            result = _try_round("round3", clarification_history=history)
            if result.all_resolved:
                return await _finalize(result, "clarify", 3, clarification_history=history)

    # Fallback: structured form
    raw3 = interrupt({"type": "fallback_form", "dispatch_run_id": run_id})
    revision_data = raw3.get("revision", raw3) if isinstance(raw3, dict) else {}
    revision = DispatchRevision.model_validate(revision_data)
    rec = await record_node(state, "parse_revision", "结构化表单回退完成")
    llm_rounds.append({"round": len(llm_rounds) + 1, "fallback": True})
    distill = build_distillation_record(
        dispatch_run_id=run_id, plan_version=plan_ver, engineer_comment=comment,
        storage_result=storage_result, llm_rounds=llm_rounds,
        final_revision=revision.model_dump(mode="json"),
        resolved_via="fallback", total_rounds=len(llm_rounds), llm_model=llm_model,
    )
    try:
        parser.write_distillation(_DISTILLATION_DIR, distill)
    except Exception:
        pass
    return {"revision_resolved": True, "revision_request": revision.model_dump(mode="json"), "fallback_form": True, "parse_round": len(llm_rounds), **rec}


async def apply_revision_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    decision = ApprovalDecision.model_validate(state["approval_decision"])
    current = DispatchRevision.model_validate(state["revision_request"]) if state.get("revision_request") else None
    requested = decision.revision or DispatchRevision()
    merged = _merge_revision(current, requested)
    version = state["current_plan_version"] + 1
    objective = (merged.objective or DispatchObjective(state["objective"])).value
    rec = await record_node(state, "apply_revision", f"审批修改已合并, 开始生成方案 V{version}", payload={"new_version": version, "objective": objective})
    return {"revision_request": merged.model_dump(mode="json"), "current_plan_version": version, "objective": objective, "run_status": RunStatus.RUNNING.value, "approval_decision": None, "storage_result": None, "carbon_result": None, "factory_summary": None, **rec}


def _merge_revision(current: DispatchRevision | None, requested: DispatchRevision) -> DispatchRevision:
    return DispatchRevision(
        terminal_soc_min_ratio=max(filter(None, [requested.terminal_soc_min_ratio, current.terminal_soc_min_ratio if current else None]), default=None),
        reserve_soc_min_ratio=max(filter(None, [requested.reserve_soc_min_ratio, current.reserve_soc_min_ratio if current else None]), default=None),
        max_discharge_power_kw=min(filter(None, [requested.max_discharge_power_kw, current.max_discharge_power_kw if current else None]), default=None),
        max_charge_power_kw=min(filter(None, [requested.max_charge_power_kw, current.max_charge_power_kw if current else None]), default=None),
        max_cell_temperature_c=min(filter(None, [requested.max_cell_temperature_c, current.max_cell_temperature_c if current else None]), default=None),
        blocked_intervals=(current.blocked_intervals if current else []) + requested.blocked_intervals,
        objective=requested.objective or (current.objective if current else None),
        max_cycles_per_day=min(filter(None, [requested.max_cycles_per_day, current.max_cycles_per_day if current else None]), default=None),
    )


async def freeze_plan_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    rec = await record_node(state, "freeze_plan", f"方案 V{state['current_plan_version']} 已批准并冻结")
    sr = state.get("storage_result")
    if sr:
        _save_plan_version(state=state, storage_result=sr, is_final=True)
    return {"run_status": RunStatus.APPROVED.value, **rec}


async def close_rejected_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    rec = await record_node(state, "close_rejected", "方案已被工程师拒绝")
    return {"run_status": RunStatus.REJECTED.value, **rec}


async def fail_run_node(state: EnergyDispatchStateV2, runtime: Runtime[AppContextV2]) -> dict[str, Any]:
    error = ErrorDetail.model_validate(state.get("error") or {"code": "RUN_FAILED", "message": "运行失败", "retryable": False})
    rec = await record_node(state, "fail_run", error.message, status="failed")
    return {"run_status": RunStatus.FAILED.value, **rec}


def build_dispatch_graph(checkpointer: Any = None) -> Any:
    builder = StateGraph(EnergyDispatchStateV2, context_schema=AppContextV2)
    builder.add_node("ingest_raw_data", ingest_raw_data_node)
    builder.add_node("initialize_run", initialize_run_node)
    builder.add_node("load_inputs", load_inputs_node)
    builder.add_node("compute_tariff", compute_tariff_node)
    builder.add_node("compute_carbon_factors", compute_carbon_factors_node)
    builder.add_node("optimize_storage", optimize_storage_node)
    builder.add_node("compute_carbon_dispatch", compute_carbon_dispatch_node)
    builder.add_node("factory_summary", factory_summary_node)
    builder.add_node("storage_approval", storage_approval_node)
    builder.add_node("archive_data", archive_data_node)
    builder.add_node("parse_revision", parse_revision_node)
    builder.add_node("apply_revision", apply_revision_node)
    builder.add_node("freeze_plan", freeze_plan_node)
    builder.add_node("close_rejected", close_rejected_node)
    builder.add_node("fail_run", fail_run_node)
    builder.add_edge(START, "ingest_raw_data")
    builder.add_conditional_edges("ingest_raw_data", route_ok_or_fail, {"ok": "initialize_run", "failed": "fail_run"})
    builder.add_edge("initialize_run", "load_inputs")
    builder.add_conditional_edges("load_inputs", route_ok_or_fail, {"ok": "compute_tariff", "failed": "fail_run"})
    builder.add_conditional_edges("compute_tariff", route_ok_or_fail, {"ok": "compute_carbon_factors", "failed": "fail_run"})
    builder.add_conditional_edges("compute_carbon_factors", route_ok_or_fail, {"ok": "optimize_storage", "failed": "fail_run"})
    builder.add_conditional_edges("optimize_storage", route_ok_or_fail, {"ok": "compute_carbon_dispatch", "failed": "fail_run"})
    builder.add_conditional_edges("compute_carbon_dispatch", route_ok_or_fail, {"ok": "factory_summary", "failed": "fail_run"})
    builder.add_conditional_edges("factory_summary", route_ok_or_fail, {"ok": "storage_approval", "failed": "fail_run"})
    builder.add_conditional_edges("storage_approval", route_after_approval, {"approve": "freeze_plan", "reject": "close_rejected", "revise": "parse_revision"})
    builder.add_edge("parse_revision", "apply_revision")
    builder.add_edge("apply_revision", "optimize_storage")
    builder.add_edge("freeze_plan", "archive_data")
    builder.add_edge("archive_data", END)
    builder.add_edge("close_rejected", END)
    builder.add_edge("fail_run", END)
    return builder.compile(checkpointer=checkpointer) if checkpointer else builder.compile()
