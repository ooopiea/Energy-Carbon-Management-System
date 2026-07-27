from datetime import date, datetime, time, timedelta

import pytest

from energy_agent_v2.algorithms.tariff import TariffCalculator
from energy_agent_v2.contracts import TariffPeriod, TariffSchedule

TARGET = date(2026, 7, 26)


def _ts():
    base = datetime.combine(TARGET, time(0, 0))
    return [base + timedelta(minutes=15 * i) for i in range(96)]


def _tariff(period, demand_price=30.0, declared=0.0, green_ratio=0.0, green_price=None):
    return TariffSchedule(
        period_map=[period] * 96,
        price_peak_cny_per_kwh=1.0,
        price_flat_cny_per_kwh=0.7,
        price_valley_cny_per_kwh=0.35,
        demand_price_cny_per_kw_month=demand_price,
        declared_demand_kw=declared,
        green_power_ratio=green_ratio,
        green_power_price_cny_per_kwh=green_price,
        gov_fund_rate_cny_per_kwh=0.0,
        reactive_adjust_ratio=0.0,
    )


def test_energy_cost_peak():
    res = TariffCalculator().calculate(_tariff(TariffPeriod.PEAK), [1000.0] * 96, _ts(), 15)
    # 1000 kW * 1.0 元/kWh * 0.25 h * 96 = 24000 元
    assert res.energy_cost_cny == pytest.approx(24000.0, rel=1e-9)
    assert res.green_power_cost_cny == 0.0
    assert res.gov_fund_cost_cny == 0.0
    assert res.reactive_adjustment_cny == 0.0


def test_peak_valley_spread():
    calc = TariffCalculator()
    grid = [1000.0] * 96
    r_peak = calc.calculate(_tariff(TariffPeriod.PEAK, demand_price=0.0), grid, _ts(), 15)
    r_valley = calc.calculate(_tariff(TariffPeriod.VALLEY, demand_price=0.0), grid, _ts(), 15)
    assert r_peak.energy_cost_cny > r_valley.energy_cost_cny
    # 1000 * 0.35 * 0.25 * 96 = 8400
    assert r_valley.energy_cost_cny == pytest.approx(8400.0, rel=1e-9)


def test_demand_cost_uses_declared_when_higher():
    res = TariffCalculator().calculate(
        _tariff(TariffPeriod.FLAT, demand_price=30.0, declared=2000.0),
        [1000.0] * 96, _ts(), 15,
    )
    # max(1000, 2000) * 30 = 60000
    assert res.demand_cost_cny == pytest.approx(60000.0, rel=1e-9)
    assert res.peak_demand_kw == pytest.approx(1000.0)


def test_demand_cost_uses_actual_when_higher():
    res = TariffCalculator().calculate(
        _tariff(TariffPeriod.FLAT, demand_price=30.0, declared=500.0),
        [1000.0] * 96, _ts(), 15,
    )
    # 实际峰值 1000 > 申报 500，按实际计
    assert res.demand_cost_cny == pytest.approx(30000.0, rel=1e-9)


def test_effective_price():
    res = TariffCalculator().calculate(
        _tariff(TariffPeriod.PEAK, demand_price=0.0), [1000.0] * 96, _ts(), 15
    )
    # 总电量 24000 kWh，电度 24000 元，综合度电成本 = 1.0
    assert res.effective_price_cny_per_kwh == pytest.approx(1.0, rel=1e-9)
    assert res.total_cost_cny == pytest.approx(24000.0, rel=1e-9)


def test_period_labels_from_map():
    tariff = TariffSchedule(
        period_map=[TariffPeriod.PEAK, TariffPeriod.VALLEY] + [TariffPeriod.FLAT] * 94,
        price_peak_cny_per_kwh=1.0, price_flat_cny_per_kwh=0.7,
        price_valley_cny_per_kwh=0.35, demand_price_cny_per_kw_month=0.0,
    )
    res = TariffCalculator().calculate(tariff, [0.0] * 96, _ts(), 15)
    assert res.period_labels[0] == "peak"
    assert res.period_labels[1] == "valley"
    assert res.period_labels[5] == "flat"


def test_period_labels_inferred_from_price():
    tariff = TariffSchedule(
        price_cny_per_kwh_by_step=[1.0, 0.35] + [0.7] * 94,
        price_peak_cny_per_kwh=1.0, price_flat_cny_per_kwh=0.7,
        price_valley_cny_per_kwh=0.35, demand_price_cny_per_kw_month=0.0,
    )
    res = TariffCalculator().calculate(tariff, [0.0] * 96, _ts(), 15)
    assert res.period_labels[0] == "peak"
    assert res.period_labels[1] == "valley"
    assert res.period_labels[2] == "flat"


def test_green_power_env_value():
    """绿电口径对齐厂务实际账单：所有电量含绿电统一按分时计费，
    绿电环境价值费用单独附加（不重复计入电度电费）。"""
    res = TariffCalculator().calculate(
        _tariff(TariffPeriod.PEAK, demand_price=0.0, green_ratio=0.2, green_price=0.5),
        [1000.0] * 96, _ts(), 15,
    )
    # 所有电量（含绿电）统一按峰电价计费：1000*1.0*0.25*96 = 24000
    assert res.energy_cost_cny == pytest.approx(24000.0, rel=1e-9)
    # 绿电环境价值：绿电电量 1000*0.2*0.25*96=4800 kWh × 0.5 元/kWh = 2400 元
    assert res.green_power_cost_cny == pytest.approx(2400.0, rel=1e-9)


def test_gov_fund_cost():
    tariff = TariffSchedule(
        period_map=[TariffPeriod.FLAT] * 96,
        price_flat_cny_per_kwh=0.6,
        demand_price_cny_per_kw_month=0.0,
        gov_fund_rate_cny_per_kwh=0.04625,
        reactive_adjust_ratio=0.0,
    )
    res = TariffCalculator().calculate(tariff, [1000.0] * 96, _ts(), 15)
    # 总电量 24000 kWh，政府基金 = 24000 * 0.04625 = 1110
    assert res.gov_fund_cost_cny == pytest.approx(1110.0, rel=1e-9)


def test_reactive_adjustment():
    tariff = TariffSchedule(
        period_map=[TariffPeriod.PEAK] * 96,
        price_peak_cny_per_kwh=1.0,
        demand_price_cny_per_kw_month=30.0,
        declared_demand_kw=1000.0,
        gov_fund_rate_cny_per_kwh=0.0,
        reactive_adjust_ratio=-0.0075,
    )
    res = TariffCalculator().calculate(tariff, [1000.0] * 96, _ts(), 15)
    # energy=24000, demand=max(1000,1000)*30=30000
    # reactive = (24000+30000)*(-0.0075) = -405
    assert res.reactive_adjustment_cny == pytest.approx(-405.0, rel=1e-9)
    assert res.energy_cost_cny == pytest.approx(24000.0, rel=1e-9)
    assert res.demand_cost_cny == pytest.approx(30000.0, rel=1e-9)


def test_seed_integration():
    from energy_agent_v2.data.provider import SeedDataProvider

    bundle = SeedDataProvider().load_dispatch_inputs(target_date=TARGET)
    res = TariffCalculator().calculate(
        bundle.tariff, bundle.load_forecast_kw, bundle.timestamps, 15
    )
    assert res.total_cost_cny > 0
    assert res.effective_price_cny_per_kwh > 0.35
    assert res.peak_demand_kw == pytest.approx(max(bundle.load_forecast_kw), rel=1e-6)
    assert "peak" in res.period_labels
    assert "valley" in res.period_labels


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        TariffCalculator().calculate(_tariff(TariffPeriod.PEAK), [1000.0] * 50, _ts(), 15)
