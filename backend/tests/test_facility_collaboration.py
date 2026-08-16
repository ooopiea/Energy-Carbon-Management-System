from __future__ import annotations

from datetime import datetime

import pytest

from agents.chat_service import ENGINEER_TOOLS, FACILITY_TOOLS, FacilityChatService
from agents.engine import SimulationEngine
from core.time_engine import TimeEngine
from llm.glm_client import GlmClient, GlmConfig
from llm.glm_client import GlmMessage, GlmToolCall


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


# --- GLM tool-calling path (previously crashed with NameError) ----------


class _MockGlmClient:
    """Simulates a configured GLM client returning tool_calls then a
    final text reply, exercising the GLM tool-execution loop without
    needing network access."""

    def __init__(self, rounds: list[GlmMessage]):
        self._rounds = list(rounds)
        self._idx = 0

    @property
    def configured(self) -> bool:
        return True

    @property
    def config(self):
        class _Cfg:
            model = "mock-glm"
        return _Cfg()

    def status(self) -> dict:
        return {"provider": "mock", "configured": True, "model": "mock-glm"}

    async def complete(self, messages, tools=None, *, max_tokens=1200):
        if self._idx < len(self._rounds):
            msg = self._rounds[self._idx]
            self._idx += 1
            return msg
        return GlmMessage(content="(no further action)")


@pytest.mark.asyncio
async def test_glm_modify_day_ahead_tool_path(engine) -> None:
    """GLM calls modify_day_ahead_plan -> must create FacilityAction
    without NameError.  This was the primary bug: variables were used
    without being extracted from args."""
    mock = _MockGlmClient([
        GlmMessage(content="", tool_calls=[GlmToolCall(
            id="call-1", name="modify_day_ahead_plan",
            arguments={
                "target_system": "storage",
                "objective_mode": "min_cost",
                "reasoning": "省钱优先",
                "confidence": 0.9,
            },
        )]),
        GlmMessage(content="储能优化目标已改为省钱优先，已生成审批提案。"),
    ])
    service = FacilityChatService(engine, mock)
    resp = await service.chat(
        message="把储能目标改成省钱优先",
        actor="厂务", actor_role="facility", session_id="glm-da",
    )
    assert resp["message"]["mode"] == "glm"
    assert len(resp["facility_actions"]) == 1
    fa = resp["facility_actions"][0]
    assert fa["action_type"] == "day_ahead_modification"
    assert fa["status"] == "proposed"
    assert fa["parameters"].get("objective_mode") == "min_cost"


@pytest.mark.asyncio
async def test_glm_modify_day_ahead_closed_loop(engine) -> None:
    """Full closed loop via GLM path: tool call -> proposal -> confirm
    -> engine objective_mode changes."""
    mock = _MockGlmClient([
        GlmMessage(content="", tool_calls=[GlmToolCall(
            id="call-1", name="modify_day_ahead_plan",
            arguments={
                "target_system": "storage",
                "objective_mode": "min_carbon",
                "parameters": {"terminal_soc": 0.3},
                "reasoning": "低碳优先",
                "confidence": 0.88,
            },
        )]),
        GlmMessage(content="储能优化目标已改为低碳优先。"),
    ])
    service = FacilityChatService(engine, mock)
    resp = await service.chat(
        message="储能改成低碳优先，末端SOC到30%",
        actor="厂务", actor_role="facility", session_id="glm-loop",
    )
    assert len(resp["facility_actions"]) == 1
    pid = resp["facility_actions"][0]["proposal_id"]
    action = await engine.confirm_facility_action(pid, "厂务")
    assert action.status == "applied"
    # D+1 modifications change the pending plan's objective_mode;
    # current-day modifications change engine._objective_mode.
    engine_mode = getattr(engine, "_objective_mode", "weighted")
    pending = getattr(engine, "_pending_day_plan", None)
    plan_mode = getattr(pending, "objective_mode", None) if pending else None
    assert engine_mode == "min_carbon" or plan_mode == "min_carbon"


@pytest.mark.asyncio
async def test_glm_demand_cap_closed_loop(engine) -> None:
    """GLM calls set_demand_cap -> proposal -> confirm -> cap applied."""
    mock = _MockGlmClient([
        GlmMessage(content="", tool_calls=[GlmToolCall(
            id="call-1", name="set_demand_cap",
            arguments={
                "demand_cap_kw": 40000,
                "objective_mode": "weighted",
                "reasoning": "限制最大需量",
                "confidence": 0.85,
            },
        )]),
        GlmMessage(content="已生成需量上限提案。"),
    ])
    service = FacilityChatService(engine, mock)
    resp = await service.chat(
        message="设置需量上限40000kW，目标加权",
        actor="厂务", actor_role="facility", session_id="glm-cap",
    )
    assert len(resp["facility_actions"]) == 1
    pid = resp["facility_actions"][0]["proposal_id"]
    action = await engine.confirm_facility_action(pid, "厂务")
    assert action.status == "applied"
    assert engine._demand_cap_kw == 40000.0
    assert engine._objective_mode == "weighted"


@pytest.mark.asyncio
async def test_glm_realtime_override_closed_loop(tmp_path) -> None:
    """GLM calls submit_realtime_override in realtime phase -> proposal
    -> confirm -> control action submitted."""
    clock = TimeEngine(start_time=datetime(2025, 5, 1), time_scale=200)
    eng = SimulationEngine(archive_root=tmp_path, time_engine=clock)
    await eng.start_day(0)
    await eng.start_day(1)  # cross-day: enables dispatch + approves gates
    mock = _MockGlmClient([
        GlmMessage(content="", tool_calls=[GlmToolCall(
            id="call-1", name="submit_realtime_override",
            arguments={
                "target_system": "storage",
                "setpoints": {"power_kw": -5000},
                "reasoning": "紧急充电",
                "confidence": 0.9,
            },
        )]),
        GlmMessage(content="已生成实时覆盖提案。"),
    ])
    service = FacilityChatService(eng, mock)
    resp = await service.chat(
        message="储能紧急充电5000kW",
        actor="厂务", actor_role="facility", session_id="glm-rt",
    )
    assert len(resp["facility_actions"]) == 1
    fa = resp["facility_actions"][0]
    assert fa["action_type"] == "realtime_override"
    pid = fa["proposal_id"]
    action = await eng.confirm_facility_action(pid, "厂务")
    assert action.status == "applied"


@pytest.mark.asyncio
async def test_glm_tool_error_degrades_gracefully(engine) -> None:
    """When a tool execution raises an unexpected error, the chat loop
    must not crash; the error is fed back to GLM as a tool result."""
    mock = _MockGlmClient([
        GlmMessage(content="", tool_calls=[GlmToolCall(
            id="call-1", name="get_report_detail",
            arguments={"report_id": "nonexistent"},
        )]),
        GlmMessage(content="报告不存在，请确认ID。"),
    ])
    service = FacilityChatService(engine, mock)
    resp = await service.chat(
        message="看一下报告nonexistent",
        actor="厂务", actor_role="facility", session_id="glm-err",
    )
    assert resp["message"]["mode"] == "glm"
    assert len(resp["message"]["content"]) > 0


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


# --- Cross-day pending plan -----------------------------------------------

@pytest.mark.asyncio
async def test_next_day_plan_generation(engine) -> None:
   """start_day(0) auto-generates a D+1 pending plan."""
   plan = engine.get_pending_day_plan()
   assert plan is not None
   assert plan["target_day"] == 1
   assert plan["status"] == "approved"
   assert plan["metrics"]


@pytest.mark.asyncio
async def test_modify_next_day_plan(engine) -> None:
   """Modify D+1 params -> preview -> apply -> confirm -> plan stays approved."""
   preview = engine.preview_next_day_modification(
       target_system="storage",
       objective_mode="min_cost",
       parameters={"terminal_soc": 0.5},
   )
   assert "before" in preview
   assert "after" in preview

   action = await engine.apply_next_day_modification(
       target_system="storage",
       objective_mode="min_cost",
       parameters={"terminal_soc": 0.5},
       reasoning="switch to cost minimization",
       actor="facility",
   )
   assert action.target_day == 1
   assert action.status == "proposed"

   confirmed = await engine.confirm_next_day_modification(action.proposal_id, "facility")
   assert confirmed.status == "applied"

   plan = engine.get_pending_day_plan()
   assert plan is not None
   assert plan["status"] == "approved"
   assert plan["objective_mode"] == "min_cost"


@pytest.mark.asyncio
async def test_revise_facility_action(engine) -> None:
   """Revise: cancel old proposal + create new with fresh preview."""
   action = await engine.apply_next_day_modification(
       target_system="storage",
       objective_mode="weighted",
       parameters={"terminal_soc": 0.5},
       reasoning="initial proposal",
       actor="facility",
   )

   result = await engine.revise_facility_action(
       action.proposal_id,
       {"objective_mode": "min_cost"},
       "facility",
   )
   new_id = result["proposal_id"]
   assert new_id != action.proposal_id
   assert result["action"]["status"] == "proposed"
   assert action.status == "cancelled"
   assert "before" in result["preview"]
   assert "after" in result["preview"]


@pytest.mark.asyncio
async def test_multiple_revisions(engine) -> None:
   """Consecutive revisions update parameters and preview each time."""
   action = await engine.apply_next_day_modification(
       target_system="storage",
       objective_mode="weighted",
       parameters={"terminal_soc": 0.5},
       reasoning="initial",
       actor="facility",
   )

   prev_id = action.proposal_id
   for mode in ("min_cost", "min_carbon", "weighted"):
       result = await engine.revise_facility_action(
           prev_id,
           {"objective_mode": mode},
           "facility",
       )
       assert result["proposal_id"] != prev_id
       assert result["action"]["status"] == "proposed"
       prev_id = result["proposal_id"]


# --- Realtime override on day 1 (cross-day dispatch activation) ------------

@pytest.mark.asyncio
async def test_realtime_override_available_day1(tmp_path) -> None:
   """Cross-day model: day 1 dispatch auto-activated, realtime override works."""
   clock = TimeEngine(start_time=datetime(2025, 5, 1), time_scale=200)
   engine = SimulationEngine(archive_root=tmp_path, time_engine=clock)
   await engine.start_day(0)
   await engine.start_day(1)

   state = engine.get_state()
   assert state["physical_dispatch"]["enabled"] is True

   preview = await engine.preview_realtime_override(
       target_system="storage",
       setpoints={"power_kw": 3000},
   )
   assert "validation" in preview
   assert "soc_projection" in preview


# --- Regression: fallback tuple unpacking and closed-loop ---


@pytest.mark.asyncio
async def test_fallback_no_crash_on_vague_facility_message(engine) -> None:
   """_fallback() must return 4 values even when nothing matches."""
   service = FacilityChatService(engine, GlmClient(GlmConfig(api_key="")))
   resp = await service.chat(
       message="今天天气不错",
       actor="厂务", actor_role="facility", session_id="reg-vague",
   )
   assert resp["message"]["mode"] == "rule_fallback"
   assert len(resp["message"]["content"]) > 0


@pytest.mark.asyncio
async def test_facility_chat_always_produces_action(engine) -> None:
   """A day-ahead modification request must always produce a
   FacilityAction proposal, whether or not a D+1 plan exists."""
   service = FacilityChatService(engine, GlmClient(GlmConfig(api_key="")))
   resp = await service.chat(
       message="把储能目标改成省钱优先",
       actor="厂务", actor_role="facility", session_id="reg-action",
   )
   assert len(resp["facility_actions"]) == 1
   fa = resp["facility_actions"][0]
   assert fa["action_type"] == "day_ahead_modification"
   assert fa["status"] == "proposed"
   assert fa["parameters"].get("objective_mode") == "min_cost"


@pytest.mark.asyncio
async def test_closed_loop_confirm_affects_plan(engine) -> None:
   """Confirm -> plan objective_mode or engine objective_mode changes."""
   service = FacilityChatService(engine, GlmClient(GlmConfig(api_key="")))
   resp = await service.chat(
       message="把储能目标改成省钱优先",
       actor="厂务", actor_role="facility", session_id="closed-loop",
   )
   assert len(resp["facility_actions"]) == 1
   pid = resp["facility_actions"][0]["proposal_id"]
   action = await engine.confirm_facility_action(pid, "厂务")
   assert action.status == "applied"
   # Either the pending D+1 plan or the engine objective changed.
   engine_mode = getattr(engine, "_objective_mode", "weighted")
   pending_plan = getattr(engine, "_pending_day_plan", None)
   plan_mode = getattr(pending_plan, "objective_mode", None) if pending_plan else None
   assert engine_mode == "min_cost" or plan_mode == "min_cost"


@pytest.mark.asyncio
async def test_closed_loop_demand_cap(engine) -> None:
   """Demand cap closed loop: chat -> proposal -> confirm -> cap set."""
   service = FacilityChatService(engine, GlmClient(GlmConfig(api_key="")))
   resp = await service.chat(
       message="设置需求上限50000 kW",
       actor="厂务", actor_role="facility", session_id="closed-loop-cap",
   )
   assert len(resp["facility_actions"]) == 1
   pid = resp["facility_actions"][0]["proposal_id"]
   action = await engine.confirm_facility_action(pid, "厂务")
   assert action.status == "applied"
   assert engine._demand_cap_kw == 50000.0
