"""HVAC 优化器：冷机群调度 + 冷水温度控制。

基于实际冷机调研数据（黄花园区），优化冷机群启停策略：
- 根据制冷负荷需求确定冷机投运台数
- 计算各时段 COP、功率、供回水温度
- 考虑冰蓄冷策略：低谷电价时段蓄冰、高峰时段融冰供冷
"""
from __future__ import annotations

import math
from typing import Any

from core.config import HVAC_DEFAULTS


# 冷机参数：5 个冷冻站，每站多台冷机
CHILLER_STATIONS = [
    {"name": "第一冷冻站", "total_rated_kw": 28128, "chiller_count": 8, "per_unit_kw": 3516},
    {"name": "第二冷冻站", "total_rated_kw": 14068, "chiller_count": 4, "per_unit_kw": 3517},
    {"name": "第三冷冻站", "total_rated_kw": 24702, "chiller_count": 5, "per_unit_kw": 4940},
    {"name": "第四冷冻站", "total_rated_kw": 7034, "chiller_count": 1, "per_unit_kw": 7034},
    {"name": "第五冷冻站", "total_rated_kw": 16878, "chiller_count": 4, "per_unit_kw": 4220},
]
TOTAL_RATED_KW = sum(s["total_rated_kw"] for s in CHILLER_STATIONS)


def optimize_hvac_dispatch(
    cooling_load_kw: list[float],
    outdoor_temp_c: list[float],
    price_cny_per_kwh: list[float],
    tariff_periods: list[str],
) -> dict[str, Any]:
    """优化冷机群调度。

    策略：
    1. 根据制冷负荷确定需要投运的冷机比例
    2. 低谷时段提前蓄冷（降低供水温度设定值）
    3. 高峰时段利用蓄冷（减少冷机投运台数）
    4. 计算 COP、功率、供回水温度
    """
    n = len(cooling_load_kw)

    power_kw = []
    active_chillers = []
    cop_series = []
    supply_temp = []
    return_temp = []
    ice_storage_level = []  # 冰蓄冷量 (kWh equivalent)
    daily_ice = 0.0
    max_ice_capacity = 5000.0  # kWh

    for t in range(n):
        load = max(0, cooling_load_kw[t])
        temp = outdoor_temp_c[t]
        period = tariff_periods[t]

        # COP 随室外温度变化：温度越高 COP 越低
        cop = HVAC_DEFAULTS["cop_nominal"] - max(0, (temp - 25)) * 0.08
        cop = max(HVAC_DEFAULTS["cop_min"], cop)

        # 低谷时段蓄冰
        if period == "valley" and daily_ice < max_ice_capacity:
            charge_power = min(800, (max_ice_capacity - daily_ice) / 0.25)  # 15min
            daily_ice += charge_power * 0.25
            extra_load = charge_power
        else:
            charge_power = 0
            extra_load = 0

        # 高峰/尖峰时段融冰
        if period in ("peak", "sharp") and daily_ice > 0:
            discharge_power = min(daily_ice / 0.25, load * 0.3)  # 融冰最多承担30%负荷
            daily_ice -= discharge_power * 0.25
            effective_load = load - discharge_power
        else:
            effective_load = load

        total_required = effective_load + extra_load

        # 冷机功率 = 制冷负荷 / COP
        elec_power = total_required / cop

        # 投运冷机数
        ratio = min(1.0, total_required / TOTAL_RATED_KW)
        n_chillers = math.ceil(ratio * sum(s["chiller_count"] for s in CHILLER_STATIONS))

        # 供水温度：负荷率越高温度越低
        supply_t = HVAC_DEFAULTS["chilled_water_temp_setpoint_c"] - ratio * 1.5
        return_t = supply_t + 5.0 + (temp - 25) * 0.1

        power_kw.append(round(elec_power, 1))
        active_chillers.append(n_chillers)
        cop_series.append(round(cop, 2))
        supply_temp.append(round(supply_t, 1))
        return_temp.append(round(return_t, 1))
        ice_storage_level.append(round(daily_ice, 1))

    # 电费对比
    dt_h = 0.25
    baseline_cost = sum(cooling_load_kw[t] / HVAC_DEFAULTS["cop_nominal"] * price_cny_per_kwh[t] * dt_h for t in range(n))
    optimized_cost = sum(power_kw[t] * price_cny_per_kwh[t] * dt_h for t in range(n))

    return {
        "power_kw": power_kw,
        "active_chillers": active_chillers,
        "cop": cop_series,
        "supply_temp_c": supply_temp,
        "return_temp_c": return_temp,
        "ice_storage_kwh": ice_storage_level,
        "total_chillers": sum(s["chiller_count"] for s in CHILLER_STATIONS),
        "total_rated_kw": TOTAL_RATED_KW,
        "baseline_cost_cny": round(baseline_cost, 2),
        "optimized_cost_cny": round(optimized_cost, 2),
        "saving_cny": round(baseline_cost - optimized_cost, 2),
        "avg_cop": round(sum(cop_series) / len(cop_series), 2) if cop_series else 0,
    }


def get_chiller_topology() -> list[dict]:
    """返回冷机拓扑结构，供前端展示。"""
    return CHILLER_STATIONS
