"""MILP 储能优化器：基于 PuLP 的日前/实时调度优化。

复用 v2 MILPStorageOptimizer 核心逻辑，简化为纯函数式接口。
约束：SOC范围、爬坡率、热安全、禁止向电网送电、末端SOC。
"""
from __future__ import annotations

import math
import time
from typing import Any

try:
    import pulp
    _HAS_PULP = True
except ImportError:
    _HAS_PULP = False

from core.config import (
    DEMAND_PRICE_CNY_PER_KW_MONTH,
    SIM_STEP_MINUTES,
    STORAGE_DEFAULTS,
)

_TOL = 1e-3


def optimize_storage_dispatch(
    load_kw: list[float],
    price_cny_per_kwh: list[float],
    carbon_factors: list[float] | None = None,
    battery_config: dict | None = None,
    objective: str = "min_cost",
    initial_soc: float = 0.5,
    terminal_soc: float | None = None,
    solar_kw: list[float] | None = None,
    billing_peak_floor_kw: float = 0.0,
    demand_price_cny_per_kw_month: float = DEMAND_PRICE_CNY_PER_KW_MONTH,
) -> dict[str, Any]:
    """优化储能充放电调度。

    ``min_cost`` 同时考虑电度电费与本日可能形成的月最大需量。默认末端 SOC
    回到初始 SOC，避免把消耗期初库存误报为节省。光伏按自发自用从园区负荷中扣除。
    """
    cfg = {**STORAGE_DEFAULTS, **(battery_config or {})}
    n = len(load_kw)
    if n == 0 or len(price_cny_per_kwh) != n:
        raise ValueError("load_kw and price_cny_per_kwh must be non-empty and equal length")
    if carbon_factors is not None and len(carbon_factors) != n:
        raise ValueError("carbon_factors length must match load_kw")
    solar = solar_kw or [0.0] * n
    if len(solar) != n:
        raise ValueError("solar_kw length must match load_kw")
    if not cfg["min_soc_ratio"] <= initial_soc <= cfg["max_soc_ratio"]:
        raise ValueError("initial_soc is outside configured SOC bounds")
    target_soc = initial_soc if terminal_soc is None else terminal_soc
    if not cfg["min_soc_ratio"] <= target_soc <= cfg["max_soc_ratio"]:
        raise ValueError("terminal_soc is outside configured SOC bounds")
    dt_h = SIM_STEP_MINUTES / 60.0
    base_grid = [max(0.0, float(load_kw[t]) - float(solar[t])) for t in range(n)]

    if not _HAS_PULP:
        return _heuristic_dispatch(base_grid, price_cny_per_kwh, cfg, initial_soc, target_soc, dt_h)

    capacity = cfg["capacity_kwh"]
    min_soc = cfg["min_soc_ratio"]
    max_soc = cfg["max_soc_ratio"]
    max_charge = cfg["max_charge_power_kw"]
    max_discharge = cfg["max_discharge_power_kw"]
    max_ramp = cfg["max_ramp_kw_per_step"]
    max_temp = cfg["max_cell_temperature_c"]
    eta_ch = cfg["charge_efficiency_ratio"]
    eta_dis = cfg["discharge_efficiency_ratio"]
    t_amb = cfg["ambient_temperature_c"]
    r_th = cfg["thermal_resistance_c_per_kw"]
    tau_th = cfg["thermal_time_constant_min"]

    charge_coef = dt_h * eta_ch / capacity
    discharge_coef = dt_h / (eta_dis * capacity)
    alpha = math.exp(-15 / tau_th)
    cf = carbon_factors or [0.5366] * n

    prob = pulp.LpProblem("storage", pulp.LpMinimize)
    p_ch = [pulp.LpVariable(f"ch_{t}", 0, max_charge) for t in range(n)]
    p_dis = [pulp.LpVariable(f"dis_{t}", 0, max_discharge) for t in range(n)]
    charge_mode = [pulp.LpVariable(f"charge_mode_{t}", cat="Binary") for t in range(n)]
    soc = [pulp.LpVariable(f"soc_{t}", min_soc, max_soc) for t in range(n)]
    temp = [pulp.LpVariable(f"temp_{t}", 0, max_temp) for t in range(n)]
    peak = pulp.LpVariable("peak", max(0.0, billing_peak_floor_kw))

    for t in range(n):
        prob += p_ch[t] <= max_charge * charge_mode[t]
        prob += p_dis[t] <= max_discharge * (1 - charge_mode[t])
        prev = initial_soc if t == 0 else soc[t - 1]
        prob += soc[t] == prev + charge_coef * p_ch[t] - discharge_coef * p_dis[t]

    for t in range(n):
        prev_p = 0 if t == 0 else p_dis[t - 1] - p_ch[t - 1]
        cur_p = p_dis[t] - p_ch[t]
        prob += cur_p - prev_p <= max_ramp
        prob += prev_p - cur_p <= max_ramp

    for t in range(n):
        prob += p_dis[t] - p_ch[t] <= base_grid[t]
        prob += base_grid[t] - p_dis[t] + p_ch[t] <= peak

    for t in range(n):
        prev_t = t_amb if t == 0 else temp[t - 1]
        prob += temp[t] == t_amb + (prev_t - t_amb) * alpha + (p_ch[t] + p_dis[t]) * r_th * (1 - alpha)

    prob += soc[n - 1] == target_soc

    max_cycles = float(cfg.get("max_equivalent_full_cycles", 0) or 0)
    if max_cycles > 0:
        prob += pulp.lpSum((p_ch[t] + p_dis[t]) * dt_h for t in range(n)) <= 2 * capacity * max_cycles

    energy_objective = pulp.lpSum(
        (base_grid[t] - p_dis[t] + p_ch[t]) * price_cny_per_kwh[t] * dt_h
        for t in range(n)
    )
    demand_objective = peak * max(0.0, demand_price_cny_per_kw_month)

    if objective == "min_cost":
        prob += energy_objective + demand_objective
    elif objective == "min_carbon":
        prob += pulp.lpSum((-p_dis[t] + p_ch[t]) * cf[t] * dt_h for t in range(n))
    elif objective == "limit_peak":
        prob += peak
    elif objective == "combined":
        prob += energy_objective + demand_objective
    else:
        raise ValueError(f"unsupported objective: {objective}")

    t0 = time.perf_counter()
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=30)
    prob.solve(solver)
    solve_ms = int((time.perf_counter() - t0) * 1000)

    status = pulp.LpStatus[prob.status]
    if prob.status != pulp.LpStatusOptimal:
        return _heuristic_dispatch(base_grid, price_cny_per_kwh, cfg, initial_soc, target_soc, dt_h)

    p_ch_v = [float(pulp.value(v) or 0) for v in p_ch]
    p_dis_v = [float(pulp.value(v) or 0) for v in p_dis]
    power = [p_dis_v[t] - p_ch_v[t] for t in range(n)]

    soc_v, temp_v = [], []
    s, th = initial_soc, t_amb
    for t in range(n):
        s = s + charge_coef * p_ch_v[t] - discharge_coef * p_dis_v[t]
        th = t_amb + (th - t_amb) * alpha + (p_ch_v[t] + p_dis_v[t]) * r_th * (1 - alpha)
        soc_v.append(s)
        temp_v.append(th)

    grid = [base_grid[t] - power[t] for t in range(n)]
    baseline_energy_cost = sum(base_grid[t] * price_cny_per_kwh[t] * dt_h for t in range(n))
    optimized_energy_cost = sum(grid[t] * price_cny_per_kwh[t] * dt_h for t in range(n))
    baseline_peak = max(max(base_grid), billing_peak_floor_kw)
    optimized_peak = max(max(grid), billing_peak_floor_kw)
    baseline_demand_cost = baseline_peak * max(0.0, demand_price_cny_per_kw_month)
    optimized_demand_cost = optimized_peak * max(0.0, demand_price_cny_per_kw_month)
    baseline_cost = baseline_energy_cost + baseline_demand_cost
    optimized_cost = optimized_energy_cost + optimized_demand_cost

    violations = _verify_constraints(
        soc_v, temp_v, power, p_ch_v, p_dis_v, cfg, max_ramp, target_soc
    )

    return {
        "power_kw": [round(p, 1) for p in power],
        "p_ch_kw": [round(p, 1) for p in p_ch_v],
        "p_dis_kw": [round(p, 1) for p in p_dis_v],
        "soc_ratio": [round(s, 4) for s in soc_v],
        "temp_c": [round(t, 2) for t in temp_v],
        "grid_kw": [round(g, 1) for g in grid],
        "baseline_cost_cny": round(baseline_cost, 2),
        "optimized_cost_cny": round(optimized_cost, 2),
        "saving_cny": round(baseline_cost - optimized_cost, 2),
        "baseline_energy_cost_cny": round(baseline_energy_cost, 2),
        "optimized_energy_cost_cny": round(optimized_energy_cost, 2),
        "baseline_demand_cost_cny": round(baseline_demand_cost, 2),
        "optimized_demand_cost_cny": round(optimized_demand_cost, 2),
        "baseline_peak_kw": round(max(base_grid), 1),
        "optimized_peak_kw": round(max(grid), 1),
        "peak_reduction_kw": round(max(base_grid) - max(grid), 1),
        "terminal_soc": round(soc_v[-1], 4),
        "max_temp_c": round(max(temp_v), 2),
        "violations": violations,
        "solver_status": status,
        "solve_ms": solve_ms,
        "objective": objective,
        "terminal_soc_target": round(target_soc, 4),
        "capacity_kwh": capacity,
        "rated_power_kw": max(max_charge, max_discharge),
        "cost_scope": "energy_day_plus_candidate_monthly_demand_peak",
    }


def _heuristic_dispatch(load, price, cfg, init_soc, target_soc, dt_h):
    """求解器不可用时安全降级为不动作，不伪造节费或违反 SOC。"""
    n = len(load)
    power = [0.0] * n
    soc_v = [init_soc] * n
    temp_v = [cfg["ambient_temperature_c"]] * n
    grid = list(load)
    baseline_cost = sum(load[t] * price[t] * dt_h for t in range(n))
    demand_cost = max(load) * DEMAND_PRICE_CNY_PER_KW_MONTH
    baseline_cost += demand_cost
    optimized_cost = baseline_cost
    return {
        "power_kw": [round(p, 1) for p in power],
        "p_ch_kw": list(power),
        "p_dis_kw": list(power),
        "soc_ratio": [round(s, 4) for s in soc_v],
        "temp_c": [round(t, 2) for t in temp_v],
        "grid_kw": [round(g, 1) for g in grid],
        "baseline_cost_cny": round(baseline_cost, 2),
        "optimized_cost_cny": round(optimized_cost, 2),
        "saving_cny": round(baseline_cost - optimized_cost, 2),
        "baseline_peak_kw": round(max(load), 1),
        "optimized_peak_kw": round(max(grid), 1),
        "peak_reduction_kw": round(max(load) - max(grid), 1),
        "terminal_soc": round(soc_v[-1], 4),
        "max_temp_c": round(max(temp_v), 2),
        "violations": ([{"code": "TERMINAL_TARGET_UNAVAILABLE_WITHOUT_SOLVER"}]
                       if abs(target_soc - init_soc) > _TOL else []),
        "solver_status": "safe_noop_fallback",
        "solve_ms": 0,
        "objective": "min_cost",
    }


def _verify_constraints(soc, temp, power, p_ch, p_dis, cfg, max_ramp, terminal_soc):
    violations = []
    for t in range(len(soc)):
        if soc[t] < cfg["min_soc_ratio"] - _TOL:
            violations.append({"step": t, "code": "SOC_BELOW_MIN", "value": round(soc[t], 4)})
        if soc[t] > cfg["max_soc_ratio"] + _TOL:
            violations.append({"step": t, "code": "SOC_ABOVE_MAX", "value": round(soc[t], 4)})
        if temp[t] > cfg["max_cell_temperature_c"] + _TOL:
            violations.append({"step": t, "code": "TEMP_EXCEEDED", "value": round(temp[t], 2)})
        if p_ch[t] > _TOL and p_dis[t] > _TOL:
            violations.append({"step": t, "code": "SIMULTANEOUS_CHARGE_DISCHARGE"})
        previous_power = 0.0 if t == 0 else power[t - 1]
        if abs(power[t] - previous_power) > max_ramp + _TOL:
            violations.append({"step": t, "code": "RAMP_EXCEEDED", "value": round(power[t] - previous_power, 2)})
    if soc and abs(soc[-1] - terminal_soc) > _TOL:
        violations.append({"step": len(soc) - 1, "code": "TERMINAL_SOC_MISMATCH", "value": round(soc[-1], 4)})
    return violations
