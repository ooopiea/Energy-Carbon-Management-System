"""FastAPI 服务器：REST + WebSocket 实时推送。

启动后自动运行模拟引擎，前端通过 WebSocket 接收实时状态更新。
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_src = str(_Path(__file__).resolve().parent.parent)
if _src not in _sys.path:
    _sys.path.insert(0, _src)

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agents.engine import get_engine
from core.time_engine import get_time_engine
from graph.workflow import get_graph_topology


_bg_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bg_task
    engine = get_engine()
    async def simulation_loop():
        while True:
            await engine.run_tick()
            await asyncio.sleep(0.5)
    _bg_task = asyncio.create_task(simulation_loop())
    yield
    _bg_task.cancel()


app = FastAPI(title="V3 工业能源管理系统", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件（前端 build 产物）
_frontend_dist = Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist"
if _frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=_frontend_dist / "assets"), name="assets")


@app.get("/api/state")
async def get_state():
    """获取完整运行状态。"""
    return get_engine().get_state()


@app.get("/api/time")
async def get_time():
    """获取当前模拟时间。"""
    return get_time_engine().tick_info()


@app.post("/api/time/pause")
async def pause():
    get_time_engine().pause()
    return {"status": "paused", "time": get_time_engine().tick_info()}


@app.post("/api/time/resume")
async def resume():
    get_time_engine().resume()
    return {"status": "resumed", "time": get_time_engine().tick_info()}


@app.post("/api/time/reset")
async def reset():
    get_time_engine().reset()
    return {"status": "reset", "time": get_time_engine().tick_info()}


@app.post("/api/approval/{gate_id}")
async def set_approval(gate_id: str, decision: str = "approve", comment: str = ""):
    """提交工程师审批决策。"""
    get_engine().set_approval(gate_id, decision, comment)
    return {"status": "ok", "gate_id": gate_id, "decision": decision}


@app.post("/api/auto_approve/{enabled}")
async def set_auto_approve(enabled: bool):
    """设置自动审批模式。"""
    get_engine().set_auto_approve(enabled)
    return {"auto_approve": enabled}


@app.post("/api/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: str):
    """确认告警。"""
    ok = get_engine().acknowledge_alert(alert_id)
    return {"acknowledged": ok}


@app.get("/api/graph")
async def get_graph():
    """获取 LangGraph 工作流拓扑。"""
    return get_graph_topology()


@app.get("/api/agents")
async def get_agents():
    """获取 Agent 节点状态列表。"""
    state = get_engine().get_state()
    return state["agent_nodes"]


@app.get("/api/reports")
async def get_reports():
    """获取报告列表。"""
    state = get_engine().get_state()
    return state["reports"]


@app.get("/api/alerts")
async def get_alerts():
    """获取告警列表。"""
    state = get_engine().get_state()
    return state["alerts"]


@app.get("/api/day_ahead")
async def get_day_ahead():
    """获取日前计划数据。"""
    state = get_engine().get_state()
    return state["day_ahead"]


@app.get("/api/series")
async def get_series():
    """获取实时时间序列数据。"""
    state = get_engine().get_state()
    return state["series"]


@app.get("/api/storage")
async def get_storage():
    """获取储能系统详情。"""
    state = get_engine().get_state()
    return {
        "summary": state.get("storage_summary"),
        "soc": state.get("storage_soc"),
        "temp_c": state.get("storage_temp_c"),
        "power_kw": state.get("storage_power_kw"),
        "day_ahead_plan": state["day_ahead"].get("soc_plan", []),
        "power_plan": state["day_ahead"].get("storage_plan", []),
    }


@app.get("/api/hvac")
async def get_hvac():
    """获取 HVAC 系统详情。"""
    state = get_engine().get_state()
    return {
        "summary": state.get("hvac_summary"),
        "chiller_topology": state.get("chiller_topology"),
        "supply_temp_c": state.get("hvac_supply_temp_c"),
        "power_kw": state.get("hvac_power_kw"),
        "day_ahead_plan": state["day_ahead"].get("hvac_plan", []),
    }


@app.get("/api/carbon")
async def get_carbon():
    """获取电碳数据。"""
    state = get_engine().get_state()
    return {
        "current_factor": state.get("carbon_factor"),
        "dispatch": state.get("carbon_dispatch"),
        "c_series": state["day_ahead"].get("carbon_c", []),
        "cr_series": state["day_ahead"].get("carbon_cr", []),
    }


@app.get("/api/tariff")
async def get_tariff():
    """获取电费数据。"""
    state = get_engine().get_state()
    return {
        "current_price": state.get("price"),
        "current_period": state.get("tariff_period"),
        "summary": state.get("tariff_summary"),
        "price_series": state["day_ahead"].get("price", []),
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket 实时推送。"""
    await ws.accept()
    engine = get_engine()
    engine.add_ws_client(ws)
    try:
        # 立即推送当前状态
        import json
        state = engine.get_state()
        await ws.send_text(json.dumps({"type": "state_update", "data": state}, ensure_ascii=False, default=str))
        # 保持连接，接收客户端消息
        while True:
            data = await ws.receive_text()
            # 可处理客户端命令
            pass
    except WebSocketDisconnect:
        engine.remove_ws_client(ws)
    except Exception:
        engine.remove_ws_client(ws)


@app.get("/")
async def index():
    """前端入口。"""
    index_file = _frontend_dist / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "V3 工业能源管理系统 API", "docs": "/docs", "websocket": "/ws"}
