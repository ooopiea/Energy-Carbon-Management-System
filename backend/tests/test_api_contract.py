from __future__ import annotations

import httpx
import pytest

from agents.engine import SimulationEngine
from api.main import create_app


@pytest.mark.asyncio
async def test_approval_api_uses_domain_error_codes(tmp_path):
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    app = create_app(engine=engine, start_background=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/api/approval/not-a-gate", json={"decision": "approve"})
        invalid = await client.post(
            "/api/approval/forecast_approval", json={"decision": "invalid"}
        )
        accepted = await client.post(
            "/api/approval/forecast_approval",
            json={"decision": "approve", "actor": "tester"},
        )
        duplicate = await client.post(
            "/api/approval/forecast_approval",
            json={"decision": "approve", "actor": "tester"},
        )

    assert missing.status_code == 404
    assert invalid.status_code == 422
    assert accepted.status_code == 200
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_health_and_reset_api_report_fresh_state(tmp_path):
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    app = create_app(engine=engine, start_background=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/api/health")
        reset = await client.post("/api/time/reset")

    assert health.status_code == 200
    assert health.json()["status"] in {"healthy", "starting"}
    assert reset.status_code == 200
    assert reset.json()["state"]["approval_gates"]["forecast_approval"]["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_physical_control_requires_approval_and_enters_next_command(tmp_path):
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    app = create_app(engine=engine, start_background=False)
    transport = httpx.ASGITransport(app=app)
    payload = {
        "system": "storage",
        "action": "manual_setpoint",
        "target": "power_kw",
        "value": 120.0,
        "unit": "kW",
        "reason": "integration test",
        "actor": "tester",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        blocked = await client.post("/api/control-actions", json=payload)
        await engine.submit_approval("forecast_approval", "approve", actor="tester")
        await engine.submit_approval("storage_approval", "approve", actor="tester")
        accepted = await client.post("/api/control-actions", json=payload)
        await engine.submit_approval("hvac_approval", "approve", actor="tester")
        await engine.run_step(3)
        history = await client.get("/api/control-actions")

    assert blocked.status_code == 409
    assert accepted.status_code == 200
    execution = engine.get_state()["physical_dispatch"]["last_execution"]
    assert execution["command"]["storage_power_kw"] == 120.0
    assert execution["command"]["manual_override"]["actions"][0]["action_id"]
    assert history.json()[0]["status"] == "executed"


@pytest.mark.asyncio
async def test_full_approval_report_endpoint_returns_schedule_and_plan(tmp_path):
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    report_id = engine.get_state()["approval_gates"]["forecast_approval"]["report_id"]
    app = create_app(engine=engine, start_background=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/reports/{report_id}")
        missing = await client.get("/api/reports/not-found")
    assert response.status_code == 200
    detail = response.json()["data"]["report_detail"]
    assert len(detail["schedule_table"]) == 96
    assert len(detail["plan_table"]) == 96
    assert missing.status_code == 404
