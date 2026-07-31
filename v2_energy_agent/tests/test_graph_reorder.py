"""Tests for the V2.2 graph reorder: carbon factors before optimize, archive at end, storage_approval."""

from __future__ import annotations

from datetime import date

import pytest

from energy_agent_v2.runner import create_app_context, make_initial_state, run_dispatch


@pytest.mark.asyncio
async def test_graph_node_sequence_approve():
    """Approve flow: verify node order matches the reordered graph."""
    ctx = create_app_context()
    state = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
    result = await run_dispatch(ctx, state, approval_decision="approve")

    node_ids = [e["node_id"] for e in result["events"]]
    assert "compute_carbon_factors" in node_ids, "carbon_factors node missing"
    assert "compute_carbon_dispatch" in node_ids, "carbon_dispatch node missing"
    assert "storage_approval" in node_ids, "storage_approval node missing"
    assert "human_approval" not in node_ids, "old human_approval should be gone"
    assert "compute_carbon" not in node_ids, "old compute_carbon should be split"

    cf_idx = node_ids.index("compute_carbon_factors")
    opt_idx = node_ids.index("optimize_storage")
    cd_idx = node_ids.index("compute_carbon_dispatch")
    assert cf_idx < opt_idx, "carbon_factors must run BEFORE optimize"
    assert opt_idx < cd_idx, "carbon_dispatch must run AFTER optimize"

    freeze_idx = node_ids.index("freeze_plan")
    arch_idx = node_ids.index("archive_data")
    assert freeze_idx < arch_idx, "archive must run AFTER freeze_plan"

    assert result["run_status"] == "approved"


@pytest.mark.asyncio
async def test_carbon_factors_in_state():
    """Carbon factors C(tau)/Cr(tau) are computed and stored in state."""
    ctx = create_app_context()
    state = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
    result = await run_dispatch(ctx, state, approval_decision="approve")

    cf = result.get("carbon_factors")
    assert cf is not None, "carbon_factors should be in state"
    assert len(cf["direct_factor_c_kg_per_kwh"]) == 96
    assert len(cf["responsibility_factor_cr_kg_per_kwh"]) == 96


@pytest.mark.asyncio
async def test_revision_loop_preserves_carbon_factors():
    """Revision re-optimizes but does NOT recompute carbon factors."""
    ctx = create_app_context()
    state = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
    result = await run_dispatch(
        ctx, state,
        approval_decision="revise",
        revision={"terminal_soc_min_ratio": 0.25},
    )
    node_ids = [e["node_id"] for e in result["events"]]
    cf_count = node_ids.count("compute_carbon_factors")
    opt_count = node_ids.count("optimize_storage")
    assert cf_count == 1, "carbon_factors should run only once"
    assert opt_count >= 2, "optimize should run at least twice (V1 + V2)"


def test_storage_approval_agent_interpret():
    """StorageApprovalAgent.interpret_command returns structured result."""
    from energy_agent_v2.llm.client import MockLLMClient
    from energy_agent_v2.llm.storage_approval import StorageApprovalAgent

    agent = StorageApprovalAgent(client=MockLLMClient())
    storage_stub = {
        "plan_id": "test", "plan_version": 1, "site_id": "huanghua",
        "target_date": "2026-07-27", "start_at": "2026-07-27T00:00:00",
        "time_step_minutes": 15, "point_count": 96,
        "timestamps": ["2026-07-27T00:00:00"], "load_forecast_kw": [50000],
        "battery_power_kw": [2000], "soc_ratio": [0.1], "cell_temperature_c": [25],
        "baseline_grid_import_power_kw": [50000], "grid_import_power_kw": [48000],
        "electricity_price_cny_per_kwh": [0.3],
        "baseline_energy_cost_cny": 180000, "optimized_energy_cost_cny": 172800,
        "energy_cost_saving_cny": 7200, "baseline_peak_demand_kw": 50000,
        "optimized_peak_demand_kw": 48000, "peak_reduction_kw": 2000,
        "terminal_soc_ratio": 0.1, "max_cell_temperature_c": 25,
        "constraint_check": {"passed": True, "violations": []},
        "solver_status": "Optimal", "solve_duration_ms": 50,
        "algorithm_version": "test", "agent_version": "test",
        "data_version": "test", "objective": "min_cost",
    }
    result = agent.interpret_command("SOC太低了，末端别低于30%", storage_stub)
    assert "decision" in result
    assert "physical_interpretation" in result
    assert "should_reoptimize" in result
    assert "revision" in result
    assert isinstance(result["confidence"], float)
    assert result["llm_model"] == "mock"


@pytest.mark.asyncio
async def test_agent_api_endpoints_exist():
    """Verify the server module imports with the new agent endpoints."""
    import server

    routes = [r.path for r in server.app.routes if hasattr(r, "path")]
    assert "/api/agents/ingest" in routes
    assert "/api/agents/approval/interpret" in routes
    assert "/api/agents/archive" in routes
    assert "/api/agents/anomaly/analyze" in routes
    assert "/api/agents/distillation/review" in routes
    assert "/api/runs" in routes
    assert "/api/daily-stream/{thread_id}" in routes


@pytest.mark.asyncio
async def test_all_agent_api_endpoints_exist():
    """All 5 agents have independent API endpoints."""
    import server

    routes = [r.path for r in server.app.routes if hasattr(r, "path")]
    expected = [
        "/api/agents/ingest",
        "/api/agents/approval/interpret",
        "/api/agents/archive",
        "/api/agents/anomaly/analyze",
        "/api/agents/distillation/review",
    ]
    for path in expected:
        assert path in routes, f"Missing agent endpoint: {path}"


def test_all_agents_instantiated():
    """create_app_context instantiates all 5 LLM agents when LLM client is available."""
    from energy_agent_v2.llm.client import MockLLMClient
    from energy_agent_v2.llm.parse_revision import RevisionParser

    from energy_agent_v2.runner import create_app_context

    parser = RevisionParser(client=MockLLMClient())
    ctx = create_app_context(revision_parser=parser)
    assert ctx.ingest_agent is not None, "ingest_agent must be instantiated"
    assert ctx.archive_agent is not None, "archive_agent must be instantiated"
    assert ctx.approval_agent is not None, "approval_agent must be instantiated"
    assert ctx.anomaly_agent is not None, "anomaly_agent must be instantiated"
    assert ctx.distillation_agent is not None, "distillation_agent must be instantiated"


def test_anomaly_agent_standalone():
    """AnomalyMonitorAgent can be called independently via its own analyze() method."""
    from datetime import UTC, datetime

    from energy_agent_v2.contracts import AnomalySignal
    from energy_agent_v2.llm.anomaly_monitor import AnomalyMonitorAgent
    from energy_agent_v2.llm.client import MockLLMClient

    agent = AnomalyMonitorAgent(client=MockLLMClient())
    signals = [
        AnomalySignal(
            signal_id="sig-1",
            source="battery_bms",
            severity="critical",
            timestamp=datetime.now(UTC),
            description="Cell temperature exceeded 45C",
        ),
    ]
    alert = agent.analyze(signals)
    assert alert.alert_level in ("info", "warning", "critical")
    assert alert.llm_model == "mock"


def test_distillation_agent_standalone():
    """DistillationReviewAgent can be called independently via its own review() method."""
    from energy_agent_v2.llm.client import MockLLMClient
    from energy_agent_v2.llm.distillation_review import DistillationReviewAgent

    agent = DistillationReviewAgent(client=MockLLMClient())
    insight = agent.review(records=[], review_period="2026-07")
    assert insight.summary
    assert insight.llm_model == "mock"


def test_deterministic_tools_not_agents():
    """Tariff and carbon are deterministic algorithms, not LLM agents."""
    from energy_agent_v2.algorithms.carbon_accounting import CarbonAccountant
    from energy_agent_v2.algorithms.tariff import TariffCalculator
    from energy_agent_v2.llm.base import BaseLLMAgent

    assert not issubclass(TariffCalculator, BaseLLMAgent), "TariffCalculator must NOT be an LLM agent"
    assert not issubclass(CarbonAccountant, BaseLLMAgent), "CarbonAccountant must NOT be an LLM agent"
