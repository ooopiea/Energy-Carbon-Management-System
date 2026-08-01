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

from core.config import STORAGE_DEFAULTS

_TOL = 1e-3


def optimize_storage_dispatch(
    load_kw: list[float],
    price_cny_per_kwh: list[float],
    carbon_factors: list[float] | None = None,
    battery_config: dict | None = None,
    objective: str = "min_cost",
    initial_soc: float = 0.5,
) -> dict[str, Any]:
    """优化储能充放电调度。"""
    cfg = {**STORAGE_DEFAULTS, **(battery_config or {})}
    n = len(load_kw)
    dt_h = 15 / 60.0

    if not _HAS_PULP:
        return _heuristic_dispatch(load_kw, price_cny_per_kwh, cfg, initial_soc, dt_h)

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
    soc = [pulp.LpVariable(f"soc_{t}", min_soc, max_soc) for t in range(n)]
    temp = [pulp.LpVariable(f"temp_{t}", 0, max_temp) for t in range(n)]
    peak = pulp.LpVariable("peak", 0)

    for t in range(n):
        prev = initial_soc if t == 0 else soc[t - 1]
        prob += soc[t] == prev + charge_coef * p_ch[t] - discharge_coef * p_dis[t]

    for t in range(n):
        prev_p = 0 if t == 0 else p_dis[t - 1] - p_ch[t - 1]
        cur_p = p_dis[t] - p_ch[t]
        prob += cur_p - prev_p <= max_ramp
        prob += prev_p - cur_p <= max_ramp

    for t in range(n):
        prob += p_dis[t] - p_ch[t] <= load_kw[t]

    for t in range(n):
        prev_t = t_amb if t == 0 else temp[t - 1]
        prob += temp[t] == t_amb + (prev_t - t_amb) * alpha + (p_ch[t] + p_dis[t]) * r_th * (1 - alpha)

    prob += soc[n - 1] == min_soc

    if objective == "min_cost":
        prob += pulp.lpSum((-p_dis[t] + p_ch[t]) * price_cny_per_kwh[t] * dt_h for t in range(n))
    elif objective == "min_carbon":
        prob += pulp.lpSum((-p_dis[t] + p_ch[t]) * cf[t] * dt_h for t in range(n))
    elif objective == "limit_peak":
        for t in range(n):
            prob += load_kw[t] - p_dis[t] + p_ch[t] <= peak
        prob += peak
    else:
        for t in range(n):
            prob += load_kw[t] - p_dis[t] + p_ch[t] <= peak
        prob += pulp.lpSum((load_kw[t] - p_dis[t] + p_ch[t]) * price_cny_per_kwh[t] * dt_h for t in range(n))

    t0 = time.perf_counter()
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=30)
    prob.solve(solver)
    solve_ms = int((time.perf_counter() - t0) * 1000)

    status = pulp.LpStatus[prob.status]
    if prob.status != pulp.LpStatusOptimal:
        return _heuristic_dispatch(load_kw, price_cny_per_kwh, cfg, initial_soc, dt_h)

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

    grid = [load_kw[t] - power[t] for t in range(n)]
    baseline_cost = sum(load_kw[t] * price_cny_per_kwh[t] * dt_h for t in range(n))
    optimized_cost = sum(grid[t] * price_cny_per_kwh[t] * dt_h for t in range(n))

    violations = _verify_constraints(soc_v, temp_v, cfg, max_ramp, min_soc)

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
        "baseline_peak_kw": round(max(load_kw), 1),
        "optimized_peak_kw": round(max(grid), 1),
        "peak_reduction_kw": round(max(load_kw) - max(grid), 1),
        "terminal_soc": round(soc_v[-1], 4),
        "max_temp_c": round(max(temp_v), 2),
        "violations": violations,
        "solver_status": status,
        "solve_ms": solve_ms,
        "objective": objective,
    }


def _heuristic_dispatch(load, price, cfg, init_soc, dt_h):
    """PuLP 不可用时的启发式调度：谷充峰放。"""
    n = len(load)
    power, soc_v, temp_v = [], [], []
    s = init_soc
    price_sorted = sorted(range(n), key=lambda t: price[t])
    charge_steps = set(price_sorted[: n // 4])
    discharge_steps = set(price_sorted[3 * n // 4 :])

    for t in range(n):
        p_ch = p_dis = 0
        if t in charge_steps and s < cfg["max_soc_ratio"]:
            p_ch = min(cfg["max_charge_power_kw"], (cfg["max_soc_ratio"] - s) * cfg["capacity_kwh"] / dt_h)
        elif t in discharge_steps and s > cfg["min_soc_ratio"]:
            p_dis = min(cfg["max_discharge_power_kw"], (s - cfg["min_soc_ratio"]) * cfg["capacity_kwh"] / dt_h, load[t])
        power.append(p_dis - p_ch)
        s += dt_h * cfg["charge_efficiency_ratio"] * p_ch / cfg["capacity_kwh"] - dt_h * p_dis / (cfg["discharge_efficiency_ratio"] * cfg["capacity_kwh"])
        soc_v.append(s)
        temp_v.append(cfg["ambient_temperature_c"] + (p_ch + p_dis) * cfg["thermal_resistance_c_per_kw"])

    grid = [load[t] - power[t] for t in range(n)]
    baseline_cost = sum(load[t] * price[t] * dt_h for t in range(n))
    optimized_cost = sum(grid[t] * price[t] * dt_h for t in range(n))
    return {
        "power_kw": [round(p, 1) for p in power],
        "p_ch_kw": [0.0] * n,
        "p_dis_kw": [0.0] * n,
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
        "violations": [],
        "solver_status": "heuristic",
        "solve_ms": 0,
        "objective": "min_cost",
    }


def _verify_constraints(soc, temp, cfg, max_ramp, min_soc):
    violations = []
    for t in range(len(soc)):
        if soc[t] < min_soc - _TOL:
            violations.append({"step": t, "code": "SOC_BELOW_MIN", "value": round(soc[t], 4)})
        if soc[t] > cfg["max_soc_ratio"] + _TOL:
            violations.append({"step": t, "code": "SOC_ABOVE_MAX", "value": round(soc[t], 4)})
        if temp[t] > cfg["max_cell_temperature_c"] + _TOL:
            violations.append({"step": t, "code": "TEMP_EXCEEDED", "value": round(temp[t], 2)})
    return violations
