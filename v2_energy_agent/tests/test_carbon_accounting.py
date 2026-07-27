"""电碳计量模块测试：C(τ) 直接因子、Cr(τ) 江亿动态责任因子与方案碳核算。"""

from __future__ import annotations

from datetime import datetime

import pytest

from energy_agent_v2.algorithms.carbon_accounting import CarbonAccountant
from energy_agent_v2.contracts import EmissionFactorLibrary, GenerationMixPoint, GenerationSource

COAL = GenerationSource.COAL.value
HYDRO = GenerationSource.HYDRO.value
WIND = GenerationSource.WIND.value
SOLAR = GenerationSource.SOLAR.value


def _mix(ts: datetime, sources: dict[str, float]) -> GenerationMixPoint:
    return GenerationMixPoint(timestamp=ts, generation_by_source_kw=dict(sources))


def _ts(n: int) -> list[datetime]:
    return [datetime(2026, 7, 27, h) for h in range(n)]


@pytest.fixture
def accountant() -> CarbonAccountant:
    return CarbonAccountant(response_lambda=0.5)


@pytest.fixture
def ef_lib() -> EmissionFactorLibrary:
    return EmissionFactorLibrary()


# ---------- 1. 直接因子手算基准 ----------
def test_direct_factor_weighted_average(accountant: CarbonAccountant, ef_lib: EmissionFactorLibrary):
    """煤电8MW+水电2MW+风电1MW 等，手算 C(τ) 与代码一致。"""
    ts = _ts(3)
    mix = [
        _mix(ts[0], {COAL: 8000.0, HYDRO: 2000.0, WIND: 1000.0}),
        _mix(ts[1], {COAL: 4000.0, WIND: 6000.0}),
        _mix(ts[2], {COAL: 6000.0, WIND: 4000.0}),
    ]
    result = accountant.compute_factors(mix, ef_lib, "cn-hunan")
    c = result.direct_factor_c_kg_per_kwh

    expected0 = (8000 * 0.85 + 2000 * 0 + 1000 * 0) / 11000  # ≈0.61818
    expected1 = (4000 * 0.85) / 10000  # 0.34
    expected2 = (6000 * 0.85) / 10000  # 0.51
    assert c[0] == pytest.approx(expected0)
    assert c[1] == pytest.approx(expected1)
    assert c[2] == pytest.approx(expected2)


# ---------- 2. 全可再生能源时段 C=0 ----------
def test_all_renewable_factor_zero(accountant: CarbonAccountant, ef_lib: EmissionFactorLibrary):
    mix = [_mix(_ts(1)[0], {WIND: 5000.0, HYDRO: 5000.0})]
    result = accountant.compute_factors(mix, ef_lib, "cn-hunan")
    assert result.direct_factor_c_kg_per_kwh[0] == pytest.approx(0.0)


# ---------- 3. 全煤电时段 C=0.85 ----------
def test_all_coal_factor(accountant: CarbonAccountant, ef_lib: EmissionFactorLibrary):
    mix = [_mix(_ts(1)[0], {COAL: 10000.0})]
    result = accountant.compute_factors(mix, ef_lib, "cn-hunan")
    assert result.direct_factor_c_kg_per_kwh[0] == pytest.approx(0.85)


# ---------- 4. Cr 方向性：低碳时段<C，高碳时段>C ----------
def test_responsibility_factor_direction(accountant: CarbonAccountant, ef_lib: EmissionFactorLibrary):
    ts = _ts(3)
    # 步1高碳(煤8风2→C=0.68)，步2低碳(煤1风9→C=0.085)，步3中碳(煤5风5→C=0.425)
    mix = [
        _mix(ts[0], {COAL: 8000.0, WIND: 2000.0}),
        _mix(ts[1], {COAL: 1000.0, WIND: 9000.0}),
        _mix(ts[2], {COAL: 5000.0, WIND: 5000.0}),
    ]
    result = accountant.compute_factors(mix, ef_lib, "cn-hunan")
    c = result.direct_factor_c_kg_per_kwh
    cr = result.responsibility_factor_cr_kg_per_kwh
    c_bar = sum(c) / len(c)

    # 步1: 高碳 C>C̄ → 责任加重 Cr>C
    assert c[0] > c_bar
    assert cr[0] > c[0]
    # 步2: 低碳 C<C̄ 且 C>0 → 责任减免 Cr<C
    assert c[1] < c_bar
    assert c[1] > 0
    assert cr[1] < c[1]


# ---------- 5. 守恒近似 mean(Cr)/mean(C) ∈ [0.9,1.1] ----------
def test_conservation_approximation(accountant: CarbonAccountant, ef_lib: EmissionFactorLibrary):
    ts = _ts(3)
    # 小波动场景：煤6风4→0.51, 煤4风6→0.34, 煤5风5→0.425
    mix = [
        _mix(ts[0], {COAL: 6000.0, WIND: 4000.0}),
        _mix(ts[1], {COAL: 4000.0, WIND: 6000.0}),
        _mix(ts[2], {COAL: 5000.0, WIND: 5000.0}),
    ]
    result = accountant.compute_factors(mix, ef_lib, "cn-hunan")
    c = result.direct_factor_c_kg_per_kwh
    cr = result.responsibility_factor_cr_kg_per_kwh
    ratio = (sum(cr) / len(cr)) / (sum(c) / len(c))
    assert 0.9 <= ratio <= 1.1


# ---------- 6. account_dispatch 方案碳核算 ----------
def test_account_dispatch(accountant: CarbonAccountant, ef_lib: EmissionFactorLibrary):
    ts = _ts(2)
    mix = [
        _mix(ts[0], {COAL: 8000.0, WIND: 2000.0}),
        _mix(ts[1], {WIND: 5000.0, HYDRO: 5000.0}),
    ]
    result = accountant.compute_factors(mix, ef_lib, "cn-hunan")
    c = result.direct_factor_c_kg_per_kwh
    cr = result.responsibility_factor_cr_kg_per_kwh

    baseline = [1000.0, 1000.0]
    optimized = [500.0, 1500.0]  # 把负荷从高碳时段挪到零碳时段
    step_hours = 0.25

    dispatch = accountant.account_dispatch(result, baseline, optimized, step_hours)

    exp_baseline_direct = sum(baseline[t] * c[t] * step_hours for t in range(2))
    exp_optimized_direct = sum(optimized[t] * c[t] * step_hours for t in range(2))
    assert dispatch.baseline_direct_carbon_kg == pytest.approx(exp_baseline_direct)
    assert dispatch.optimized_direct_carbon_kg == pytest.approx(exp_optimized_direct)
    assert dispatch.direct_carbon_reduction_kg == pytest.approx(
        exp_baseline_direct - exp_optimized_direct
    )

    exp_baseline_resp = sum(baseline[t] * cr[t] * step_hours for t in range(2))
    assert dispatch.baseline_responsibility_carbon_kg == pytest.approx(exp_baseline_resp)
    # 因子库版本
    assert result.emission_factor_library_version == "ef-hunan-2022-v1"
