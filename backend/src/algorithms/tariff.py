"""分时电费计算：电度 + 需量 + 政府基金 + 力调 + 绿电环境价值。

复用自 v2 TariffCalculator，对齐黄花厂务实际账单结构。
"""
from __future__ import annotations

from core.config import (
    DEMAND_PRICE_CNY_PER_KW_MONTH,
    GOV_FUND_RATE,
    REACTIVE_ADJUST_RATIO,
)


def calculate_tariff(
    grid_import_kw: list[float],
    price_cny_per_kwh: list[float],
    step_minutes: int = 15,
    declared_demand_kw: float | None = None,
    billing_days: float = 30.0,
) -> dict:
    """计算电费。

    口径：
    - 电度电费 = Σ grid(t) × price(t) × Δt
    - 需量电费 = max(实际最大需量, 申报需量) × 需量单价 (元/kW/月)
    - 政府基金 = 总电量 × gov_fund_rate
    - 力调电费 = (电度+需量) × reactive_adjust_ratio
    """
    n = len(grid_import_kw)
    if n == 0 or len(price_cny_per_kwh) != n:
        raise ValueError("grid_import_kw and price_cny_per_kwh must be non-empty and equal length")
    if step_minutes <= 0 or billing_days <= 0:
        raise ValueError("step_minutes and billing_days must be positive")
    if any(float(power) < 0 for power in grid_import_kw):
        raise ValueError("grid_import_kw cannot be negative; export must be accounted separately")
    dt_hours = step_minutes / 60.0

    energy_cost = 0.0
    total_kwh = 0.0
    for power, price in zip(grid_import_kw, price_cny_per_kwh, strict=True):
        kwh = float(power) * dt_hours
        total_kwh += kwh
        energy_cost += kwh * float(price)

    peak_demand = max((float(p) for p in grid_import_kw), default=0.0)
    effective_demand = max(peak_demand, declared_demand_kw or 0.0)
    monthly_demand_cost = effective_demand * DEMAND_PRICE_CNY_PER_KW_MONTH
    horizon_days = n * step_minutes / (24 * 60)
    demand_allocation_ratio = min(1.0, horizon_days / billing_days)
    demand_cost = monthly_demand_cost * demand_allocation_ratio

    gov_fund_cost = total_kwh * GOV_FUND_RATE
    reactive_adjustment = (energy_cost + demand_cost) * REACTIVE_ADJUST_RATIO

    total_cost = energy_cost + demand_cost + gov_fund_cost + reactive_adjustment
    effective_price = total_cost / total_kwh if total_kwh > 0 else 0.0

    return {
        "energy_cost_cny": round(energy_cost, 2),
        "demand_cost_cny": round(demand_cost, 2),
        "monthly_demand_cost_cny": round(monthly_demand_cost, 2),
        "demand_allocation_ratio": round(demand_allocation_ratio, 6),
        "gov_fund_cost_cny": round(gov_fund_cost, 2),
        "reactive_adjustment_cny": round(reactive_adjustment, 2),
        "total_cost_cny": round(total_cost, 2),
        "total_kwh": round(total_kwh, 1),
        "peak_demand_kw": round(peak_demand, 1),
        "effective_price_cny_per_kwh": round(effective_price, 6),
        "billing_scope": "horizon variable costs plus prorated monthly demand charge",
    }
