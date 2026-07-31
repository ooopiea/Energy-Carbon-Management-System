from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np

from energy_agent_v2.contracts import (
    BatteryConfigV2,
    DispatchInputBundleV2,
    EmissionFactorLibrary,
    GenerationMixPoint,
    TariffPeriod,
    TariffSchedule,
)

POINTS_PER_DAY = 96
STEP_MINUTES = 15


class SeedDataProvider:
    """合成种子数据提供者（无网络可独立演示）。

    生成覆盖完整一天（96 点、15 分钟粒度）的园区调度输入：
    工业负荷曲线、湖南分时电价、10 MWh 储能配置、湖南典型发电结构。
    所有随机量由固定 seed 驱动，保证可复现。
    """

    REGION = "cn-hunan"

    # 真实黄花月度4档到户含税电度单价（元/kWh）
    # 来源：蓝思科技黄花园区 2026年1-6月实际结算账单（GB2312 CSV）
    # 7-12月为推断值（无实际账单），用1-6月月度均值 + 尖峰月套用1月尖峰价1.1102
    MONTHLY_PRICES: dict[int, dict[str, float | None]] = {
        1:  {"sharp": 1.1102, "peak": 0.9310, "valley": 0.2588, "flat": 0.5949},
        2:  {"sharp": None,   "peak": 1.0394, "valley": 0.3272, "flat": 0.6833},
        3:  {"sharp": None,   "peak": 0.9284, "valley": 0.2582, "flat": 0.5933},
        4:  {"sharp": None,   "peak": 0.7164, "valley": 0.2450, "flat": 0.4807},
        5:  {"sharp": None,   "peak": 0.8316, "valley": 0.2460, "flat": 0.5388},
        6:  {"sharp": None,   "peak": 0.9383, "valley": 0.3197, "flat": 0.6290},
        # 7-12月推断值（均值 peak=0.8975/valley=0.2758/flat=0.5867）
        7:  {"sharp": 1.1102, "peak": 0.8975, "valley": 0.2758, "flat": 0.5867},
        8:  {"sharp": 1.1102, "peak": 0.8975, "valley": 0.2758, "flat": 0.5867},
        9:  {"sharp": None,   "peak": 0.8975, "valley": 0.2758, "flat": 0.5867},
        10: {"sharp": None,   "peak": 0.8975, "valley": 0.2758, "flat": 0.5867},
        11: {"sharp": None,   "peak": 0.8975, "valley": 0.2758, "flat": 0.5867},
        12: {"sharp": 1.1102, "peak": 0.8975, "valley": 0.2758, "flat": 0.5867},
    }
    # 尖峰触发月份（冬夏高负荷季）
    SHARP_MONTHS = {1, 7, 8, 12}
    WINTER_SHARP_MONTHS = {1, 12}
    SUMMER_SHARP_MONTHS = {7, 8}

    DEMAND_PRICE_CNY_PER_KW_MONTH = 30.6  # 黄花实际需量单价
    DECLARED_DEMAND_KW = 110_000.0
    GOV_FUND_RATE_CNY_PER_KWH = 0.04625  # 政府基金及附加
    REACTIVE_ADJUST_RATIO = -0.0075  # 力调 -0.75%

    # 发电结构基准比例（不含光伏；夜间按其和归一化）
    GEN_BASE = {
        "coal": 0.62,
        "hydro": 0.25,
        "wind": 0.06,
        "purchase": 0.06,
    }

    def __init__(self, seed: int = 20260726, fetcher: Any | None = None) -> None:
        self.seed = seed
        self._fetcher = fetcher

    # ------------------------------------------------------------------ 时间轴
    @staticmethod
    def _build_timestamps(target_date: date) -> list[datetime]:
        base = datetime.combine(target_date, time(0, 0))
        return [base + timedelta(minutes=STEP_MINUTES * i) for i in range(POINTS_PER_DAY)]

    # ----------------------------------------------------- 湖南分时时段映射
    @classmethod
    def _build_period_map(cls, month: int) -> list[TariffPeriod]:
        """黄花园区真实分时时段映射（按电价分析报告）。

        全年通用时段：
        - 谷(valley): 00:00-06:00, 12:00-14:00
        - 平(flat):   06:00-12:00, 14:00-16:00
        - 峰(peak):   16:00-24:00（非尖峰时段）

        季节性尖峰（仅 1/7/8/12 月，从高峰时段中划出）：
        - 冬季(1,12月) 尖: 18:00-22:00
        - 夏季(7,8月) 尖: 20:00-24:00
        """
        periods: list[TariffPeriod] = []
        for i in range(POINTS_PER_DAY):
            hour = (i * STEP_MINUTES) // 60
            if hour < 6 or 12 <= hour < 14:
                periods.append(TariffPeriod.VALLEY)
            elif 6 <= hour < 12 or 14 <= hour < 16:
                periods.append(TariffPeriod.FLAT)
            else:
                if month in cls.SHARP_MONTHS:
                    if month in cls.WINTER_SHARP_MONTHS and 18 <= hour < 22:
                        periods.append(TariffPeriod.SHARP)
                    elif month in cls.SUMMER_SHARP_MONTHS and 20 <= hour < 24:
                        periods.append(TariffPeriod.SHARP)
                    else:
                        periods.append(TariffPeriod.PEAK)
                else:
                    periods.append(TariffPeriod.PEAK)
        return periods

    # ------------------------------------------------------------ 分时电价规则
    def get_tariff(self, site_id: str = "huanghua", month: int | None = None) -> TariffSchedule:
        """返回黄花园区真实分时电价规则（月度4档 + 需量 + 政府基金 + 力调）。"""
        if month is None:
            month = date.today().month
        prices = self.MONTHLY_PRICES.get(month, self.MONTHLY_PRICES[7])
        return TariffSchedule(
            period_map=self._build_period_map(month),
            price_sharp_cny_per_kwh=prices["sharp"] or 0.0,
            price_peak_cny_per_kwh=prices["peak"],
            price_flat_cny_per_kwh=prices["flat"],
            price_valley_cny_per_kwh=prices["valley"],
            demand_price_cny_per_kw_month=self.DEMAND_PRICE_CNY_PER_KW_MONTH,
            declared_demand_kw=self.DECLARED_DEMAND_KW,
            gov_fund_rate_cny_per_kwh=self.GOV_FUND_RATE_CNY_PER_KWH,
            reactive_adjust_ratio=self.REACTIVE_ADJUST_RATIO,
            tariff_month=month,
        )

    # ------------------------------------------------------------ 合成负荷
    def _synth_load(self, rng: np.random.Generator) -> np.ndarray:
        # 24 小时工业负荷基准形状（双峰：上午 + 下午），单位 kW
        hourly_base = np.array(
            [
                46000, 45000, 44000, 44000, 45000, 47000,   # 00-05 谷段
                52000, 65000, 82000, 92000,                 # 06-09 爬升
                105000, 112000,                             # 10-11 上午峰
                98000, 96000, 98000,                        # 12-14 午间回落
                108000, 116000, 119000, 118000,             # 15-18 下午峰
                116000, 112000, 88000,                      # 19-21
                68000, 54000,                               # 22-23 下降
            ],
            dtype=float,
        )
        x_15 = np.arange(POINTS_PER_DAY) / 4.0  # 每个 15 分钟点对应的起始小时
        load = np.interp(x_15, np.arange(24), hourly_base)
        load = load * (1.0 + rng.normal(0.0, 0.02, POINTS_PER_DAY))  # ±2% 抖动
        return np.clip(load, 38000.0, 125000.0)

    # ------------------------------------------------------------ 光伏比例
    @staticmethod
    def _solar_ratio(hour: float) -> float:
        if hour < 6.0 or hour >= 19.0:
            return 0.0
        # 钟形曲线，正午 12:30 最高约 0.14
        return 0.14 * math.exp(-((hour - 12.5) ** 2) / 12.0)

    # ------------------------------------------------------------ 合成发电结构
    def _synth_generation_mix(
        self, timestamps: list[datetime], load: np.ndarray
    ) -> list[GenerationMixPoint]:
        base_sum = sum(self.GEN_BASE.values())
        points: list[GenerationMixPoint] = []
        for i, ts in enumerate(timestamps):
            hour = i / 4.0
            total_gen = float(load[i]) * 1.05  # 发电略高于用电
            solar = self._solar_ratio(hour)
            non_solar = 1.0 - solar
            by_source: dict[str, float] = {
                k: total_gen * (v / base_sum) * non_solar for k, v in self.GEN_BASE.items()
            }
            if solar > 0.0:
                by_source["solar"] = total_gen * solar
            points.append(GenerationMixPoint(timestamp=ts, generation_by_source_kw=by_source))
        return points

    # ------------------------------------------------------------ 储能配置
    @staticmethod
    def _build_battery() -> BatteryConfigV2:
        return BatteryConfigV2(
            battery_id="battery-huanghua-01",
            capacity_kwh=10000.0,        # 10 MWh
            max_charge_power_kw=5000.0,  # 5 MW
            max_discharge_power_kw=5000.0,
            min_soc_ratio=0.1,
            max_soc_ratio=0.9,
            charge_efficiency_ratio=0.95,
            discharge_efficiency_ratio=0.95,
            max_ramp_kw_per_step=2000.0,
            max_cell_temperature_c=45.0,
            thermal_resistance_c_per_kw=0.002,
            thermal_time_constant_min=60.0,
            ambient_temperature_c=25.0,
        )

    # ------------------------------------------------------------ 主入口
    def load_dispatch_inputs(
        self,
        site_id: str = "huanghua",
        target_date: date | None = None,
        fetcher: Any | None = None,
        price_mode: str = "auto",
        battery_override: dict[str, Any] | None = None,
    ) -> DispatchInputBundleV2:
        """生成合成种子数据；price_mode 控制电价来源（见 SPEC_SCENARIOS D3/D4）。

        price_mode:
          - "tou":  强制分时电价，即使有 CSV 出清价也不覆盖（场景 A/B）
          - "rtp":  强制实时出清价，无数据时报错而非静默回退（场景 C）
          - "auto": 维持旧行为，有 CSV 出清价就用 RTP，无则分时
        battery_override: 覆盖默认 10 MWh 电池参数（如容量扩到 40 MWh）。
        """
        if target_date is None:
            target_date = date.today()

        timestamps = self._build_timestamps(target_date)
        rng = np.random.default_rng(self.seed)
        load = self._synth_load(rng)

        tariff = self.get_tariff(site_id, target_date.month)
        price_series = tariff.resolve_price_series(POINTS_PER_DAY)

        # 发电结构：优先真实数据，空则回退合成
        generation_mix = self._synth_generation_mix(timestamps, load)
        effective_fetcher = fetcher or self._fetcher
        data_source = "seed"
        # 电价来源由 price_mode 控制（SPEC_SCENARIOS D3/D4）：
        #   tou  = 强制分时电价（不被 RTP 覆盖）
        #   rtp  = 强制实时出清价（无数据则报错）
        #   auto = 有 CSV 数据就用 RTP，无则回退分时
        use_rtp = price_mode == "rtp" or (
            price_mode == "auto" and effective_fetcher is not None
        )
        if effective_fetcher is not None:
            start, end = timestamps[0], timestamps[-1]
            # 真实负荷优先（替代合成负荷）
            if hasattr(effective_fetcher, "fetch_real_load"):
                try:
                    real_load = effective_fetcher.fetch_real_load(start)
                    if real_load and len(real_load) == POINTS_PER_DAY:
                        load = np.array(real_load, dtype=float)
                except Exception:
                    pass  # 回退合成负荷
            try:
                remote_mix = effective_fetcher.fetch_generation_mix(start, end)
            except Exception:
                remote_mix = []
            if remote_mix:
                generation_mix = remote_mix

            if use_rtp:
                # 真实出清价（元/MWh -> 元/kWh）替代合成分时电价
                try:
                    remote_prices = effective_fetcher.fetch_realtime_price(start, end)
                except Exception:
                    remote_prices = []
                if remote_prices and len(remote_prices) == POINTS_PER_DAY:
                    clearing_prices = [p / 1000.0 for p in remote_prices]
                    tariff = tariff.model_copy(
                        update={"price_cny_per_kwh_by_step": clearing_prices}
                    )
                    data_source = "csv-realtime"
                elif price_mode == "rtp":
                    raise ValueError(
                        "price_mode=rtp 但无可用实时出清价数据，请检查 CSV 数据"
                    )

        price_series = tariff.resolve_price_series(POINTS_PER_DAY)

        battery = self._build_battery()
        if battery_override:
            battery = battery.model_copy(
                update={k: v for k, v in battery_override.items() if v is not None}
            )

        # Collect data provenance from fetcher (actual dates vs requested)
        provenance = {}
        if effective_fetcher is not None and hasattr(effective_fetcher, "get_data_provenance"):
            try:
                provenance = effective_fetcher.get_data_provenance(target_date)
            except Exception:
                provenance = {}

        return DispatchInputBundleV2(
            data_version=f"{data_source}-{self.seed}" if data_source == "seed" else data_source,
            site_id=site_id,
            target_date=target_date,
            time_step_minutes=STEP_MINUTES,
            timestamps=timestamps,
            load_forecast_kw=[float(x) for x in load],
            electricity_price_cny_per_kwh=price_series,
            initial_soc_ratio=battery.min_soc_ratio,
            battery=battery,
            generation_mix=generation_mix,
            emission_factors=EmissionFactorLibrary(
                source_label="national-grid-avg-2022",
                calibrated_region=self.REGION,
            ),
            region=self.REGION,
            tariff=tariff,
            data_source_info=provenance,
        )
