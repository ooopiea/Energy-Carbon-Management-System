from __future__ import annotations

from datetime import datetime

import pytest

from agents.chat_service import ENGINEER_TOOLS, FACILITY_TOOLS, FacilityChatService
from agents.engine import SimulationEngine
from core.time_engine import TimeEngine
from llm.glm_client import GlmClient, GlmConfig


@pytest.fixture
async def engine(tmp_path):
    clock = TimeEngine(start_time=datetime(2025, 5, 1), time_scale=200)
    eng = SimulationEngine(archive_root=tmp_path, time_engine=clock)
    await eng.start_day(0)
    return eng


def _facility_names() -> set[str]:
    return {t["function"]["name"] for t in FACILITY_TOOLS}


def _engineer_names() -> set[str]:
    return {t["function"]["name"] for t in ENGINEER_TOOLS}


# --- Role separation -------------------------------------------------------

def test_engineer_has_no_execution_tools() -> None:
    eng = _engineer_names()
    exec_tools = {"modify_day_ahead_plan", "submit_realtime_override", "set_demand_cap",
                  "propose_disturbance", "coordinate_energy_agents"}
    assert not (eng & exec_tools), "engineer tools must not include execution tools"
    assert "get_system_snapshot" in eng
    assert "get_system_design" in eng


def test_facility_has_execution_tools() -> None:
    fac = _facility_names()
    assert "modify_day_ahead_plan" in fac
    assert "submit_realtime_override" in fac
    assert "set_demand_cap" in fac
    assert "propose_disturbance" in fac
    assert "coordinate_energy_agents" in fac


@pytest.mark.asyncio
async def test_engineer_chat_returns_no_facility_actions(engine) -> None:
    service = FacilityChatService(engine, GlmClient(GlmConfig(api_key="")))
    resp = await service.chat(
        message="what is the current load?",
        actor="engineer",
        actor_role="engineer",
        session_id="test-eng",
    )
    assert resp["facility_actions"] == []
    assert resp["mission_result"] is None


# --- Phase detection -------------------------------------------------------

def test_phase_is_day_ahead_before_dispatch(engine) -> None:
    assert engine.get_current_phase() == "day_ahead"
    state = engine.get_state()
    assert state["current_phase"] == "day_ahead"


def test_phase_is_realtime_after_dispatch_enabled(engine) -> None:
    # Simulate enabling physical dispatch by setting the internal flag
    engine._dispatch_enabled = True
    assert engine.get_current_phase() == "realtime"


# --- Day-ahead modification preview ----------------------------------------

@pytest.mark.asyncio
async def test_day_ahead_modification_preview_returns_before_after(engine) -> None:
    preview = await engine.preview_day_ahead_modification(
        target_system="storage",
        objective_mode="min_cost",
        parameters={"terminal_soc": 0.5},
    )
    assert "before" in preview
    assert "after" in preview
    assert "diff" in preview
    assert isinstance(preview["before"], dict)
    assert isinstance(preview["after"], dict)


@pytest.mark.asyncio
async def test_day_ahead_propose_and_confirm_creates_facility_action(engine) -> None:
    action = await engine.propose_facility_action(
        action_type="day_ahead_modification",
        target_system="storage",
        parameters={"objective_mode": "min_cost", "terminal_soc": 0.5},
        reasoning="switch to cost minimization",
        confidence=0.9,
        impact_preview={"before": {}, "after": {}, "diff": {}},
        actor="facility",
    )
    assert action.status == "proposed"
    assert action.proposal_id.startswith("fac-")
    state = engine.get_state()
    assert any(fa["proposal_id"] == action.proposal_id for fa in state["facility_actions"])


# --- Realtime override validation ------------------------------------------

@pytest.mark.asyncio
async def test_realtime_override_validates_power_limit(engine) -> None:
    engine._dispatch_enabled = True
    preview = await engine.preview_realtime_override(
        target_system="storage",
        setpoints={"power_kw": 20000},  # exceeds 15 MW limit
    )
    validations = preview.get("validation", [])
    power_val = [v for v in validations if v.get("field") == "power_kw"]
    assert len(power_val) == 1
    assert power_val[0]["status"] == "exceeded"


@pytest.mark.asyncio
async def test_realtime_override_validates_supply_temp(engine) -> None:
    engine._dispatch_enabled = True
    preview = await engine.preview_realtime_override(
        target_system="hvac",
        setpoints={"supply_temp_c": 3.0},  # below 5 C minimum
    )
    validations = preview.get("validation", [])
    temp_val = [v for v in validations if v.get("field") == "supply_temp_c"]
    assert len(temp_val) == 1
    assert temp_val[0]["status"] == "exceeded"


@pytest.mark.asyncio
async def test_realtime_override_soc_projection(engine) -> None:
    engine._dispatch_enabled = True
    preview = await engine.preview_realtime_override(
        target_system="storage",
        setpoints={"power_kw": 5000},
    )
    assert "soc_projection" in preview
    projection = preview["soc_projection"]
    assert len(projection) == 4
    assert all(0.1 <= p["soc"] <= 0.9 for p in projection)


# --- Demand cap immediate apply --------------------------------------------

@pytest.mark.asyncio
async def test_demand_cap_applies_immediately(engine) -> None:
    action = await engine.propose_facility_action(
        action_type="demand_cap",
        target_system="overview",
        parameters={"demand_cap_kw": 50000},
        reasoning="limit peak demand",
        confidence=0.9,
        impact_preview={},
        actor="facility",
    )
    confirmed = await engine.confirm_facility_action(action.proposal_id, "facility")
    assert confirmed.status == "applied"
    assert engine._demand_cap_kw == 50000.0


@pytest.mark.asyncio
async def test_cancel_facility_action(engine) -> None:
    action = await engine.propose_facility_action(
        action_type="demand_cap",
        target_system="overview",
        parameters={"demand_cap_kw": 60000},
        reasoning="test cancel",
        confidence=0.5,
        impact_preview={},
        actor="facility",
    )
    cancelled = await engine.cancel_facility_action(action.proposal_id, "facility")
    assert cancelled.status == "cancelled"


# --- Post-execution monitoring ---------------------------------------------

@pytest.mark.asyncio
async def test_post_execution_monitoring_tracks_applied_action(engine) -> None:
    action = await engine.propose_facility_action(
        action_type="demand_cap",
        target_system="overview",
        parameters={"demand_cap_kw": 50000},
        reasoning="test monitoring",
        confidence=0.9,
        impact_preview={"after": {"daily_cost_cny": 100000}},
        actor="facility",
    )
    await engine.confirm_facility_action(action.proposal_id, "facility")
    assert action.monitor_steps_remaining == 24
    # Simulate a tick with measurements
    engine._check_facility_action_monitoring({"storage_soc": 0.5})
    assert action.monitor_steps_remaining == 23
    assert action.post_execution is not None


# --- Structured parameter accuracy -----------------------------------------

def test_modify_day_ahead_tool_schema_is_correct() -> None:
    tool = next(t for t in FACILITY_TOOLS if t["function"]["name"] == "modify_day_ahead_plan")
    props = tool["function"]["parameters"]["properties"]
    assert "target_system" in props
    assert "objective_mode" in props
    assert props["objective_mode"]["enum"] == ["min_cost", "min_carbon", "weighted"]
    assert "parameters" in props
    inner_props = props["parameters"]["properties"]
    assert "terminal_soc" in inner_props
    assert "power_limit_windows" in inner_props
    assert "available_chillers_override" in inner_props


def test_realtime_override_tool_schema_is_correct() -> None:
    tool = next(t for t in FACILITY_TOOLS if t["function"]["name"] == "submit_realtime_override")
    props = tool["function"]["parameters"]["properties"]
    assert "target_system" in props
    assert props["target_system"]["enum"] == ["storage", "hvac"]
    assert "setpoints" in props
    setpoint_props = props["setpoints"]["properties"]
    assert "power_kw" in setpoint_props
    assert "supply_temp_c" in setpoint_props


def test_demand_cap_tool_schema_is_correct() -> None:
    tool = next(t for t in FACILITY_TOOLS if t["function"]["name"] == "set_demand_cap")
    props = tool["function"]["parameters"]["properties"]
    assert "demand_cap_kw" in props
    assert "objective_mode" in props
    assert "reasoning" in props
    required = tool["function"]["parameters"]["required"]
    assert "demand_cap_kw" in required
    assert "reasoning" in required
