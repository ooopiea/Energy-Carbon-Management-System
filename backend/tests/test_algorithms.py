from __future__ import annotations

import pytest

from algorithms.carbon import compute_carbon_factors
from algorithms.compressor import assess_compressor_flexibility
from algorithms.hvac import optimize_hvac_dispatch
from algorithms.storage import optimize_storage_dispatch
from algorithms.tariff import calculate_tariff


def test_storage_has_fair_terminal_soc_and_mutually_exclusive_modes() -> None:
    result = optimize_storage_dispatch(
        load_kw=[100.0] * 8,
        price_cny_per_kwh=[0.2] * 4 + [1.0] * 4,
        battery_config={
            "capacity_kwh": 100.0,
            "max_charge_power_kw": 50.0,
            "max_discharge_power_kw": 50.0,
            "min_soc_ratio": 0.1,
            "max_soc_ratio": 0.9,
            "max_ramp_kw_per_step": 50.0,
            "thermal_resistance_c_per_kw": 0.01,
            "max_equivalent_full_cycles": 1.5,
        },
        initial_soc=0.5,
        demand_price_cny_per_kw_month=0.0,
    )
    assert result["terminal_soc"] == pytest.approx(0.5, abs=1e-4)
    assert result["saving_cny"] >= 0
    assert result["violations"] == []
    assert all(
        not (charge > 1e-6 and discharge > 1e-6)
        for charge, discharge in zip(result["p_ch_kw"], result["p_dis_kw"], strict=True)
    )


def test_hvac_uses_real_registry_scale_without_fictional_ice_storage() -> None:
    n = 96
    result = optimize_hvac_dispatch(
        cooling_load_kw=[80_000.0] * n,
        outdoor_temp_c=[35.0] * n,
        price_cny_per_kwh=[0.8] * n,
        tariff_periods=["peak"] * n,
    )
    assert result["total_chillers"] == 37
    assert result["total_rated_kw"] == pytest.approx(256_708)
    assert result["thermal_storage_enabled"] is False
    assert max(result["ice_storage_kwh"]) == 0
    assert result["saving_cny"] >= 0
    assert max(result["return_temp_c"]) <= 12.0


def test_tariff_prorates_monthly_demand_charge_for_daily_horizon() -> None:
    result = calculate_tariff([100.0] * 96, [1.0] * 96)
    assert result["monthly_demand_cost_cny"] == pytest.approx(3060.0)
    assert result["demand_cost_cny"] == pytest.approx(102.0)
    assert result["demand_allocation_ratio"] == pytest.approx(1 / 30, abs=1e-6)


def test_carbon_accounts_for_unclassified_generation() -> None:
    result = compute_carbon_factors([
        {"coal": 20.0, "hydro": 20.0, "wind": 10.0, "solar": 0.0, "other": 50.0, "total": 100.0}
    ])
    expected = (20 * 0.85 + 50 * 0.5366) / 100
    assert result["c_factors"][0] == pytest.approx(expected)
    assert result["known_generation_ratio_mean"] == pytest.approx(0.5)


def test_compressor_stays_monitor_only_without_pressure_data() -> None:
    result = assess_compressor_flexibility([10_000.0, 20_000.0])
    assert result["dispatch_ready"] is False
    assert result["dispatch_mode"] == "monitor_only"
    assert "pressure_bar" in result["missing_inputs"]
