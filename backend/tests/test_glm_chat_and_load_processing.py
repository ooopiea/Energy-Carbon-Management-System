from __future__ import annotations

import json
from datetime import datetime

import httpx
from typing import Any
import pytest

from agents.chat_service import FacilityChatService
from agents.engine import SimulationEngine
from core.time_engine import TimeEngine
from data.raw_loader import get_load_data_range, process_load_to_15min
from llm.glm_client import GlmClient, GlmConfig, GlmMessage, GlmToolCall


class _FakeMultiStepGlm:
    """Mock GLM client that returns tool_calls across multiple rounds."""

    def __init__(self):
        self.configured = True
        self.config = GlmConfig()
        self.calls = 0

    async def complete(self, messages, tools=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return GlmMessage(
                content="",
                reasoning="First I need to check the system snapshot.",
                tool_calls=[GlmToolCall(id="call-1", name="get_system_snapshot", arguments={})],
            )
        if self.calls == 2:
            return GlmMessage(
                content="",
                reasoning="Now I will check available chillers.",
                tool_calls=[GlmToolCall(id="call-2", name="get_system_design", arguments={"topic": "storage"})],
            )
        return GlmMessage(
            content="协调完成：储能 SOC 90%，建议继续放电。",
            reasoning="Both tools executed successfully.",
            tool_calls=[],
        )

    def status(self):
        return {"configured": True, "model": "fake-glm", "thinking": "enabled"}


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


@pytest.mark.asyncio
async def test_glm_extracts_reasoning_content() -> None:
    """GLM thinking=enabled returns reasoning_content; GlmMessage.reasoning should capture it."""
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["thinking"] = body.get("thinking")
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "建议午间储能放电。",
                    "reasoning_content": "负荷峰值在 12:00-14:00，对应峰段电价最高，应在此前充满并放电。",
                    "tool_calls": [],
                }
            }]
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GlmClient(
            GlmConfig(api_key="test", model="glm-test", max_retries=0),
            http_client=http_client,
        )
        msg = await client.complete(
            [{"role": "user", "content": "分析"}],
            [],
        )
    assert msg.reasoning == "负荷峰值在 12:00-14:00，对应峰段电价最高，应在此前充满并放电。"
    assert msg.content == "建议午间储能放电。"
    assert msg.tool_calls == []


@pytest.mark.asyncio
async def test_multi_step_tool_loop_executes_all_rounds(tmp_path) -> None:
    """chat() must loop: round 1 calls a tool, round 2 calls another, round 3 returns final text."""
    clock = TimeEngine(start_time=datetime(2025, 11, 1), time_scale=200)
    engine = SimulationEngine(archive_root=tmp_path, time_engine=clock)
    await engine.start_day(0)
    fake_glm = _FakeMultiStepGlm()
    service = FacilityChatService(engine, fake_glm)
    resp = await service.chat(
        message="帮我做全面检查",
        actor="facility",
        actor_role="facility",
        session_id="multi-step-test",
    )
    msg = resp["message"]
    # Two tool calls should have been executed across two rounds.
    assert len(msg["tool_trace"]) == 2
    names = [t["name"] for t in msg["tool_trace"]]
    assert names == ["get_system_snapshot", "get_system_design"]
    # Three GLM completions: round1 (tool), round2 (tool), round3 (final).
    assert fake_glm.calls == 3
    # Final content comes from the last non-tool-call response.
    assert "协调完成" in msg["content"]
    # Reasoning from the last round is preserved.
    assert msg["reasoning"] == "Both tools executed successfully."
