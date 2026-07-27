from __future__ import annotations

from datetime import datetime

from energy_agent_v2.contracts import TariffPeriod, TariffResult, TariffSchedule


class TariffCalculator:
    """分时电费计算器：电度电费 + 需量电费 + 政府基金 + 力调 + 绿电环境价值。

    计算口径（对齐黄花厂务实际账单结构）：
    - 电度电费 = Σ grid(t) × price(t) × Δt（所有电量含绿电统一按分时计费）
    - 需量电费 = max(实际最大需量, 申报需量) × 需量单价（元/kW/月）
    - 政府基金 = 总电量 × gov_fund_rate（固定 0.04625 元/kWh）
    - 力调电费 = (电度 + 需量) × reactive_adjust_ratio（默认 -0.75% 奖励）
    - 绿电环境价值 = 绿电电量 × 环境价值单价（额外附加，绿电电度不重复计费）
    - 总费用 = 电度 + 需量 + 政府基金 + 力调 + 绿电环境价值
    - 综合度电成本 = 总费用 / 总电量
    """

    def calculate(
        self,
        tariff: TariffSchedule,
        grid_import_kw: list[float],
        timestamps: list[datetime],
        step_minutes: int,
    ) -> TariffResult:
        n = len(timestamps)
        if n == 0:
            raise ValueError("timestamps 不能为空")
        if len(grid_import_kw) != n:
            raise ValueError(
                f"grid_import_kw 长度({len(grid_import_kw)})与时间轴({n})不一致"
            )

        price_series = tariff.resolve_price_series(n)
        dt_hours = step_minutes / 60.0

        green_ratio = float(tariff.green_power_ratio or 0.0)
        green_env_price = float(tariff.green_power_price_cny_per_kwh) if tariff.green_power_price_cny_per_kwh is not None else 0.0

        energy_cost = 0.0
        total_kwh = 0.0
        green_kwh = 0.0
        for power, price in zip(grid_import_kw, price_series):
            p = float(power)
            kwh = p * dt_hours
            total_kwh += kwh
            # 所有电量（含绿电）统一按分时电价计费
            energy_cost += kwh * float(price)
            green_kwh += kwh * green_ratio

        # 绿电环境价值费用（额外附加，绿电电度已含在分时电度中，不重复计费）
        green_cost = green_kwh * green_env_price

        peak_demand = max((float(p) for p in grid_import_kw), default=0.0)
        declared = float(tariff.declared_demand_kw or 0.0)
        effective_demand = max(peak_demand, declared)
        demand_cost = effective_demand * float(tariff.demand_price_cny_per_kw_month)

        # V2.1: 政府基金及附加（按总电量计，固定费率 0.04625 元/kWh）
        gov_fund_cost = total_kwh * float(tariff.gov_fund_rate_cny_per_kwh)

        # V2.1: 力调电费 =（时段电度 + 基本电费）× reactive_adjust_ratio
        reactive_adjustment = (energy_cost + demand_cost) * float(tariff.reactive_adjust_ratio)

        # 种子分时规则未单独建模固定基本电费（需量电费已涵盖按需计费部分）
        basic_fee = 0.0
        total_cost = energy_cost + demand_cost + basic_fee + gov_fund_cost + reactive_adjustment + green_cost
        effective_price = total_cost / total_kwh if total_kwh > 0 else 0.0

        period_labels = self._derive_period_labels(tariff, price_series, n)

        return TariffResult(
            timestamps=list(timestamps),
            price_cny_per_kwh=[float(p) for p in price_series],
            period_labels=period_labels,
            energy_cost_cny=round(energy_cost, 4),
            demand_cost_cny=round(demand_cost, 4),
            basic_fee_cny=basic_fee,
            gov_fund_cost_cny=round(gov_fund_cost, 4),
            reactive_adjustment_cny=round(reactive_adjustment, 4),
            green_power_cost_cny=round(green_cost, 4),
            total_cost_cny=round(total_cost, 4),
            peak_demand_kw=round(peak_demand, 4),
            effective_price_cny_per_kwh=round(effective_price, 6),
        )

    @staticmethod
    def _derive_period_labels(
        tariff: TariffSchedule, price_series: list[float], n: int
    ) -> list[str]:
        """优先用 period_map 推导；无 period_map 时由价格反推时段标签。"""
        if tariff.period_map is not None and len(tariff.period_map) == n:
            return [p.value for p in tariff.period_map]
        price_to_label = {
            tariff.price_sharp_cny_per_kwh: TariffPeriod.SHARP.value,
            tariff.price_peak_cny_per_kwh: TariffPeriod.PEAK.value,
            tariff.price_flat_cny_per_kwh: TariffPeriod.FLAT.value,
            tariff.price_valley_cny_per_kwh: TariffPeriod.VALLEY.value,
        }
        return [price_to_label.get(float(p), TariffPeriod.FLAT.value) for p in price_series]
