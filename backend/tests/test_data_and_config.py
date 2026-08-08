from __future__ import annotations

from core.config import (
    SITE_SOLAR_CAPACITY_KW,
    SITE_STORAGE_CAPACITY_KWH,
    SITE_STORAGE_POWER_KW,
    STORAGE_DEFAULTS,
    build_period_map,
)
from data.simulator import DataSimulator


def test_confirmed_site_capacities_and_monthly_periods() -> None:
    assert SITE_SOLAR_CAPACITY_KW == 19_100
    assert SITE_STORAGE_POWER_KW == 15_000
    assert SITE_STORAGE_CAPACITY_KWH == 30_000
    assert STORAGE_DEFAULTS["charge_efficiency_ratio"] == 0.90
    assert STORAGE_DEFAULTS["discharge_efficiency_ratio"] == 0.90
    assert STORAGE_DEFAULTS["mode_switch_penalty_cny"] == 10.0
    assert build_period_map(7)[80] == "sharp"  # 20:00
    assert build_period_map(2)[80] == "peak"
    assert build_period_map(1)[72] == "sharp"  # 18:00


def test_simulator_is_15_minute_and_reports_provenance() -> None:
    day = DataSimulator(seed=7).generate_day(day_index=0, month=7)
    assert len(day["timestamps"]) == 96
    assert all(
        (day["timestamps"][index + 1] - day["timestamps"][index]).total_seconds() == 900
        for index in range(95)
    )
    assert 15_000 < max(day["solar_kw"]) <= SITE_SOLAR_CAPACITY_KW
    assert day["asset_registry"]["summary"]["chiller_units"] == 37
    assert day["asset_registry"]["summary"]["compressor_units"] == 38
    assert day["compressor_capability"]["dispatch_mode"] == "monitor_only"
    assert set(day["data_provenance"]) == {
        "load", "weather", "site_solar", "generation_mix", "tariff", "assets", "cr"
    }
    assert len(day["cr_factors"]) == 96
    assert all(isinstance(v, float) for v in day["cr_factors"])


def test_cross_month_global_day_index_does_not_double_count_days() -> None:
    day = DataSimulator(seed=1).generate_day(day_index=17, month=8)
    assert day["timestamps"][0].date().isoformat() == "2025-08-01"
