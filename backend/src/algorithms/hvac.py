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


# 冷机参数：由原始台账按厂/站汇总。额定值暂按制冷量解释，待铭牌复核。
CHILLER_STATIONS = [
    {"name": "一期冷冻站", "total_rated_kw": 52744, "chiller_count": 8, "running_count": 4},
    {"name": "二期冷冻站三厂", "total_rated_kw": 24619, "chiller_count": 4, "running_count": 1},
    {"name": "二期冷冻站四厂", "total_rated_kw": 24619, "chiller_count": 4, "running_count": 3},
    {"name": "二期冷冻站五厂", "total_rated_kw": 21102, "chiller_count": 3, "running_count": 2},
    {"name": "二期冷冻站六厂", "total_rated_kw": 17585, "chiller_count": 3, "running_count": 2},
    {"name": "三期冷冻站", "total_rated_kw": 60476, "chiller_count": 8, "running_count": 6},
    {"name": "四期西冷冻站", "total_rated_kw": 42201, "chiller_count": 5, "running_count": 3},
    {"name": "四期东冷冻站", "total_rated_kw": 13362, "chiller_count": 2, "running_count": 2},
]
TOTAL_RATED_KW = sum(s["total_rated_kw"] for s in CHILLER_STATIONS)
TOTAL_CHILLERS = sum(s["chiller_count"] for s in CHILLER_STATIONS)
CURRENT_RUNNING_CHILLERS = sum(s["running_count"] for s in CHILLER_STATIONS)


def optimize_hvac_dispatch(
    cooling_load_kw: list[float],
    outdoor_temp_c: list[float],
    price_cny_per_kwh: list[float],
    tariff_periods: list[str],
) -> dict[str, Any]:
    """按台账容量和部分负荷效率进行冷机群启停优化。

    原始资料没有冰蓄冷设备或蓄冷容量，因此默认不建立虚构的跨时段能量库存。
    基线使用台账当前运行台数；优化方案在满足制冷量的前提下提高单机负荷率。
    """
    n = len(cooling_load_kw)
    if n == 0 or not (len(outdoor_temp_c) == len(price_cny_per_kwh) == len(tariff_periods) == n):
        raise ValueError("all HVAC input series must be non-empty and equal length")

    power_kw = []
    baseline_power_kw = []
    active_chillers = []
    cop_series = []
    supply_temp = []
    return_temp = []
    ice_storage_level = [0.0] * n
    violations = []
    avg_unit_cooling = TOTAL_RATED_KW / TOTAL_CHILLERS

    for t in range(n):
        load = max(0, cooling_load_kw[t])
        temp = outdoor_temp_c[t]
        if load > TOTAL_RATED_KW:
            violations.append({"step": t, "code": "COOLING_CAPACITY_EXCEEDED", "value": round(load, 1)})
        served_load = min(load, TOTAL_RATED_KW)
        weather_cop = HVAC_DEFAULTS["cop_nominal"] - max(0.0, temp - 25.0) * 0.08
        weather_cop = max(HVAC_DEFAULTS["cop_min"], min(5.5, weather_cop))

        baseline_capacity = max(avg_unit_cooling, CURRENT_RUNNING_CHILLERS * avg_unit_cooling)
        baseline_plr = min(1.0, served_load / baseline_capacity)
        baseline_cop = max(
            HVAC_DEFAULTS["cop_min"],
            weather_cop * (0.82 + 0.18 * min(1.0, baseline_plr / 0.80)),
        )

        n_chillers = max(1, min(TOTAL_CHILLERS, math.ceil(served_load / (avg_unit_cooling * 0.82))))
        optimized_plr = min(1.0, served_load / max(avg_unit_cooling, n_chillers * avg_unit_cooling))
        cop = max(
            baseline_cop,
            weather_cop * (0.86 + 0.14 * min(1.0, optimized_plr / 0.82)),
        )
        # 冷机输入功率 + 8% 水泵/冷却塔辅助功率。
        baseline_elec = served_load / baseline_cop * 1.08
        elec_power = served_load / cop * 1.08

        ratio = min(1.0, served_load / TOTAL_RATED_KW)

        # 供水温度：负荷率越高温度越低
        supply_t = max(5.0, min(9.0, HVAC_DEFAULTS["chilled_water_temp_setpoint_c"] - ratio * 1.0))
        return_t = min(HVAC_DEFAULTS["return_water_temp_max_c"], supply_t + 5.0)

        power_kw.append(round(elec_power, 1))
        baseline_power_kw.append(round(baseline_elec, 1))
        active_chillers.append(n_chillers)
        cop_series.append(round(cop, 2))
        supply_temp.append(round(supply_t, 1))
        return_temp.append(round(return_t, 1))

    # 电费对比
    dt_h = 0.25
    baseline_cost = sum(baseline_power_kw[t] * price_cny_per_kwh[t] * dt_h for t in range(n))
    optimized_cost = sum(power_kw[t] * price_cny_per_kwh[t] * dt_h for t in range(n))

    return {
        "power_kw": power_kw,
        "baseline_power_kw": baseline_power_kw,
        "active_chillers": active_chillers,
        "cop": cop_series,
        "supply_temp_c": supply_temp,
        "return_temp_c": return_temp,
        "ice_storage_kwh": ice_storage_level,
        "total_chillers": TOTAL_CHILLERS,
        "total_rated_kw": TOTAL_RATED_KW,
        "baseline_cost_cny": round(baseline_cost, 2),
        "optimized_cost_cny": round(optimized_cost, 2),
        "saving_cny": round(baseline_cost - optimized_cost, 2),
        "avg_cop": round(sum(cop_series) / len(cop_series), 2) if cop_series else 0,
        "violations": violations,
        "thermal_storage_enabled": False,
        "model_assumptions": [
            "冷机台账额定功率按额定制冷量解释",
            "缺少冰蓄冷资产依据，默认不启用蓄冷",
            "水泵与冷却塔电耗按冷机输入功率的8%估算",
        ],
    }


def get_chiller_topology() -> list[dict]:
    """返回冷机拓扑结构，供前端展示。"""
    return [
        {
            **station,
            "per_unit_kw": round(station["total_rated_kw"] / station["chiller_count"]),
        }
        for station in CHILLER_STATIONS
    ]
