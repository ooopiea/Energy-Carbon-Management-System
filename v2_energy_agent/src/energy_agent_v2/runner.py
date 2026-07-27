"""V2 运行入口：构建上下文、运行 LangGraph 调度、处理审批 interrupt。"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from energy_agent_v2.contracts import RunStatus
from energy_agent_v2.orchestration import AppContextV2, EnergyDispatchStateV2, build_dispatch_graph


def create_app_context(revision_parser: Any = None) -> AppContextV2:
    """实例化各算法模块，构建依赖注入容器。

    revision_parser 默认 None 时自动调用 create_llm_client() 检测环境变量：
    设了 LLM_API_KEY + LLM_BASE_URL 就接真实 GLM-5，没设就回退 mock。
    数据清洗/封存 agent 共享同一个 LLM client，没有 key 时均为 None（主图中跳过）。
    """
    from energy_agent_v2.algorithms.storage_optimizer import MILPStorageOptimizer
    from energy_agent_v2.algorithms.carbon_accounting import CarbonAccountant
    from energy_agent_v2.algorithms.tariff import TariffCalculator
    from energy_agent_v2.data.provider import SeedDataProvider
    from energy_agent_v2.data.csv_fetcher import CSVDataFetcher
    from energy_agent_v2.llm.client import create_llm_client
    from energy_agent_v2.llm.data_archive import DataArchiveAgent
    from energy_agent_v2.llm.data_ingest import DataIngestAgent
    from energy_agent_v2.llm.parse_revision import RevisionParser

    client = None
    if revision_parser is None:
        client = create_llm_client()
        if client is not None:
            revision_parser = RevisionParser(client=client)
    elif hasattr(revision_parser, "client"):
        client = revision_parser.client

    ingest_agent = DataIngestAgent(client=client) if client is not None else None
    archive_agent = DataArchiveAgent(client=client) if client is not None else None
    return AppContextV2(
        data_provider=SeedDataProvider(fetcher=CSVDataFetcher()),
        storage_optimizer=MILPStorageOptimizer(),
        carbon_accountant=CarbonAccountant(),
        tariff_calculator=TariffCalculator(),
        revision_parser=revision_parser,
        ingest_agent=ingest_agent,
        archive_agent=archive_agent,
    )


def make_initial_state(
    site_id: str,
    target_date: date,
    objective: str,
    requested_by: str = "demo.engineer",
    raw_file: dict[str, Any] | None = None,
    price_mode: str = "auto",
    battery_override: dict[str, Any] | None = None,
) -> EnergyDispatchStateV2:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    plan_id = f"plan-{uuid.uuid4().hex[:8]}"
    return EnergyDispatchStateV2(
        dispatch_run_id=run_id,
        thread_id=run_id,
        site_id=site_id,
        target_date=target_date.isoformat(),
        objective=objective,
        requested_by=requested_by,
        run_status=RunStatus.CREATED.value,
        current_plan_id=plan_id,
        current_plan_version=1,
       events=[],
       raw_file=raw_file,
        price_mode=price_mode,
        battery_override=battery_override,
   )


async def run_dispatch(
    ctx: AppContextV2,
    initial_state: EnergyDispatchStateV2,
    approval_decision: str = "approve",
    revision: dict[str, Any] | None = None,
    decision_fn: Any = None,
    clarify_fn: Any = None,
    fallback_revision: dict[str, Any] | None = None,
) -> EnergyDispatchStateV2:
    # Loop-resume through all interrupt types (approval, clarify, fallback) inside ONE
    # checkpointer so the event chain stays continuous.
    checkpointer = MemorySaver()
    graph = build_dispatch_graph(checkpointer=checkpointer)
    thread_id = initial_state["thread_id"]
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}

    result = await graph.ainvoke(initial_state, config=config, context=ctx)

    guard = 0
    while "__interrupt__" in result and guard < 10:
        guard += 1
        int_type = _get_interrupt_type(result)
        if int_type == "clarify":
            answer = clarify_fn(result) if clarify_fn else "yes"
            resume_value: Any = {"answer": answer}
        elif int_type == "fallback_form":
            resume_value = {"revision": fallback_revision or {}}
        else:
            if decision_fn is not None:
                ret = decision_fn(result)
                if len(ret) == 3:
                    dec, rev, comment = ret
                else:
                    dec, rev = ret
                    comment = f"auto-{dec}"
            else:
                dec, rev = approval_decision, revision
                comment = f"auto-{dec}"
            resume_value = {
                "dispatch_run_id": result.get("dispatch_run_id", initial_state["dispatch_run_id"]),
                "decision": dec,
                "comment": comment,
                "revision": rev,
                "decided_by": initial_state["requested_by"],
                "decided_at": datetime.now(UTC).isoformat(),
                "expected_plan_version": result.get("current_plan_version", 1),
                "idempotency_key": f"auto-{uuid.uuid4().hex[:16]}",
            }
        result = await graph.ainvoke(Command(resume=resume_value), config=config, context=ctx)

    return result

def _get_interrupt_type(result: dict[str, Any]) -> str:
    """Detect interrupt type from LangGraph __interrupt__ payload."""
    interrupts = result.get("__interrupt__", [])
    if not interrupts:
        return "approval"
    for item in interrupts:
        val = item.value if hasattr(item, "value") else item
        if isinstance(val, dict):
            return val.get("type", "approval")
    return "approval"

def _is_pending_approval(state: dict[str, Any]) -> bool:
    return state.get("run_status") == RunStatus.PENDING_APPROVAL.value


def print_result(state: dict[str, Any], label: str = "") -> None:
    """打印运行结果摘要。"""
    print(f"\n{'='*60}")
    if label:
        print(f"  {label}")
        print(f"{'='*60}")
    print(f"  运行ID: {state.get('dispatch_run_id', 'N/A')}")
    print(f"  最终状态: {state.get('run_status', 'N/A')}")
    print(f"  方案版本: V{state.get('current_plan_version', 'N/A')}")
    storage = state.get("storage_result")
    if storage:
        print(f"  --- 储能优化结果 ---")
        print(f"  日电度电费节省: {storage.get('energy_cost_saving_cny', 0):.0f} 元/日")
        print(f"  峰值削减: {storage.get('peak_reduction_kw', 0):.0f} kW")
        print(f"  最高电芯温度: {storage.get('max_cell_temperature_c', 0):.1f} C")
        print(f"  末端SOC: {storage.get('terminal_soc_ratio', 0):.3f}")
        print(f"  算法版本: {storage.get('algorithm_version', 'N/A')}")
        print(f"  求解器状态: {storage.get('solver_status', 'N/A')}")
        cc = storage.get("constraint_check", {})
        print(f"  约束校验: {'通过' if cc.get('passed') else '未通过'}")
    tariff = state.get("tariff_result")
    if tariff:
        print(f"  --- 电费(日度/月度分离) ---")
        print(f"  日电度电费: {tariff.get('energy_cost_cny', 0):.0f} 元")
        print(f"  需量电费(月): {tariff.get('demand_cost_cny', 0):.0f} 元")
        print(f"  政府基金(月): {tariff.get('gov_fund_cost_cny', 0):.0f} 元")
        print(f"  力调电费(月): {tariff.get('reactive_adjustment_cny', 0):.0f} 元")
        print(f"  综合电价: {tariff.get('effective_price_cny_per_kwh', 0):.4f} 元/kWh")
    carbon = state.get("carbon_result")
    if carbon:
        print(f"  --- 电碳双因子 ---")
        print(f"  直接碳减排 C(tau): {carbon.get('direct_carbon_reduction_kg', 0):.1f} kg")
        print(f"  责任碳减排 Cr(tau): {carbon.get('responsibility_carbon_reduction_kg', 0):.1f} kg")
    ingest = state.get("ingest_result")
    if ingest:
        print(f"  --- 数据清洗 ---")
        print(f"  文件类型: {ingest.get('file_type', 'N/A')}")
        print(f"  置信度: {ingest.get('overall_confidence', 0):.2f}")
        print(f"  字段映射: {len(ingest.get('field_mappings', []))} 条")
    archive = state.get("archive_result")
    if archive:
        print(f"  --- 数据封存 ---")
        print(f"  封存版本: {archive.get('archive_version', 'N/A')}")
        print(f"  校验和: {archive.get('checksum', 'N/A')}")
    events = state.get("events", [])
    if events:
        print(f"  --- 编排事件 ({len(events)} 条) ---")
        for ev in events:
            status_icon = "+" if ev["status"] == "succeeded" else "x"
            dur = f" [{ev['duration_ms']}ms]" if ev.get("duration_ms") else ""
            print(f"    {status_icon} {ev['node_id']}: {ev['summary']}{dur}")
    print(f"{'='*60}\n")


def save_result_json(state: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"结果已保存: {path}")
