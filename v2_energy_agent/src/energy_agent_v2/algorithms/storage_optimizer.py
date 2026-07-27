"""MILP storage optimizer for V2 campus energy dispatch.

Solves battery charge/discharge dispatch under SOC range, ramp-rate,
thermal-safety, no-export and terminal-SOC constraints using PuLP + CBC.
"""

from __future__ import annotations

import math
import time

import pulp

from energy_agent_v2.contracts import (
    ConstraintCheckResult,
    ConstraintViolation,
    DispatchInputBundleV2,
    DispatchObjective,
    GenerationSource,
    StorageDispatchRequestV2,
    StorageOptimizationResult,
)
from energy_agent_v2.errors import AppError

ALGORITHM_VERSION = "milp-storage-2.0.0"
_AGENT_VERSION = "2.0.0"
_DEFAULT_CARBON_FACTOR = 0.5366
_TOL = 1e-3


class MILPStorageOptimizer:
    """MILP-based battery dispatch optimizer (PuLP + CBC).

    Decision variables (per time step *t*): ``p_dis[t]`` (discharge kW),
    ``p_ch[t]`` (charge kW).  Net battery power ``p[t] = p_dis - p_ch``
    (positive = discharging, negative = charging).  The optimizer never
    charges and discharges simultaneously because round-trip losses and
    thermal penalties make it strictly dominated.
    """

    ALGORITHM_VERSION = ALGORITHM_VERSION

    def __init__(self, agent_version: str = _AGENT_VERSION) -> None:
        self.agent_version = agent_version

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def optimize(self, request: StorageDispatchRequestV2) -> StorageOptimizationResult:
        inputs = request.inputs
        battery = inputs.battery
        revision = request.revision
        objective = request.objective
        if revision and revision.objective is not None:
            objective = revision.objective

        n = len(inputs.timestamps)
        dt_h = inputs.time_step_minutes / 60.0
        load = inputs.load_forecast_kw
        price = inputs.electricity_price_cny_per_kwh
        initial_soc = inputs.initial_soc_ratio

        # -- effective parameters (revision overrides) --
        eff_min_soc = battery.min_soc_ratio
        if revision and revision.reserve_soc_min_ratio is not None:
            eff_min_soc = max(eff_min_soc, revision.reserve_soc_min_ratio)
        # revision 提高 min_soc 时，初始 SOC 也要跟着提到新下限，否则第一步就违反约束
        if initial_soc < eff_min_soc:
            initial_soc = eff_min_soc

        eff_max_discharge = battery.max_discharge_power_kw
        if revision and revision.max_discharge_power_kw is not None:
            eff_max_discharge = min(eff_max_discharge, revision.max_discharge_power_kw)

        # 每天清零：末端 SOC 必须回到物理最低值（eff_min_soc）
        # revision 可提高末端目标（如工程师要求保留更多备用电量）
        terminal_target = max(
            eff_min_soc,
            revision.terminal_soc_min_ratio if revision and revision.terminal_soc_min_ratio is not None else eff_min_soc,
        )

        blocked_steps: set[int] = set()
        if revision:
            for iv in revision.blocked_intervals:
                for t in range(iv.start_index, iv.end_index + 1):
                    if 0 <= t < n:
                        blocked_steps.add(t)

        # -- model coefficients --
        capacity = battery.capacity_kwh
        charge_coef = dt_h * battery.charge_efficiency_ratio / capacity
        discharge_coef = dt_h / (battery.discharge_efficiency_ratio * capacity)

        alpha = math.exp(-inputs.time_step_minutes / battery.thermal_time_constant_min)
        t_amb = battery.ambient_temperature_c
        r_th = battery.thermal_resistance_c_per_kw

        carbon_factors = self._resolve_carbon_factors(inputs, n)

        # ---- build LP ----
        prob = pulp.LpProblem("storage_dispatch", pulp.LpMinimize)

        p_ch = [
            pulp.LpVariable(f"pch_{t}", lowBound=0, upBound=battery.max_charge_power_kw)
            for t in range(n)
        ]
        p_dis = [
            pulp.LpVariable(f"pdis_{t}", lowBound=0, upBound=eff_max_discharge)
            for t in range(n)
        ]
        soc = [
            pulp.LpVariable(f"soc_{t}", lowBound=eff_min_soc, upBound=battery.max_soc_ratio)
            for t in range(n)
        ]
        temp = [
            pulp.LpVariable(f"temp_{t}", lowBound=0, upBound=battery.max_cell_temperature_c)
            for t in range(n)
        ]
        peak = pulp.LpVariable("peak", lowBound=0)

        # SOC dynamics (linearised)
        for t in range(n):
            prev = initial_soc if t == 0 else soc[t - 1]
            prob += soc[t] == prev + charge_coef * p_ch[t] - discharge_coef * p_dis[t]

        # ramp rate |p[t]-p[t-1]| <= max_ramp  (p[-1] := 0)
        for t in range(n):
            prev_p = 0 if t == 0 else p_dis[t - 1] - p_ch[t - 1]
            cur_p = p_dis[t] - p_ch[t]
            prob += cur_p - prev_p <= battery.max_ramp_kw_per_step
            prob += prev_p - cur_p <= battery.max_ramp_kw_per_step

        # no export to grid: grid = load - p_dis + p_ch >= 0
        for t in range(n):
            prob += p_dis[t] - p_ch[t] <= load[t]

        # blocked intervals force both powers to zero
        for t in blocked_steps:
            prob += p_dis[t] + p_ch[t] <= 0

        # V2.2: max_cycles_per_day — 限制日等效满循环数 (EFC)。
        # 原先用 unit-commitment 二进制绑定 p_ch<->z_chg 来数充电段，
        # 但求解器会用 p_ch=epsilon 的微量充电把多段焊成一段绕过约束。
        # 改用纯线性的总充电能量约束：一次满循环 = 充满 capacity*depth_range
        # 再放空。EFC = sum(p_ch)*dt / (capacity * depth_range)，无法作弊。
        if revision and revision.max_cycles_per_day is not None:
            _depth_range = battery.max_soc_ratio - battery.min_soc_ratio
            _max_chg_energy = revision.max_cycles_per_day * capacity * _depth_range
            prob += pulp.lpSum(p_ch[t] for t in range(n)) * dt_h <= _max_chg_energy

        # first-order thermal model
        for t in range(n):
            prev_t = t_amb if t == 0 else temp[t - 1]
            prob += (
                temp[t]
                == t_amb
                + (prev_t - t_amb) * alpha
                + (p_ch[t] + p_dis[t]) * r_th * (1 - alpha)
            )

        # terminal SOC
        prob += soc[n - 1] == terminal_target

        # objective
        if objective == DispatchObjective.MIN_COST:
            prob += pulp.lpSum(
                (-p_dis[t] + p_ch[t]) * price[t] * dt_h for t in range(n)
            )
        elif objective == DispatchObjective.LIMIT_PEAK_DEMAND:
            for t in range(n):
                prob += load[t] - p_dis[t] + p_ch[t] <= peak
            prob += peak
        elif objective == DispatchObjective.WEIGHTED:
            # min(alpha*(电度电费+需量电费) + beta*碳排放)
            # 需量电费：引入 effective_demand = max(peak, declared_demand)
            _tariff = inputs.tariff
            declared = float(_tariff.declared_demand_kw or 0.0) if _tariff else 0.0
            demand_price = float(_tariff.demand_price_cny_per_kw_month) / 30.0 if _tariff else 0.0
            eff_demand = pulp.LpVariable("eff_demand", lowBound=declared)
            for t in range(n):
                prob += load[t] - p_dis[t] + p_ch[t] <= peak
            prob += peak <= eff_demand
            alpha = float(getattr(request, "weight_alpha_cost", 1.0))
            beta = float(getattr(request, "weight_beta_carbon", 0.01))
            energy_term = pulp.lpSum(
                (load[t] - p_dis[t] + p_ch[t]) * price[t] * dt_h for t in range(n)
            )
            carbon_term = pulp.lpSum(
                (load[t] - p_dis[t] + p_ch[t]) * carbon_factors[t] * dt_h for t in range(n)
            )
            prob += alpha * (energy_term + eff_demand * demand_price) + beta * carbon_term
        else:  # MIN_CARBON
            prob += pulp.lpSum(
                (-p_dis[t] + p_ch[t]) * carbon_factors[t] * dt_h for t in range(n)
            )

        # ---- solve ----
        solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=120)
        t0 = time.perf_counter()
        prob.solve(solver)
        solve_ms = int((time.perf_counter() - t0) * 1000)

        status_name = pulp.LpStatus[prob.status]
        if prob.status != pulp.LpStatusOptimal:
            raise AppError(
                code="STORAGE_OPTIMIZATION_INFEASIBLE",
                message=(
                    f"Solver returned status '{status_name}' for objective "
                    f"'{objective.value}'."
                ),
                details={"solver_status": status_name, "objective": objective.value},
            )

        # ---- extract & independently recompute ----
        p_ch_vals = [float(pulp.value(v) or 0.0) for v in p_ch]
        p_dis_vals = [float(pulp.value(v) or 0.0) for v in p_dis]
        power = [p_dis_vals[t] - p_ch_vals[t] for t in range(n)]

        soc_vals: list[float] = []
        temp_vals: list[float] = []
        s_prev = initial_soc
        th_prev = t_amb
        for t in range(n):
            s = s_prev + charge_coef * p_ch_vals[t] - discharge_coef * p_dis_vals[t]
            th = (
                t_amb
                + (th_prev - t_amb) * alpha
                + (p_ch_vals[t] + p_dis_vals[t]) * r_th * (1 - alpha)
            )
            soc_vals.append(s)
            temp_vals.append(th)
            s_prev = s
            th_prev = th

        grid = [load[t] - power[t] for t in range(n)]

        constraint_check = self._verify(
            power=power,
            p_ch=p_ch_vals,
            p_dis=p_dis_vals,
            soc=soc_vals,
            temp=temp_vals,
            grid=grid,
            eff_min_soc=eff_min_soc,
            max_soc=battery.max_soc_ratio,
            terminal_target=terminal_target,
            max_charge=battery.max_charge_power_kw,
            eff_max_discharge=eff_max_discharge,
            max_ramp=battery.max_ramp_kw_per_step,
            max_temp=battery.max_cell_temperature_c,
        )

        baseline_cost = sum(load[t] * price[t] * dt_h for t in range(n))
        optimized_cost = sum(grid[t] * price[t] * dt_h for t in range(n))
        baseline_peak = max(load)
        optimized_peak = max(grid)

        return StorageOptimizationResult(
            plan_id=request.plan_id,
            plan_version=request.plan_version,
            site_id=inputs.site_id,
            target_date=inputs.target_date,
            start_at=inputs.timestamps[0],
            time_step_minutes=inputs.time_step_minutes,
            point_count=n,
            timestamps=list(inputs.timestamps),
            load_forecast_kw=list(load),
            battery_power_kw=power,
            soc_ratio=soc_vals,
            cell_temperature_c=temp_vals,
            baseline_grid_import_power_kw=list(load),
            grid_import_power_kw=grid,
            electricity_price_cny_per_kwh=list(price),
            baseline_energy_cost_cny=baseline_cost,
            optimized_energy_cost_cny=optimized_cost,
            energy_cost_saving_cny=baseline_cost - optimized_cost,
            baseline_peak_demand_kw=baseline_peak,
            optimized_peak_demand_kw=optimized_peak,
            peak_reduction_kw=baseline_peak - optimized_peak,
            terminal_soc_ratio=soc_vals[-1],
            max_cell_temperature_c=max(temp_vals),
            constraint_check=constraint_check,
            solver_status=status_name,
            solve_duration_ms=solve_ms,
            algorithm_version=self.ALGORITHM_VERSION,
            agent_version=self.agent_version,
            data_version=inputs.data_version,
            objective=objective,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_carbon_factors(inputs: DispatchInputBundleV2, n: int) -> list[float]:
        """Per-step carbon emission factor (kgCO2/kWh).

        Uses weighted generation mix when available, otherwise the
        library ``purchase`` factor (default 0.5366).
        """
        lib = inputs.emission_factors.factors_kg_per_kwh
        default_f = lib.get(GenerationSource.PURCHASE.value, _DEFAULT_CARBON_FACTOR)
        if inputs.generation_mix and len(inputs.generation_mix) == n:
            factors: list[float] = []
            for gm in inputs.generation_mix:
                total = gm.total_generation_kw
                if total > 1e-9:
                    weighted = sum(
                        gm.generation_by_source_kw.get(src, 0.0) * lib.get(src, 0.0)
                        for src in gm.generation_by_source_kw
                    )
                    factors.append(weighted / total)
                else:
                    factors.append(default_f)
            return factors
        return [default_f] * n

    @staticmethod
    def _verify(
        *,
        power: list[float],
        p_ch: list[float],
        p_dis: list[float],
        soc: list[float],
        temp: list[float],
        grid: list[float],
        eff_min_soc: float,
        max_soc: float,
        terminal_target: float,
        max_charge: float,
        eff_max_discharge: float,
        max_ramp: float,
        max_temp: float,
    ) -> ConstraintCheckResult:
        """Independent post-solve constraint verification."""
        violations: list[ConstraintViolation] = []
        n = len(power)

        for t in range(n):
            if soc[t] < eff_min_soc - _TOL:
                violations.append(ConstraintViolation(
                    code="SOC_BELOW_MIN",
                    severity="critical",
                    message=(
                        f"SOC ratio {soc[t]:.6f} below effective minimum "
                        f"{eff_min_soc:.6f} at step {t}"
                    ),
                    point_index=t,
                    actual_value=soc[t],
                    limit_value=eff_min_soc,
                    unit="ratio",
                ))
            if soc[t] > max_soc + _TOL:
                violations.append(ConstraintViolation(
                    code="SOC_ABOVE_MAX",
                    severity="critical",
                    message=(
                        f"SOC ratio {soc[t]:.6f} above maximum {max_soc:.6f} "
                        f"at step {t}"
                    ),
                    point_index=t,
                    actual_value=soc[t],
                    limit_value=max_soc,
                    unit="ratio",
                ))
            if p_ch[t] > max_charge + _TOL:
                violations.append(ConstraintViolation(
                    code="CHARGE_POWER_EXCEEDED",
                    severity="critical",
                    message=(
                        f"Charge power {p_ch[t]:.2f} kW exceeds limit "
                        f"{max_charge:.2f} kW at step {t}"
                    ),
                    point_index=t,
                    actual_value=p_ch[t],
                    limit_value=max_charge,
                    unit="kW",
                ))
            if p_dis[t] > eff_max_discharge + _TOL:
                violations.append(ConstraintViolation(
                    code="DISCHARGE_POWER_EXCEEDED",
                    severity="critical",
                    message=(
                        f"Discharge power {p_dis[t]:.2f} kW exceeds limit "
                        f"{eff_max_discharge:.2f} kW at step {t}"
                    ),
                    point_index=t,
                    actual_value=p_dis[t],
                    limit_value=eff_max_discharge,
                    unit="kW",
                ))
            if grid[t] < -_TOL:
                violations.append(ConstraintViolation(
                    code="GRID_EXPORT_VIOLATION",
                    severity="critical",
                    message=(
                        f"Grid import {grid[t]:.2f} kW is negative (export) "
                        f"at step {t}"
                    ),
                    point_index=t,
                    actual_value=grid[t],
                    limit_value=0.0,
                    unit="kW",
                ))
            if temp[t] > max_temp + _TOL:
                violations.append(ConstraintViolation(
                    code="TEMPERATURE_EXCEEDED",
                    severity="critical",
                    message=(
                        f"Cell temperature {temp[t]:.2f} C exceeds limit "
                        f"{max_temp:.2f} C at step {t}"
                    ),
                    point_index=t,
                    actual_value=temp[t],
                    limit_value=max_temp,
                    unit="celsius",
                ))

        for t in range(n):
            prev_p = 0.0 if t == 0 else power[t - 1]
            ramp = abs(power[t] - prev_p)
            if ramp > max_ramp + _TOL:
                violations.append(ConstraintViolation(
                    code="RAMP_RATE_EXCEEDED",
                    severity="critical",
                    message=(
                        f"Ramp {ramp:.2f} kW/step exceeds limit "
                        f"{max_ramp:.2f} kW/step at step {t}"
                    ),
                    point_index=t,
                    actual_value=ramp,
                    limit_value=max_ramp,
                    unit="kW/step",
                ))

        if soc[-1] < terminal_target - _TOL:
            violations.append(ConstraintViolation(
                code="TERMINAL_SOC_BELOW_TARGET",
                severity="critical",
                message=(
                    f"Terminal SOC {soc[-1]:.6f} below target "
                    f"{terminal_target:.6f}"
                ),
                point_index=n - 1,
                actual_value=soc[-1],
                limit_value=terminal_target,
                unit="ratio",
            ))

        return ConstraintCheckResult(
            passed=len(violations) == 0,
            violations=violations,
        )
