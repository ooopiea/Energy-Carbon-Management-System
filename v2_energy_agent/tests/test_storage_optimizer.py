"""Tests for MILPStorageOptimizer.

96-point (15-min) synthetic scenario: industrial load profile + Hunan
peak/valley tariff + a representative BatteryConfigV2.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from energy_agent_v2.algorithms.storage_optimizer import MILPStorageOptimizer
from energy_agent_v2.contracts import (
    BatteryConfigV2,
    DispatchInputBundleV2,
    DispatchObjective,
    DispatchRevision,
    StorageDispatchRequestV2,
    TimeInterval,
)
from energy_agent_v2.errors import AppError

# tolerances for solver numerical slack
TOL_SOC = 1e-4
TOL_KW = 1e-1
TOL_TEMP = 1e-1


# ---------------------------------------------------------------------------
# scenario builders
# ---------------------------------------------------------------------------

def _battery() -> BatteryConfigV2:
    return BatteryConfigV2(
        battery_id="test-battery-01",
        capacity_kwh=2000.0,
        max_charge_power_kw=500.0,
        max_discharge_power_kw=500.0,
        min_soc_ratio=0.1,
        max_soc_ratio=0.9,
        charge_efficiency_ratio=0.95,
        discharge_efficiency_ratio=0.95,
        max_ramp_kw_per_step=300.0,
        max_cell_temperature_c=55.0,
        thermal_resistance_c_per_kw=0.03,
        thermal_time_constant_min=30.0,
        ambient_temperature_c=25.0,
    )


def _timestamps() -> list[datetime]:
    base = datetime(2025, 7, 15, 0, 0, 0)
    return [base + timedelta(minutes=15 * i) for i in range(96)]


def _load_series() -> list[float]:
    rng = np.random.default_rng(42)
    loads: list[float] = []
    for step in range(96):
        h = step * 0.25
        if h < 6:
            base = 350.0
        elif h < 8:
            base = 350.0 + (h - 6) * 275.0
        elif h < 12:
            base = 900.0 + 120.0 * math.sin((h - 8) / 4 * math.pi)
        elif h < 14:
            base = 850.0
        elif h < 18:
            base = 1000.0 + 80.0 * math.sin((h - 14) / 4 * math.pi)
        elif h < 21:
            base = 1200.0 + 80.0 * math.sin((h - 18) / 3 * math.pi)
        else:
            base = 1200.0 - (h - 21) * 283.0
        loads.append(float(max(base + rng.normal(0, 15), 200.0)))
    return loads


def _price_series() -> list[float]:
    """Hunan-style TOU tariff (yuan/kWh), 96 steps."""
    prices: list[float] = []
    for step in range(96):
        h = step * 0.25
        if h < 8:
            prices.append(0.35)       # valley
        elif h < 11:
            prices.append(0.60)       # flat
        elif h < 15:
            prices.append(0.90)       # peak
        elif h < 18:
            prices.append(0.60)       # flat
        elif h < 21:
            prices.append(1.25)       # sharp
        else:
            prices.append(0.60)       # flat
    return prices


def _inputs() -> DispatchInputBundleV2:
    return DispatchInputBundleV2(
        data_version="2025-07-15-v1",
        site_id="test-park",
        target_date=date(2025, 7, 15),
        time_step_minutes=15,
        timestamps=_timestamps(),
        load_forecast_kw=_load_series(),
        electricity_price_cny_per_kwh=_price_series(),
        initial_soc_ratio=0.5,
        battery=_battery(),
    )


def _request(
    objective: DispatchObjective = DispatchObjective.MIN_COST,
    revision: DispatchRevision | None = None,
) -> StorageDispatchRequestV2:
    return StorageDispatchRequestV2(
        dispatch_run_id="run-test-001",
        plan_id="plan-test-001",
        plan_version=1,
        objective=objective,
        inputs=_inputs(),
        revision=revision,
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_min_cost_optimization():
    """Core scenario: min_cost, verify all constraints and savings."""
    result = MILPStorageOptimizer().optimize(_request())
    bat = _battery()

    assert result.solver_status == "Optimal"
    assert result.algorithm_version == "milp-storage-2.0.0"
    assert result.point_count == 96
    assert result.constraint_check.passed is True
    assert len(result.constraint_check.violations) == 0

    # SOC within [min, max]
    for i, s in enumerate(result.soc_ratio):
        assert bat.min_soc_ratio - TOL_SOC <= s <= bat.max_soc_ratio + TOL_SOC, (
            f"SOC out of range at step {i}: {s}"
        )

    # ramp each step <= max_ramp
    pw = result.battery_power_kw
    for i in range(len(pw)):
        prev = 0.0 if i == 0 else pw[i - 1]
        assert abs(pw[i] - prev) <= bat.max_ramp_kw_per_step + TOL_KW, (
            f"Ramp violation at step {i}: {abs(pw[i] - prev)}"
        )

    # temperature <= max
    for i, tc in enumerate(result.cell_temperature_c):
        assert tc <= bat.max_cell_temperature_c + TOL_TEMP, (
            f"Temperature violation at step {i}: {tc}"
        )

    # 每天清零：末端 SOC 必须回到物理最低值 0.1
    assert abs(result.terminal_soc_ratio - 0.1) <= TOL_SOC, (
        f"Terminal SOC {result.terminal_soc_ratio} not cleared to min 0.1"
    )

    # cost improvement
    assert result.optimized_energy_cost_cny <= result.baseline_energy_cost_cny + 1.0, (
        f"Optimized cost {result.optimized_energy_cost_cny} > "
        f"baseline {result.baseline_energy_cost_cny}"
    )
    assert result.energy_cost_saving_cny >= -1.0


def test_revision_blocked_intervals():
    """Blocked steps must have zero battery power."""
    revision = DispatchRevision(
        blocked_intervals=[TimeInterval(start_index=48, end_index=51)],
    )
    result = MILPStorageOptimizer().optimize(_request(revision=revision))

    assert result.solver_status == "Optimal"
    for t in range(48, 52):
        assert abs(result.battery_power_kw[t]) <= TOL_KW, (
            f"Power not zero at blocked step {t}: {result.battery_power_kw[t]}"
        )


def test_infeasible_terminal_soc():
    """Terminal SOC above max_soc must raise STORAGE_OPTIMIZATION_INFEASIBLE."""
    revision = DispatchRevision(terminal_soc_min_ratio=0.95)  # > max_soc 0.9
    with pytest.raises(AppError) as exc_info:
        MILPStorageOptimizer().optimize(_request(revision=revision))
    assert exc_info.value.code == "STORAGE_OPTIMIZATION_INFEASIBLE"


def test_limit_peak_demand():
    """Peak demand objective should reduce (or not increase) peak."""
    result = MILPStorageOptimizer().optimize(
        _request(objective=DispatchObjective.LIMIT_PEAK_DEMAND)
    )
    assert result.solver_status == "Optimal"
    assert result.constraint_check.passed is True
    assert result.optimized_peak_demand_kw <= result.baseline_peak_demand_kw + TOL_KW
    assert result.peak_reduction_kw >= -TOL_KW


def test_min_carbon():
    """Min carbon objective should solve feasibly."""
    result = MILPStorageOptimizer().optimize(
        _request(objective=DispatchObjective.MIN_CARBON)
    )
    assert result.solver_status == "Optimal"
    assert result.constraint_check.passed is True
    assert result.algorithm_version == "milp-storage-2.0.0"


def test_revision_reserve_and_discharge_limit():
    """Reserve SOC and discharge power limits should be respected."""
    revision = DispatchRevision(
        reserve_soc_min_ratio=0.3,
        max_discharge_power_kw=300.0,
    )
    result = MILPStorageOptimizer().optimize(_request(revision=revision))

    assert result.solver_status == "Optimal"
    assert result.constraint_check.passed is True
    for i, s in enumerate(result.soc_ratio):
        assert s >= 0.3 - TOL_SOC, f"SOC below reserve at step {i}: {s}"
    for i, p in enumerate(result.battery_power_kw):
        assert p <= 300.0 + TOL_KW, f"Discharge exceeds revised limit at step {i}: {p}"


def test_weighted_objective():
    """加权目标 min(alpha*电费+需量 + beta*碳排) 应优化电费+碳排+削峰。"""
    request = StorageDispatchRequestV2(
        dispatch_run_id="run-weighted-001",
        plan_id="plan-weighted-001",
        plan_version=1,
        objective=DispatchObjective.WEIGHTED,
        inputs=_inputs(),
        weight_alpha_cost=1.0,
        weight_beta_carbon=0.01,
    )
    result = MILPStorageOptimizer().optimize(request)
    assert result.solver_status == "Optimal"
    assert result.constraint_check.passed is True
    # 削峰应有效果（峰值不高于基线）
    assert result.optimized_peak_demand_kw <= result.baseline_peak_demand_kw + TOL_KW
    # SOC 每天从 min 开始、结束清零到 min
    assert abs(result.soc_ratio[0] - 0.1) < TOL_SOC or result.soc_ratio[0] > 0.1 - TOL_SOC
    assert abs(result.terminal_soc_ratio - 0.1) <= TOL_SOC, (
        f"Terminal SOC {result.terminal_soc_ratio} not cleared to min 0.1"
    )

def test_weighted_objective():
    """加权目标 min(alpha*电费+需量 + beta*碳排) 应正常求解。"""
    request = StorageDispatchRequestV2(
        dispatch_run_id="run-weighted-001",
        plan_id="plan-weighted-001",
        plan_version=1,
        objective=DispatchObjective.WEIGHTED,
        inputs=_inputs(),
        weight_alpha_cost=1.0,
        weight_beta_carbon=0.01,
    )
    result = MILPStorageOptimizer().optimize(request)
    assert result.solver_status == "Optimal"
    assert result.constraint_check.passed is True
    # 电费不应恶化
    assert result.optimized_energy_cost_cny <= result.baseline_energy_cost_cny + 1.0
    # SOC 每天从 min 开始、结束清零到 min
    assert abs(result.terminal_soc_ratio - 0.1) <= TOL_SOC, (
        f"Terminal SOC {result.terminal_soc_ratio} not cleared to min 0.1"
    )
