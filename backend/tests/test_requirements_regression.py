from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from agents.chat_service import FacilityChatService
from agents.engine import SimulationEngine
from agents.engine import InvalidControlAction
from algorithms.hvac import optimize_hvac_dispatch
from core.config import SITE_SOLAR_CAPACITY_KW, SITE_STORAGE_POWER_KW
from data.raw_loader import get_load_data_range, load_load_profile
from llm.glm_client import GlmClient, GlmConfig


def test_hourly_ledger_is_the_only_load_source_and_is_disaggregated() -> None:
    first, last, range_provenance = get_load_data_range()
    assert first == date(2025, 11, 1)
    assert last is not None and last >= date(2026, 6, 1)
    assert range_provenance["source"] == "raw_hourly_load"
    assert range_provenance["source_resolution_minutes"] == 60

    profile, provenance = load_load_profile(date(2025, 11, 1))
    assert profile is not None and len(profile) == 96
    assert provenance["source"] == "raw_hourly_load"
    assert provenance["path"].endswith("用电负荷_1h.xlsx")
    assert provenance["source_points"] == 24
    assert provenance["source_resolution_minutes"] == 60
    assert provenance["resolution_minutes"] == 15
    assert provenance["perturbation"]["enabled"] is True
    assert provenance["perturbation"]["max_ratio"] > 0
    assert max(profile) > 50_000

    # Hourly energy remains unchanged after the 15-minute shape disturbance.
    for hourly_kw, block in zip(provenance["hourly_source_kw"],
                                (profile[i:i + 4] for i in range(0, 96, 4))):
        assert sum(block) / 4 == pytest.approx(hourly_kw, rel=1e-8)


@pytest.mark.asyncio
async def test_site_load_scale_dominates_storage_and_solar(tmp_path) -> None:
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    state = engine.get_state()
    load = state["day_ahead"]["load_forecast"]

    assert sum(load) / len(load) > 50_000
    assert SITE_STORAGE_POWER_KW / (sum(load) / len(load)) < 0.35
    assert SITE_SOLAR_CAPACITY_KW / (sum(load) / len(load)) < 0.35
    assert state["data_provenance"]["load"]["source"] == "raw_hourly_load"


@pytest.mark.asyncio
async def test_realtime_feedback_drives_actual_storage_and_hvac_state(tmp_path) -> None:
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.submit_approval("hvac_approval", "approve", actor="tester")

    await engine.submit_control_action(
        system="storage", action="hold", target="power_kw", value=0,
        unit="kW", reason="closed-loop regression", actor="tester",
    )
    await engine.submit_control_action(
        system="hvac", action="set supply", target="supply_temp_c", value=8.0,
        unit="°C", reason="closed-loop regression", actor="tester",
    )
    before_soc = engine.get_state()["storage_soc"]
    await engine.run_step(1)
    state = engine.get_state()
    execution = state["physical_dispatch"]["last_execution"]

    assert execution["command"]["storage_power_kw"] == 0
    assert execution["command"]["hvac_supply_temp_c"] == 8.0
    assert state["storage_soc"] == pytest.approx(before_soc, abs=1e-6)
    assert state["storage_soc"] == pytest.approx(
        execution["feedback"]["measured_storage_soc"], abs=1e-6
    )
    assert state["storage_temp_c"] == pytest.approx(
        execution["feedback"]["measured_storage_temp_c"], abs=1e-2
    )
    assert state["hvac_supply_temp_c"] == pytest.approx(
        execution["feedback"]["measured_hvac_supply_temp_c"], abs=1e-1
    )
    assert len(state["series"]["storage_power"]) == 1
    assert len(state["series"]["temp"]) == 1
    assert len(state["series"]["hvac_power"]) == 1


@pytest.mark.asyncio
async def test_every_approval_report_has_full_report_sections(tmp_path) -> None:
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    forecast = engine.get_state()["approval_gates"]["forecast_approval"]["report"]
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    state = engine.get_state()
    reports = [
        forecast,
        state["approval_gates"]["storage_approval"]["report"],
        state["approval_gates"]["hvac_approval"]["report"],
    ]
    for report in reports:
        detail = report["data"]["report_detail"]
        assert detail["situation_summary"]
        assert len(detail["schedule_table"]) == 96
        assert detail["key_metrics"]
        assert detail["plan_table"]
        assert "risks" in detail


@pytest.mark.asyncio
async def test_engineer_receives_project_feedback_and_facility_can_delegate_agents(tmp_path) -> None:
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    service = FacilityChatService(engine, GlmClient(GlmConfig(api_key="")))

    engineer = await service.chat(
        message="请反馈当前项目情况", actor="工程师", actor_role="engineer", session_id="eng",
    )
    assert "审批" in engineer["message"]["content"]
    assert "数据源" in engineer["message"]["content"]

    facility = await service.chat(
        message="今天下午负荷偏高，请调度相关agent分析并给出处理方案",
        actor="厂务", actor_role="facility", session_id="facility",
    )
    assert facility["delegation"]
    assert {item["agent"] for item in facility["delegation"]} >= {
        "data_agent", "storage_agent", "hvac_agent", "monitor_agent"
    }
    assert "处理方案" in facility["message"]["content"]


@pytest.mark.asyncio
async def test_revision_is_not_mislabeled_as_rejection(tmp_path) -> None:
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    gate = await engine.submit_approval(
        "forecast_approval", "revise", comment="补充数据血缘", actor="tester"
    )
    state = engine.get_state()
    assert gate.status == "pending_approval"
    assert gate.report is not None and gate.report.status == "pending"
    assert state["workflow"]["status"] == "revision_requested"


class ManualClock:
    def __init__(self) -> None:
        self.start = datetime(2025, 11, 1)
        self.step = 0
        self.paused = False

    @property
    def sim_time(self): return self.start + timedelta(minutes=15 * self.step)
    @property
    def current_step(self): return self.step
    @property
    def day_count(self): return 0
    @property
    def is_paused(self): return self.paused
    def reset(self): self.step = 0
    def pause(self): self.paused = True
    def resume(self): self.paused = False
    def tick_info(self):
        return {"sim_time": self.sim_time.isoformat(), "step": self.step, "day": 0,
                "hour": self.step / 4, "progress": self.step / 96,
                "paused": self.paused, "time_scale": 200}


@pytest.mark.asyncio
async def test_skipped_clock_steps_are_replayed_in_order(tmp_path) -> None:
    clock = ManualClock()
    engine = SimulationEngine(archive_root=tmp_path, time_engine=clock)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.submit_approval("hvac_approval", "approve", actor="tester")
    await engine.run_tick()
    clock.step = 5
    await engine.run_tick()
    assert [item["step"] for item in engine.get_state()["series"]["load"]] == list(range(6))


@pytest.mark.asyncio
async def test_unsafe_manual_actions_fail_before_the_next_tick(tmp_path) -> None:
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.submit_approval("hvac_approval", "approve", actor="tester")
    with pytest.raises(InvalidControlAction, match="HVAC"):
        await engine.submit_control_action(system="hvac", action="unsafe", target="power_kw",
                                           value=1_000_000_000, unit="kW", reason="test", actor="tester")
    await engine.run_step(0)
    previous = engine.get_state()["storage_power_kw"]
    opposite_limit = -15_000 if previous >= 0 else 15_000
    with pytest.raises(InvalidControlAction, match="爬坡"):
        await engine.submit_control_action(system="storage", action="unsafe", target="power_kw",
                                           value=opposite_limit, unit="kW", reason="test", actor="tester")


def test_hvac_schedule_responds_to_price_signal() -> None:
    n = 96
    load = [80_000.0] * n
    temp = [32.0] * n
    a = optimize_hvac_dispatch(load, temp, [0.2] * 48 + [1.2] * 48,
                               ["valley"] * 48 + ["peak"] * 48)
    b = optimize_hvac_dispatch(load, temp, [1.2] * 48 + [0.2] * 48,
                               ["peak"] * 48 + ["valley"] * 48)
    assert a["price_response_enabled"] is True
    assert a["supply_temp_c"] != b["supply_temp_c"]
    assert a["power_kw"] != b["power_kw"]


@pytest.mark.asyncio
async def test_combined_accounting_and_overview_strategy_are_effective(tmp_path) -> None:
    engine = SimulationEngine(archive_root=tmp_path)
    await engine.start_day(0)
    await engine.submit_approval("forecast_approval", "approve", actor="tester")
    await engine.submit_approval("storage_approval", "approve", actor="tester")
    await engine.submit_approval("hvac_approval", "approve", actor="tester")
    state = engine.get_state()
    storage_only = state["storage_summary"]["grid_kw"]
    combined = state["storage_summary"]["combined_grid_kw"]
    assert combined != storage_only
    expected_energy_cost = sum(
        grid * price * 0.25 for grid, price in zip(combined, state["day_ahead"]["price"], strict=True)
    )
    assert state["tariff_summary"]["energy_cost_cny"] == pytest.approx(expected_energy_cost, abs=0.02)

    await engine.submit_control_action(system="overview", action="cap", target="demand_cap_kw",
                                       value=30_000, unit="kW", reason="test", actor="tester")
    assert engine.get_state()["active_strategy"] == {"demand_cap_kw": 30_000, "enabled": True}
    await engine.run_step(0)
    command = engine.get_state()["physical_dispatch"]["last_execution"]["command"]
    assert command["storage_power_kw"] >= state["day_ahead"]["storage_plan"][0]
