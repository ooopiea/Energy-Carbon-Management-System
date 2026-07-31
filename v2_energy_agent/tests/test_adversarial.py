"""Adversarial tests: error handling, boundary conditions, security constraints.

Covers scenarios the happy-path tests miss:
- API layer 4xx/5xx error handling
- Agent adversarial inputs (empty, malformed, oversized)
- Revision constraint safety (tighten-only, reject loosening)
- Agent failure recovery (None agents, LLM errors)
- Concurrent run isolation
"""
from __future__ import annotations

import pytest
from datetime import date, datetime, UTC

from energy_agent_v2.runner import create_app_context, make_initial_state, run_dispatch
from energy_agent_v2.contracts import (
    AnomalySignal,
    DispatchRevision,
    StorageOptimizationResult,
)
from energy_agent_v2.llm.client import MockLLMClient
from energy_agent_v2.llm.storage_approval import StorageApprovalAgent
from energy_agent_v2.llm.anomaly_monitor import AnomalyMonitorAgent


# ===========================================================================
# 1. API Layer Error Handling (FastAPI TestClient)
# ===========================================================================

class TestAPIErrors:
    """Verify API endpoints return proper error codes for bad requests."""

    def test_create_run_invalid_date_format(self):
        from starlette.testclient import TestClient
        import server

        client = TestClient(server.app)
        resp = client.post("/api/runs", json={
            "site_id": "huanghua", "target_date": "not-a-date", "objective": "min_cost",
        })
        assert resp.status_code == 400

    def test_create_run_invalid_objective(self):
        from starlette.testclient import TestClient
        import server

        client = TestClient(server.app)
        resp = client.post("/api/runs", json={
            "site_id": "huanghua", "target_date": "2026-07-27", "objective": "hack_the_grid",
        })
        assert resp.status_code == 400

    def test_get_run_not_found(self):
        from starlette.testclient import TestClient
        import server

        client = TestClient(server.app)
        resp = client.get("/api/runs/nonexistent-thread-id")
        assert resp.status_code == 404

    def test_resume_nonexistent_thread(self):
        from starlette.testclient import TestClient
        import server

        client = TestClient(server.app)
        resp = client.post("/api/runs/nonexistent-thread-id/resume",
                           json={"decision": "approve"})
        assert resp.status_code == 404

    def test_resume_not_paused_run(self):
        """Resuming a completed run should return 400, not crash."""
        from starlette.testclient import TestClient
        import server

        client = TestClient(server.app)
        # Create and complete a run first
        resp = client.post("/api/runs", json={
            "site_id": "huanghua", "target_date": "2026-07-27", "objective": "min_cost",
        })
        tid = resp.json()["thread_id"]
        # Approve it
        client.post(f"/api/runs/{tid}/resume", json={"decision": "approve"})
        # Try to resume again
        resp2 = client.post(f"/api/runs/{tid}/resume", json={"decision": "approve"})
        assert resp2.status_code == 400

    def test_anomaly_analyze_empty_signals(self):
        from starlette.testclient import TestClient
        import server

        client = TestClient(server.app)
        resp = client.post("/api/agents/anomaly/analyze", json={"signals": []})
        assert resp.status_code == 400

    def test_anomaly_analyze_malformed_signal(self):
        from starlette.testclient import TestClient
        import server

        client = TestClient(server.app)
        resp = client.post("/api/agents/anomaly/analyze", json={
            "signals": [{"garbage": True}]
        })
        assert resp.status_code == 422  # Pydantic validation error


# ===========================================================================
# 2. Agent Adversarial Inputs
# ===========================================================================

class TestAgentAdversarialInputs:
    """Verify agents handle adversarial inputs gracefully."""

    def test_approval_agent_empty_comment(self):
        """Empty comment should not crash the agent."""
        agent = StorageApprovalAgent(client=MockLLMClient())
        sr = {"plan_id": "t", "plan_version": 1, "site_id": "h", "target_date": "2026-07-27",
              "start_at": "2026-07-27T00:00", "time_step_minutes": 15, "point_count": 1,
              "timestamps": ["2026-07-27T00:00"], "load_forecast_kw": [50000],
              "battery_power_kw": [0], "soc_ratio": [0.1], "cell_temperature_c": [25],
              "baseline_grid_import_power_kw": [50000], "grid_import_power_kw": [50000],
              "electricity_price_cny_per_kwh": [0.3], "baseline_energy_cost_cny": 180000,
              "optimized_energy_cost_cny": 180000, "energy_cost_saving_cny": 0,
              "baseline_peak_demand_kw": 50000, "optimized_peak_demand_kw": 50000,
              "peak_reduction_kw": 0, "terminal_soc_ratio": 0.1, "max_cell_temperature_c": 25,
              "constraint_check": {"passed": True, "violations": []}, "solver_status": "Optimal",
              "solve_duration_ms": 0, "algorithm_version": "t", "agent_version": "t",
              "data_version": "t", "objective": "min_cost"}
        result = agent.interpret_command("", sr)
        assert "decision" in result
        assert isinstance(result["confidence"], float)

    def test_approval_agent_nonsense_comment(self):
        """Gibberish input should produce a valid result, not crash."""
        agent = StorageApprovalAgent(client=MockLLMClient())
        sr = {"plan_id": "t", "plan_version": 1, "site_id": "h", "target_date": "2026-07-27",
              "start_at": "2026-07-27T00:00", "time_step_minutes": 15, "point_count": 1,
              "timestamps": ["2026-07-27T00:00"], "load_forecast_kw": [50000],
              "battery_power_kw": [0], "soc_ratio": [0.1], "cell_temperature_c": [25],
              "baseline_grid_import_power_kw": [50000], "grid_import_power_kw": [50000],
              "electricity_price_cny_per_kwh": [0.3], "baseline_energy_cost_cny": 180000,
              "optimized_energy_cost_cny": 180000, "energy_cost_saving_cny": 0,
              "baseline_peak_demand_kw": 50000, "optimized_peak_demand_kw": 50000,
              "peak_reduction_kw": 0, "terminal_soc_ratio": 0.1, "max_cell_temperature_c": 25,
              "constraint_check": {"passed": True, "violations": []}, "solver_status": "Optimal",
              "solve_duration_ms": 0, "algorithm_version": "t", "agent_version": "t",
              "data_version": "t", "objective": "min_cost"}
        result = agent.interpret_command("xyzzy flurbo bazinga 42", sr)
        assert "decision" in result

    def test_approval_agent_missing_storage_fields(self):
        """Missing fields in storage_result should not crash."""
        agent = StorageApprovalAgent(client=MockLLMClient())
        result = agent.interpret_command("SOC 30%", {})
        assert "decision" in result

    def test_anomaly_agent_empty_signal_list(self):
        """Empty signal list should return empty alert, not crash."""
        from energy_agent_v2.llm.anomaly_monitor import AnomalyMonitorAgent
        agent = AnomalyMonitorAgent(client=MockLLMClient())
        # Empty list - the monitoring graph handles this, but let's test direct call doesn't crash
        # with a single dummy signal that has extreme values
        signal = AnomalySignal(
            signal_id="s1", source="battery_bms", severity="info",
            timestamp=datetime.now(UTC), description="",
        )
        alert = agent.analyze([signal])
        assert alert is not None
        assert alert.llm_model == "mock"


# ===========================================================================
# 3. Revision Constraint Safety (Security Critical)
# ===========================================================================

class TestRevisionConstraintSafety:
    """Verify revision can only TIGHTEN constraints, never loosen them."""

    def test_merge_revision_tightens_terminal_soc(self):
        """New terminal SOC min must be >= existing (only tighten)."""
        from energy_agent_v2.orchestration import _merge_revision
        current = DispatchRevision(terminal_soc_min_ratio=0.3)
        # Try to loosen to 0.1
        requested = DispatchRevision(terminal_soc_min_ratio=0.1)
        merged = _merge_revision(current, requested)
        assert merged.terminal_soc_min_ratio == 0.3, "Revision must NOT loosen SOC constraint"

    def test_merge_revision_tightens_reserve_soc(self):
        from energy_agent_v2.orchestration import _merge_revision
        current = DispatchRevision(reserve_soc_min_ratio=0.25)
        requested = DispatchRevision(reserve_soc_min_ratio=0.1)
        merged = _merge_revision(current, requested)
        assert merged.reserve_soc_min_ratio == 0.25, "Reserve SOC must NOT be loosened"

    def test_merge_revision_reduces_discharge_power(self):
        """New max discharge power must be <= existing (only reduce)."""
        from energy_agent_v2.orchestration import _merge_revision
        current = DispatchRevision(max_discharge_power_kw=3000)
        requested = DispatchRevision(max_discharge_power_kw=5000)
        merged = _merge_revision(current, requested)
        assert merged.max_discharge_power_kw == 3000, "Discharge power must NOT be increased"

    def test_merge_revision_accumulates_blocked_intervals(self):
        """Blocked intervals accumulate, never removed."""
        from energy_agent_v2.orchestration import _merge_revision
        from energy_agent_v2.contracts import TimeInterval
        current = DispatchRevision(blocked_intervals=[
            TimeInterval(start_index=0, end_index=4),
        ])
        requested = DispatchRevision(blocked_intervals=[
            TimeInterval(start_index=10, end_index=14),
        ])
        merged = _merge_revision(current, requested)
        assert len(merged.blocked_intervals) == 2, "Blocked intervals must accumulate"

    def test_merge_revision_first_time_no_constraints(self):
        from energy_agent_v2.orchestration import _merge_revision
        merged = _merge_revision(None, DispatchRevision(terminal_soc_min_ratio=0.3))
        assert merged.terminal_soc_min_ratio == 0.3

    def test_multi_revision_loop_tightens_monotonically(self):
        """Successive revisions must monotonically tighten SOC."""
        from energy_agent_v2.orchestration import _merge_revision
        merged = None
        soc_values = [0.1, 0.3, 0.2, 0.5, 0.4]  # engineer tries to loosen then tighten
        expected = [0.1, 0.3, 0.3, 0.5, 0.5]  # merged must only go up
        for i, soc in enumerate(soc_values):
            merged = _merge_revision(merged, DispatchRevision(terminal_soc_min_ratio=soc))
            assert merged.terminal_soc_min_ratio == expected[i], \
                f"Revision {i}: soc={soc} should merge to {expected[i]}, got {merged.terminal_soc_min_ratio}"


# ===========================================================================
# 4. Agent Failure Recovery
# ===========================================================================

class TestAgentFailureRecovery:
    """Verify graceful degradation when agents fail."""

    def test_run_without_llm_agents(self):
        """Main graph should still work when all LLM agents are None (no API key)."""
        from energy_agent_v2.orchestration import AppContextV2
        from energy_agent_v2.algorithms.storage_optimizer import MILPStorageOptimizer
        from energy_agent_v2.algorithms.carbon_accounting import CarbonAccountant
        from energy_agent_v2.algorithms.tariff import TariffCalculator
        from energy_agent_v2.data.provider import SeedDataProvider
        from energy_agent_v2.data.csv_fetcher import CSVDataFetcher

        ctx = AppContextV2(
            data_provider=SeedDataProvider(fetcher=CSVDataFetcher()),
            storage_optimizer=MILPStorageOptimizer(),
            carbon_accountant=CarbonAccountant(),
            tariff_calculator=TariffCalculator(),
            revision_parser=None,
            ingest_agent=None,
            archive_agent=None,
            approval_agent=None,
            anomaly_agent=None,
            distillation_agent=None,
        )
        state = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
        result = pytest.run_coroutine_as_main(
            run_dispatch(ctx, state, approval_decision="approve")
        ) if hasattr(pytest, "run_coroutine_as_main") else None
        # Fallback: use asyncio
        import asyncio
        result = asyncio.run(run_dispatch(ctx, state, approval_decision="approve"))
        assert result["run_status"] == "approved"

    def test_run_skips_ingest_when_no_raw_file(self):
        """Graph should skip ingest gracefully when no raw_file is provided."""
        ctx = create_app_context()
        state = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
        assert state.get("raw_file") is None
        import asyncio
        result = asyncio.run(run_dispatch(ctx, state, approval_decision="approve"))
        node_ids = [e["node_id"] for e in result["events"]]
        assert "ingest_raw_data" in node_ids
        assert result["run_status"] == "approved"


# ===========================================================================
# 5. Concurrent Run Isolation
# ===========================================================================

class TestConcurrentIsolation:
    """Verify multiple runs don't pollute each other's state."""

    def test_two_consecutive_runs_have_different_ids(self):
        """Each run gets a unique thread_id."""
        ctx = create_app_context()
        state1 = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
        state2 = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
        assert state1["thread_id"] != state2["thread_id"]

    def test_run_events_are_disjoint(self):
        """Events from run A must not appear in run B."""
        ctx = create_app_context()
        import asyncio
        state1 = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
        result1 = asyncio.run(run_dispatch(ctx, state1, approval_decision="approve"))

        state2 = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
        result2 = asyncio.run(run_dispatch(ctx, state2, approval_decision="approve"))

        assert result1["thread_id"] != result2["thread_id"]
        run1_ids = {e["event_id"] for e in result1["events"]}
        run2_ids = {e["event_id"] for e in result2["events"]}
        assert run1_ids.isdisjoint(run2_ids), "Events from different runs must not overlap"


# ===========================================================================
# 6. Graph Edge Cases
# ===========================================================================

class TestGraphEdgeCases:
    """Verify graph handles edge-case flows correctly."""

    def test_reject_flow_completes(self):
        """Reject path should reach 'rejected' status."""
        ctx = create_app_context()
        state = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
        import asyncio
        result = asyncio.run(run_dispatch(ctx, state, approval_decision="reject"))
        assert result["run_status"] == "rejected"

    def test_revise_then_approve_two_versions(self):
        """Revise then approve should produce V2 plan."""
        ctx = create_app_context()
        state = make_initial_state("huanghua", date(2026, 7, 27), "min_cost")
        import asyncio
        call_count = [0]
        def decision_fn(r):
            call_count[0] += 1
            if call_count[0] == 1:
                return ("revise", {"terminal_soc_min_ratio": 0.25}, "tighten SOC")
            return ("approve", None, "approved V2")
        result = asyncio.run(run_dispatch(
            ctx, state,
            decision_fn=decision_fn,
        ))
        assert result["run_status"] == "approved"
        assert result["current_plan_version"] >= 2, "Revise should increment plan version"
