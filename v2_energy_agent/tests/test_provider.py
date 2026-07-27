from datetime import date, datetime

import pytest

from energy_agent_v2.contracts import DispatchInputBundleV2, TariffPeriod
from energy_agent_v2.data.provider import SeedDataProvider

TARGET = date(2026, 7, 26)


def test_seed_bundle_validates():
    bundle = SeedDataProvider().load_dispatch_inputs(target_date=TARGET)
    assert isinstance(bundle, DispatchInputBundleV2)
    assert len(bundle.timestamps) == 96
    assert bundle.region == "cn-hunan"
    assert bundle.site_id == "huanghua"
    assert bundle.battery.capacity_kwh == 10000.0
    assert bundle.battery.max_ramp_kw_per_step == 2000.0
    assert bundle.battery.max_cell_temperature_c == 45.0
    # SOC 从物理最低值开始（min_soc_ratio=0.1），每天清零
    assert bundle.initial_soc_ratio == 0.1
    assert bundle.time_step_minutes == 15


def test_seed_load_shape():
    load = SeedDataProvider().load_dispatch_inputs(target_date=TARGET).load_forecast_kw
    assert min(load) >= 38000.0
    assert max(load) <= 125000.0
    # 工业曲线：白天明显高于夜间
    assert load[48] > load[4]   # 12:00 vs 01:00
    assert load[68] > load[4]   # 17:00 vs 01:00


def test_seed_price_peak_valley_spread():
    prices = SeedDataProvider().load_dispatch_inputs(target_date=TARGET).electricity_price_cny_per_kwh
    # 7月是尖峰月：sharp=1.1102, valley=0.2758
    assert max(prices) == pytest.approx(1.1102)
    assert min(prices) == pytest.approx(0.2758)
    # 尖谷差 0.8344（含尖峰）
    assert max(prices) - min(prices) == pytest.approx(0.8344, abs=0.01)


def test_tariff_schedule():
    tariff = SeedDataProvider().get_tariff(month=7)  # 7月=夏季尖峰月
    assert tariff.price_sharp_cny_per_kwh == pytest.approx(1.1102)
    assert tariff.price_peak_cny_per_kwh == pytest.approx(0.8975)
    assert tariff.price_flat_cny_per_kwh == pytest.approx(0.5867)
    assert tariff.price_valley_cny_per_kwh == pytest.approx(0.2758)
    assert tariff.demand_price_cny_per_kw_month == 30.6
    assert tariff.gov_fund_rate_cny_per_kwh == pytest.approx(0.04625)
    assert tariff.reactive_adjust_ratio == pytest.approx(-0.0075)
    assert set(tariff.resolve_price_series(96)) <= {1.1102, 0.8975, 0.5867, 0.2758}
    pm = tariff.period_map
    assert pm[0] == TariffPeriod.VALLEY    # 00:00 谷
    assert pm[44] == TariffPeriod.FLAT     # 11:00 平（06-12）
    assert pm[80] == TariffPeriod.SHARP    # 20:00 尖（夏季20-24）


def test_tariff_winter_sharp():
    tariff = SeedDataProvider().get_tariff(month=1)  # 1月=冬季尖峰月
    pm = tariff.period_map
    assert pm[72] == TariffPeriod.SHARP    # 18:00 尖（冬季18-22）
    assert pm[84] == TariffPeriod.SHARP    # 21:00 尖（仍在18-22范围）
    assert pm[88] == TariffPeriod.PEAK     # 22:00 峰


def test_tariff_non_sharp_month():
    tariff = SeedDataProvider().get_tariff(month=4)  # 4月=非尖峰月
    pm = tariff.period_map
    assert TariffPeriod.SHARP not in pm    # 非尖峰月无尖峰档
    assert pm[64] == TariffPeriod.PEAK     # 16:00 峰


def test_seed_generation_mix():
    bundle = SeedDataProvider().load_dispatch_inputs(target_date=TARGET)
    assert len(bundle.generation_mix) == 96
    noon = bundle.generation_mix[48].generation_by_source_kw
    assert noon["solar"] > 0.0
    assert noon["coal"] > noon["hydro"]    # 煤电为主
    night = bundle.generation_mix[4].generation_by_source_kw
    assert night.get("solar", 0.0) == 0.0  # 夜间无光伏


def test_seed_reproducible():
    a = SeedDataProvider(seed=42).load_dispatch_inputs(target_date=TARGET)
    b = SeedDataProvider(seed=42).load_dispatch_inputs(target_date=TARGET)
    assert a.load_forecast_kw == b.load_forecast_kw
    assert a.generation_mix[0].generation_by_source_kw == b.generation_mix[0].generation_by_source_kw


def test_emission_factors_hunan():
    lib = SeedDataProvider().load_dispatch_inputs(target_date=TARGET).emission_factors
    assert lib.calibrated_region == "cn-hunan"
    assert lib.factors_kg_per_kwh["purchase"] == pytest.approx(0.5366, rel=1e-6)


def test_db_fetcher_swallows_query_error(monkeypatch):
    from energy_agent_v2.data.db_fetcher import HunanDataFetcher

    fetcher = HunanDataFetcher(db_uri="postgresql://u:p@nonexistent.invalid:5432/db")

    def boom(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(fetcher, "_query_rows", boom)
    s, e = datetime(2026, 4, 1), datetime(2026, 4, 1, 23, 45)
    assert fetcher.fetch_generation_mix(s, e) == []
    assert fetcher.fetch_realtime_price(s, e) == []


def test_db_fetcher_no_driver_returns_empty():
    # 真实环境无 psycopg2/sqlalchemy：_open_handle 抛异常，fetch 返回空
    from energy_agent_v2.data.db_fetcher import HunanDataFetcher

    fetcher = HunanDataFetcher(db_uri="postgresql://u:p@nowhere.invalid:5432/db")
    s, e = datetime(2026, 4, 1), datetime(2026, 4, 1, 23, 45)
    assert fetcher.fetch_generation_mix(s, e) == []
    assert fetcher.fetch_realtime_price(s, e) == []


def test_provider_falls_back_to_seed_when_fetcher_empty():
    class StubFetcher:
        def fetch_generation_mix(self, s, e):
            return []

        def fetch_realtime_price(self, s, e):
            return []

    bundle = SeedDataProvider().load_dispatch_inputs(target_date=TARGET, fetcher=StubFetcher())
    assert len(bundle.generation_mix) == 96   # 回退合成
