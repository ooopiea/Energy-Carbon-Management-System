"""V2 园区能源调度 FastAPI server。

把 LangGraph 编排(储能优化 + 电碳双因子 + 人工审批)暴露成 HTTP API。
启动:
    python server.py
  或
    uvicorn server:app --reload --port 8000
启动后访问 http://localhost:8000/docs 可在浏览器交互测试所有接口。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 未安装态:把 src 加入 sys.path(与 conftest.py 一致),保证可直接 python server.py 运行
_SRC = str(Path(__file__).resolve().parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel

from energy_agent_v2.contracts import DispatchObjective
from energy_agent_v2.orchestration import AppContextV2, build_dispatch_graph
from energy_agent_v2.runner import create_app_context, make_initial_state

# ---------------------------------------------------------------------------
# 全局单例:checkpointer + 编译好的 graph + 依赖注入容器
# 进程启动时初始化一次,所有请求复用。
# 注意:MemorySaver 存在内存,服务重启会丢失运行历史;
#       后续 24h 部署可换 SqliteSaver / PostgresSaver 做持久化。
# ---------------------------------------------------------------------------
_checkpointer = MemorySaver()
_graph = build_dispatch_graph(checkpointer=_checkpointer)
_context: AppContextV2 = create_app_context()

# 最近一次状态快照(thread_id -> 结果),供 GET 查询用
_snapshots: dict[str, dict[str, Any]] = {}

app = FastAPI(title="V2 园区能源调度 API", version="2.0.0")
# 允许网页跨域访问(开发期全放开;上线后应收紧到你的域名)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class CreateRunRequest(BaseModel):
    site_id: str = "huanghua"
    target_date: str  # ISO 格式 "2026-07-27"
    objective: str = "min_cost"  # min_cost | limit_peak_demand | min_carbon | weighted
    requested_by: str = "web"
    price_mode: str = "auto"  # tou | rtp | auto（见 SPEC_SCENARIOS）
    battery_override: dict[str, Any] | None = None  # {"capacity_kwh", "max_charge_power_kw", ...}


class ResumeRequest(BaseModel):
    # 审批决策(approval interrupt 时用)
    decision: str | None = None  # approve | reject | revise
    comment: str = ""
    revision: dict[str, Any] | None = None
    decided_by: str = "web"
    # 澄清回答(clarify interrupt 时用)
    answer: str | None = None


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}


def _interrupts(result: dict[str, Any]) -> list:
    return result.get("__interrupt__") or []


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """提取 interrupt 的完整 payload,供前端展示。"""
    for item in _interrupts(result):
        val = item.value if hasattr(item, "value") else item
        if isinstance(val, dict):
            return val
    return None


def _interrupt_type(result: dict[str, Any]) -> str:
    """识别当前 interrupt 类型:approval / clarify / fallback_form。"""
    payload = _interrupt_payload(result)
    return payload.get("type", "approval") if payload else "approval"


def _is_paused(result: dict[str, Any]) -> bool:
    return len(_interrupts(result)) > 0


def _build_resume_value(snap: dict[str, Any], req: ResumeRequest) -> dict[str, Any]:
    """根据 interrupt 类型构造 LangGraph Command(resume=...) 的值。"""
    int_type = snap.get("interrupt_type", "approval")

    if int_type == "clarify":
        # LLM 澄清提问,前端返回自由文本回答
        return {"answer": req.answer or "yes"}

    if int_type == "fallback_form":
        # 结构化表单回退,前端返回完整 revision
        return {"revision": req.revision or {}}

    # approval:工程师审批,需构造 ApprovalDecision 兼容的结构
    decision = req.decision or "approve"
    return {
        "dispatch_run_id": snap.get("dispatch_run_id", ""),
        "decision": decision,
        "comment": req.comment,
        # approve / reject 不允许携带 revision(契约约束),仅 revise 时透传
        "revision": req.revision if decision == "revise" else None,
        "decided_by": req.decided_by,
        "decided_at": datetime.now(UTC).isoformat(),
        "expected_plan_version": snap.get("current_plan_version", 1),
        "idempotency_key": f"web-{uuid4().hex[:16]}",
    }


def _snapshot(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """把 ainvoke 结果整理成前端友好的快照并缓存。"""
    paused = _is_paused(result)
    snap = {
        "thread_id": thread_id,
        "dispatch_run_id": result.get("dispatch_run_id"),
        "run_status": result.get("run_status"),
        "current_plan_id": result.get("current_plan_id"),
        "current_plan_version": result.get("current_plan_version", 1),
        "objective": result.get("objective"),
        "input_bundle": result.get("input_bundle"),
        "storage_result": result.get("storage_result"),
        "tariff_result": result.get("tariff_result"),
       "carbon_result": result.get("carbon_result"),
       "carbon_factors": result.get("carbon_factors"),
       "factory_summary": result.get("factory_summary"),
       "approval_decision": result.get("approval_decision"),
       "events": result.get("events", []),
       "error": result.get("error"),
       "data_source_info": (result.get("input_bundle") or {}).get("data_source_info", {}),
        "paused": paused,
        "interrupt_type": _interrupt_type(result) if paused else None,
        "interrupt": _interrupt_payload(result) if paused else None,
    }
    _snapshots[thread_id] = snap
    return snap


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@app.get("/")
async def root() -> dict:
    return {"service": "V2 园区能源调度 API", "docs": "/docs", "health": "/health"}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "langgraph": True,
        "llm_parser": _context.revision_parser is not None,
    }


@app.get("/dashboard")
async def dashboard():
    """提供实时交互前端页面(单文件 HTML,调本服务的 API)。"""
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).resolve().parent / "dashboard_new.html")


@app.get("/chart.min.js")
async def chart_js():
    """Serve local Chart.js library (CDN blocked in local sandbox)."""
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).resolve().parent / "chart.min.js", media_type="application/javascript")


@app.get("/api/objectives")
async def objectives() -> dict:
    return {"objectives": [o.value for o in DispatchObjective]}


@app.get("/api/data-health")
async def data_health() -> dict:
    """报告 CSV 数据覆盖范围与可用日期，供前端校验日期有效性。"""
    from energy_agent_v2.data.csv_fetcher import CSVDataFetcher

    f = CSVDataFetcher()
    try:
        total = f._load_csv(f.hunan_dir / "实时总发电出力.csv")
        price = f._load_csv(f.price_path)
    except Exception:
        return {"available": False}

    gen_dates = sorted({ts.date().isoformat() for ts in total})
    price_dates = sorted({ts.date().isoformat() for ts in price})
    if not gen_dates or not price_dates:
        return {"available": False}

    load_idx = f._build_load_index()
    load_dates = sorted(d.isoformat() for d in load_idx) if load_idx else []

    overlap = sorted(set(gen_dates) & set(price_dates))
    return {
        "available": True,
        "generation_coverage": {
            "start": gen_dates[0],
            "end": gen_dates[-1],
            "days": len(gen_dates),
        },
        "price_coverage": {
            "start": price_dates[0],
            "end": price_dates[-1],
            "days": len(price_dates),
        },
        "load_coverage": {
            "start": load_dates[0],
            "end": load_dates[-1],
            "days": len(load_dates),
        } if load_dates else None,
        "overlap_days": len(overlap),
        "data_version": "csv-realtime",
    }


@app.post("/api/runs")
async def create_run(req: CreateRunRequest) -> dict:
    """创建一次调度,自动跑到第一个 interrupt(通常是 storage_approval)。"""
    try:
        target = date.fromisoformat(req.target_date)
    except ValueError:
        raise HTTPException(400, f"target_date 格式错误,应为 ISO 日期,收到 {req.target_date!r}")

    valid = {o.value for o in DispatchObjective}
    if req.objective not in valid:
        raise HTTPException(400, f"objective 非法,可选 {sorted(valid)}")

    state = make_initial_state(
        site_id=req.site_id,
        target_date=target,
        objective=req.objective,
        requested_by=req.requested_by,
        price_mode=req.price_mode,
        battery_override=req.battery_override,
    )
    thread_id = state["thread_id"]

    try:
        result = await _graph.ainvoke(state, config=_config(thread_id), context=_context)
    except Exception as exc:
        raise HTTPException(500, f"调度执行失败: {exc}") from exc

    return _snapshot(thread_id, result)


@app.get("/api/runs/{thread_id}")
async def get_run(thread_id: str) -> dict:
    if thread_id not in _snapshots:
        raise HTTPException(404, f"未找到 thread_id={thread_id}(可能服务已重启)")
    return _snapshots[thread_id]


@app.post("/api/runs/{thread_id}/resume")
async def resume_run(thread_id: str, req: ResumeRequest) -> dict:
    """对处于 interrupt 的运行提交决策,继续执行到下一个 interrupt 或结束。"""
    snap = _snapshots.get(thread_id)
    if snap is None:
        raise HTTPException(404, f"未找到 thread_id={thread_id}")
    if not snap.get("paused"):
        raise HTTPException(400, "该运行未处于 interrupt 状态,无需 resume")

    resume_value = _build_resume_value(snap, req)
    try:
        result = await _graph.ainvoke(
            Command(resume=resume_value), config=_config(thread_id), context=_context
        )
    except Exception as exc:
        raise HTTPException(500, f"resume 执行失败: {exc}") from exc

    return _snapshot(thread_id, result)


# ---------------------------------------------------------------------------
# Agent API: 轻量级 per-agent 端点，可独立调用单个 agent
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    file_name: str
    file_path: str = ""
    file_format: str = "csv"
    uploaded_by: str = "web"
    raw_content_preview: str = ""


class ApprovalInterpretRequest(BaseModel):
    comment: str
    storage_result: dict[str, Any]


class ArchiveAgentRequest(BaseModel):
    ingest_result: dict[str, Any]


@app.post("/api/agents/ingest")
async def agent_ingest(req: IngestRequest) -> dict:
    """DataIngestAgent: 原始文件 -> 类型判断 + 字段映射 + 质量检测。"""
    from datetime import datetime

    from energy_agent_v2.contracts import RawDataFile

    if _context.ingest_agent is None:
        raise HTTPException(503, "ingest_agent 未配置（需 LLM_API_KEY）")
    raw = RawDataFile(
        file_name=req.file_name,
        file_path=req.file_path,
        file_format=req.file_format,
        uploaded_by=req.uploaded_by,
        uploaded_at=datetime.now(UTC),
        raw_content_preview=req.raw_content_preview,
        file_size_bytes=len(req.raw_content_preview.encode()),
    )
    result = _context.ingest_agent.ingest(raw)
    return result.model_dump(mode="json")


@app.post("/api/agents/approval/interpret")
async def agent_approval_interpret(req: ApprovalInterpretRequest) -> dict:
    """StorageApprovalAgent: 工程师指令 -> 物理含义 + 结构化约束 + 重算决策。"""
    if _context.approval_agent is None:
        raise HTTPException(503, "approval_agent 未配置（需 LLM_API_KEY）")
    result = _context.approval_agent.interpret_command(req.comment, req.storage_result)
    return result


@app.post("/api/agents/archive")
async def agent_archive(req: ArchiveAgentRequest) -> dict:
    """DataArchiveAgent: 清洗结果 -> 血缘记录 + 落盘。"""
    from energy_agent_v2.contracts import DataIngestResult
    from pydantic import ValidationError as PydanticValidationError

    if _context.archive_agent is None:
        raise HTTPException(503, "archive_agent 未配置（需 LLM_API_KEY）")
    try:
        ingest = DataIngestResult.model_validate(req.ingest_result)
    except PydanticValidationError as e:
        raise HTTPException(422, f"ingest_result validation failed: {e.errors()[:3]}")
    result = _context.archive_agent.archive(ingest)
    return result.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Daily JSON Stream: 把一次完整 run 重建为结构化每日记录
# ---------------------------------------------------------------------------


class AnomalyAnalyzeRequest(BaseModel):
    signals: list[dict[str, Any]]


class DistillationReviewRequest(BaseModel):
    records: list[dict[str, Any]] | None = None
    review_period: str = ""


@app.post("/api/agents/anomaly/analyze")
async def agent_anomaly_analyze(req: AnomalyAnalyzeRequest) -> dict:
    from energy_agent_v2.contracts import AnomalySignal
    from pydantic import ValidationError as PydanticValidationError

    if _context.anomaly_agent is None:
        raise HTTPException(503, "anomaly_agent not configured (LLM_API_KEY required)")
    if not req.signals:
        raise HTTPException(400, "signals must not be empty")
    try:
        signals = [AnomalySignal.model_validate(s) for s in req.signals]
    except PydanticValidationError as e:
        raise HTTPException(422, f"signal validation failed: {e.errors()[:3]}")
    alert = _context.anomaly_agent.analyze(signals)
    return alert.model_dump(mode="json")


@app.post("/api/agents/distillation/review")
async def agent_distillation_review(req: DistillationReviewRequest) -> dict:
    if _context.distillation_agent is None:
        raise HTTPException(503, "distillation_agent not configured (LLM_API_KEY required)")
    insight = _context.distillation_agent.review(
        records=req.records,
        review_period=req.review_period,
    )
    return insight.model_dump(mode="json")

@app.get("/api/daily-stream/{thread_id}")
async def daily_stream(thread_id: str) -> dict:
    """重建每日 JSON 流：清洗输入 → MILP 结果 → 审批决策 → 重算结果。"""
    snap = _snapshots.get(thread_id)
    if snap is None:
        raise HTTPException(404, f"未找到 thread_id={thread_id}")
    events = snap.get("events", [])
    storage = snap.get("storage_result")
    tariff = snap.get("tariff_result")
    carbon_factors = snap.get("carbon_factors")
    carbon_result = snap.get("carbon_result")
    plan_versions: dict[int, list] = {}
    for ev in events:
        ver = 1
        for p in ev.get("event_id", "").split(":"):
            if p.startswith("v") and p[1:].isdigit():
                ver = int(p[1:])
                break
        plan_versions.setdefault(ver, []).append(ev)
    stages = []
    for ver in sorted(plan_versions.keys()):
        ver_events = plan_versions[ver]
        stage = {"plan_version": ver, "events": [{"node_id": e["node_id"], "status": e["status"], "summary": e["summary"], "duration_ms": e.get("duration_ms")} for e in ver_events], "objective": snap.get("objective")}
        approval_evs = [e for e in ver_events if e["node_id"] == "storage_approval"]
        if approval_evs:
            payload = approval_evs[0].get("payload", {})
            stage["approval"] = {"decision": payload.get("decision"), "decided_by": payload.get("decided_by"), "comment": payload.get("comment")}
        if storage and ver == sorted(plan_versions.keys())[-1]:
            stage["storage_summary"] = {"plan_id": storage.get("plan_id"), "plan_version": storage.get("plan_version"), "energy_cost_saving_cny": storage.get("energy_cost_saving_cny", 0), "peak_reduction_kw": storage.get("peak_reduction_kw", 0), "max_cell_temperature_c": storage.get("max_cell_temperature_c", 0), "terminal_soc_ratio": storage.get("terminal_soc_ratio", 0), "solver_status": storage.get("solver_status"), "constraint_passed": storage.get("constraint_check", {}).get("passed", False)}
        stages.append(stage)
    cf_c = carbon_factors.get("direct_factor_c_kg_per_kwh", []) if carbon_factors else []
    cf_cr = carbon_factors.get("responsibility_factor_cr_kg_per_kwh", []) if carbon_factors else []
    return {"thread_id": thread_id, "dispatch_run_id": snap.get("dispatch_run_id"), "run_status": snap.get("run_status"), "total_plan_versions": len(plan_versions), "tariff_summary": {"daily_energy_cost_cny": tariff.get("energy_cost_cny") if tariff else None, "monthly_demand_cost_cny": tariff.get("demand_cost_cny") if tariff else None, "effective_price_cny_per_kwh": tariff.get("effective_price_cny_per_kwh") if tariff else None} if tariff else None, "carbon_factors_summary": {"C_mean": round(sum(cf_c)/len(cf_c), 4) if cf_c else None, "Cr_mean": round(sum(cf_cr)/len(cf_cr), 4) if cf_cr else None, "library_version": carbon_factors.get("emission_factor_library_version") if carbon_factors else None} if carbon_factors else None, "carbon_dispatch_summary": {"direct_carbon_reduction_kg": carbon_result.get("direct_carbon_reduction_kg") if carbon_result else None, "responsibility_carbon_reduction_kg": carbon_result.get("responsibility_carbon_reduction_kg") if carbon_result else None} if carbon_result else None, "stages": stages, "event_count": len(events)}


if __name__ == "__main__":
    import uvicorn

    import argparse
    import socket

    parser = argparse.ArgumentParser(description="V2 园区能源调度 FastAPI server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--reload", action="store_true", help="开发热重载（默认关闭）")
    args = parser.parse_args()

    # 端口占用预检：避免残留进程导致的 bind 冲突
    _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _sock.bind((args.host, args.port))
    except OSError:
        print(f"端口 {args.port} 已被占用，请先关闭占用进程或换端口（--port N）")
        raise
    finally:
        _sock.close()

    print(f"V2 能源调度服务启动 -> http://localhost:{args.port}")
    print(f"  仪表盘: http://localhost:{args.port}/dashboard")
    print(f"  API文档: http://localhost:{args.port}/docs")
    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload)
