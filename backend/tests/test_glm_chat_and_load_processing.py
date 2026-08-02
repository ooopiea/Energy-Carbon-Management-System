from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from agents.chat_service import FacilityChatService
from agents.engine import SimulationEngine
from core.time_engine import TimeEngine
from data.raw_loader import get_load_data_range, process_load_to_15min
from llm.glm_client import GlmClient, GlmConfig


def test_load_processing_preserves_every_hour_energy() -> None:
    hourly = [70_000.0 + index * 100 for index in range(24)]
    processed, provenance = process_load_to_15min(hourly, 60)

    assert len(processed) == 96
    assert provenance["method"] == "shape_preserving_interval_disaggregation"
    for hour, source in enumerate(hourly):
        block = processed[hour * 4 : hour * 4 + 4]
        assert sum(block) / 4 == pytest.approx(source)


def test_runtime_data_range_is_read_from_the_ledger() -> None:
    first, last, provenance = get_load_data_range()
    assert first is not None and first.isoformat() == "2025-11-01"
    assert last is not None and last >= first
    assert provenance["source"] == "raw_hourly_load"
    assert provenance["source_resolution_minutes"] == 60
    assert provenance["target_resolution_minutes"] == 15


@pytest.mark.asyncio
async def test_glm_client_parses_official_tool_call_shape() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tool_choice"] == "auto"
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "get_system_snapshot",
                                "arguments": "{}",
                            },
                        }],
                    }
                }]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GlmClient(
            GlmConfig(api_key="test-secret", model="glm-test", max_retries=0),
            http_client=http_client,
        )
        result = await client.complete(
            [{"role": "user", "content": "状态"}],
            [{"type": "function", "function": {"name": "get_system_snapshot", "parameters": {"type": "object"}}}],
        )
    assert result.tool_calls[0].name == "get_system_snapshot"
    assert result.tool_calls[0].arguments == {}


@pytest.mark.asyncio
async def test_natural_language_failure_is_confirmed_before_replan(tmp_path) -> None:
    clock = TimeEngine(start_time=datetime(2025, 5, 1), time_scale=200)
    engine = SimulationEngine(archive_root=tmp_path, time_engine=clock)
    await engine.start_day(0)
    service = FacilityChatService(
        engine,
        GlmClient(GlmConfig(api_key="")),
    )

    response = await service.chat(
        message="今天 10:30 3号冷机故障，预计 2 小时恢复，然后重新计算",
        actor="测试工程师",
        actor_role="engineer",
        session_id="test",
    )
    assert response["needs_confirmation"] is True
    event = response["events"][0]
    assert event["status"] == "proposed"
    assert event["start_time"].startswith("2025-05-01T10:30")
    assert engine.get_state()["approval_gates"]["forecast_approval"]["status"] == "pending_approval"

    applied = await engine.decide_disturbance(event["event_id"], "apply", "测试工程师")
    assert applied.status == "applied"
    state = engine.get_state()
    assert state["physical_dispatch"]["enabled"] is False
    assert state["approval_gates"]["forecast_approval"]["status"] == "pending_approval"

    await engine.submit_approval("forecast_approval", "approve", actor="测试工程师")
    state = engine.get_state()
    assert state["hvac_summary"]["available_chillers"][42] == 36
    assert state["hvac_summary"]["available_chillers"][49] == 36
    assert state["hvac_summary"]["available_chillers"][50] == 37
