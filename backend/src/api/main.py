"""Production-oriented FastAPI application for REST and WebSocket clients."""
from __future__ import annotations

import asyncio
import json
import os
import sys as _sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from pathlib import Path as _Path
from typing import Literal

_src = str(_Path(__file__).resolve().parent.parent)
if _src not in _sys.path:
    _sys.path.insert(0, _src)

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.engine import (
    ApprovalBindingMismatch,
    ApprovalConflict,
    ApprovalNotFound,
    InvalidControlAction,
    InvalidApprovalDecision,
    SimulationEngine,
    get_engine,
)
from graph.workflow import get_graph_topology


class ApprovalRequest(BaseModel):
    decision: str = "approve"
    comment: str = Field(default="", max_length=2000)
    actor: str = Field(default="engineer", min_length=1, max_length=128)
    report_id: str | None = None
    report_hash: str | None = None


class ControlActionRequest(BaseModel):
    system: Literal["overview", "storage", "hvac"]
    action: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    value: float
    unit: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=2000)
    actor: str = Field(min_length=1, max_length=128)


def _allowed_origins() -> list[str]:
    configured = os.getenv("ENERGY_CORS_ORIGINS", "")
    if configured.strip():
        return [item.strip() for item in configured.split(",") if item.strip()]
    return ["http://127.0.0.1:5173", "http://localhost:5173"]


def create_app(engine: SimulationEngine | None = None, start_background: bool = True) -> FastAPI:
    runtime_engine = engine or get_engine()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task: asyncio.Task | None = None

        async def simulation_supervisor() -> None:
            while True:
                try:
                    await runtime_engine.run_tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # keep API alive but expose degraded health
                    runtime_engine.record_runtime_error(exc)
                    await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(0.5)

        if start_background:
            task = asyncio.create_task(simulation_supervisor(), name="energy-simulation-loop")
        app.state.simulation_task = task
        yield
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    application = FastAPI(
        title="V3 工业能源管理系统",
        version="3.1.0",
        lifespan=lifespan,
    )
    application.state.engine = runtime_engine
    application.state.simulation_task = None
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )

    frontend_dist = Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist"
    if (frontend_dist / "assets").exists():
        application.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

    @application.get("/api/health")
    async def health():
        result = runtime_engine.health()
        task = application.state.simulation_task
        result["background_task_running"] = bool(task is not None and not task.done())
        return result

    @application.get("/api/state")
    async def get_state():
        return runtime_engine.get_state()

    @application.get("/api/time")
    async def get_time():
        return runtime_engine.get_state()["time"]

    @application.post("/api/time/pause")
    async def pause():
        runtime_engine.pause()
        await runtime_engine.broadcast_state()
        return {"status": "paused", "time": runtime_engine.get_state()["time"]}

    @application.post("/api/time/resume")
    async def resume():
        runtime_engine.resume()
        await runtime_engine.broadcast_state()
        return {"status": "resumed", "time": runtime_engine.get_state()["time"]}

    @application.post("/api/time/reset")
    async def reset():
        await runtime_engine.reset()
        await runtime_engine.broadcast_state()
        return {
            "status": "reset",
            "time": runtime_engine.get_state()["time"],
            "state": runtime_engine.get_state(),
        }

    @application.post("/api/approval/{gate_id}")
    async def set_approval(
        gate_id: str,
        payload: ApprovalRequest | None = Body(default=None),
        decision: str | None = Query(default=None),
        comment: str = Query(default="", max_length=2000),
        actor: str = Query(default="engineer", min_length=1, max_length=128),
    ):
        # JSON is canonical; query parameters remain for the existing frontend.
        request = payload or ApprovalRequest(
            decision=decision or "approve",
            comment=comment,
            actor=actor,
        )
        try:
            gate = await runtime_engine.submit_approval(
                gate_id,
                request.decision,
                request.comment,
                request.actor,
                request.report_id,
                request.report_hash,
            )
        except ApprovalNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidApprovalDecision as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (ApprovalConflict, ApprovalBindingMismatch) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await runtime_engine.broadcast_state()
        return {
            "status": "ok",
            "gate_id": gate_id,
            "decision": request.decision,
            "gate": gate.model_dump(mode="json"),
        }

    @application.post("/api/auto_approve/{enabled}")
    async def set_auto_approve(enabled: bool):
        runtime_engine.set_auto_approve(enabled)
        return {"auto_approve": enabled}

    @application.post("/api/control-actions")
    async def create_control_action(payload: ControlActionRequest):
        try:
            record = await runtime_engine.submit_control_action(**payload.model_dump())
        except ApprovalConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidControlAction as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await runtime_engine.broadcast_state()
        return record.model_dump(mode="json")

    @application.get("/api/control-actions")
    async def list_control_actions(limit: int = Query(default=30, ge=1, le=100)):
        return runtime_engine.get_control_actions(limit)

    @application.post("/api/alerts/{alert_id}/acknowledge")
    async def acknowledge_alert(alert_id: str):
        if not runtime_engine.acknowledge_alert(alert_id):
            raise HTTPException(status_code=404, detail="告警不存在")
        await runtime_engine.broadcast_state()
        return {"acknowledged": True}

    @application.get("/api/graph")
    async def get_graph():
        return get_graph_topology()

    @application.get("/api/agents")
    async def get_agents():
        return runtime_engine.get_state()["agent_nodes"]

    @application.get("/api/reports")
    async def get_reports():
        return runtime_engine.get_state()["reports"]

    @application.get("/api/alerts")
    async def get_alerts():
        return runtime_engine.get_state()["alerts"]

    @application.get("/api/day_ahead")
    async def get_day_ahead():
        return runtime_engine.get_state()["day_ahead"]

    @application.get("/api/series")
    async def get_series():
        return runtime_engine.get_state()["series"]

    @application.get("/api/storage")
    async def get_storage():
        state = runtime_engine.get_state()
        return {
            "summary": state.get("storage_summary"),
            "soc": state.get("storage_soc"),
            "temp_c": state.get("storage_temp_c"),
            "power_kw": state.get("storage_power_kw"),
            "day_ahead_plan": state["day_ahead"].get("soc_plan", []),
            "power_plan": state["day_ahead"].get("storage_plan", []),
            "dispatch": state["physical_dispatch"],
        }

    @application.get("/api/hvac")
    async def get_hvac():
        state = runtime_engine.get_state()
        return {
            "summary": state.get("hvac_summary"),
            "chiller_topology": state.get("chiller_topology"),
            "supply_temp_c": state.get("hvac_supply_temp_c"),
            "power_kw": state.get("hvac_power_kw"),
            "day_ahead_plan": state["day_ahead"].get("hvac_plan", []),
            "dispatch": state["physical_dispatch"],
        }

    @application.get("/api/carbon")
    async def get_carbon():
        state = runtime_engine.get_state()
        return {
            "current_factor": state.get("carbon_factor"),
            "dispatch": state.get("carbon_dispatch"),
            "c_series": state["day_ahead"].get("carbon_c", []),
            "cr_series": state["day_ahead"].get("carbon_cr", []),
        }

    @application.get("/api/tariff")
    async def get_tariff():
        state = runtime_engine.get_state()
        return {
            "current_price": state.get("price"),
            "current_period": state.get("tariff_period"),
            "summary": state.get("tariff_summary"),
            "price_series": state["day_ahead"].get("price", []),
        }

    @application.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        runtime_engine.add_ws_client(ws)
        try:
            state = runtime_engine.get_state()
            await ws.send_text(
                json.dumps(
                    {"type": "state_update", "data": state},
                    ensure_ascii=False,
                    default=str,
                )
            )
            while True:
                message = await ws.receive_text()
                if message == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            pass
        finally:
            runtime_engine.remove_ws_client(ws)

    @application.get("/")
    async def index():
        index_file = frontend_dist / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {"message": "V3 工业能源管理系统 API", "docs": "/docs", "websocket": "/ws"}

    return application


app = create_app()
