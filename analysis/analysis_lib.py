"""Storage + PV dispatch analysis toolkit.

Heavy logic for the dispatch_analysis notebook. Reuses backend/src algorithms
unchanged: optimize_storage_dispatch, calculate_tariff, compute_carbon_factors
and account_dispatch_carbon.

Three accounting tiers per day:
  ref   = load only (no PV, no storage) -> the "without the project" reference
  base  = load - PV (no storage)        -> PV contribution
  opt   = load - PV - storage           -> full project (PV + storage)
"""
from __future__ import annotations

import csv
import hashlib
import os
import pickle
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# --- Make backend/src importable (same convention as backend/tests). ---------
ANALYSIS_DIR = Path(__file__).resolve().parent
BACKEND_SRC = ANALYSIS_DIR.parent / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

try:
    from algorithms.carbon import account_dispatch_carbon, compute_carbon_factors
    _CARBON_BACKEND = True
except Exception:
    # backend/src/algorithms/carbon.py is mid-refactor (external_cr_factors).
    # Vendor the stable pure-math copy so the notebook keeps running; once the
    # refactor lands this branch is unused and the real backend is imported.
    _CARBON_BACKEND = False
    from core.config import EMISSION_FACTORS as _EF
    _EPS = 1e-9

    def compute_carbon_factors(generation_mix, response_lambda=0.5):
        ef = _EF
        purchase_f = ef["purchase"]
        c_factors, weights = [], []
        for gm in generation_mix:
            coal = max(0.0, gm.get("coal", 0))
            hydro = max(0.0, gm.get("hydro", 0))
            wind = max(0.0, gm.get("wind", 0))
            solar = max(0.0, gm.get("solar", 0))
            known = coal + hydro + wind + solar
            total = max(float(gm.get("total", 0)), known)
            if total <= _EPS:
                c_factors.append(purchase_f)
                weights.append(1.0)
                continue
            residual = max(0.0, float(gm.get("other", 0)), total - known)
            weighted = (coal * ef["coal"] + hydro * ef["hydro"] + wind * ef["wind"]
                        + solar * ef["solar"] + residual * purchase_f)
            c_factors.append(weighted / total)
            weights.append(total)
        weight_sum = sum(weights)
        c_bar = (sum(c * w for c, w in zip(c_factors, weights)) / weight_sum
                 if c_factors and weight_sum > _EPS else 0.0)
        denom = max(c_bar, _EPS)
        cr_factors, ratios = [], []
        for c in c_factors:
            adjust = 1.0 + response_lambda * (c - c_bar) / denom
            cr_factors.append(c * adjust)
            ratios.append(adjust)
        if cr_factors:
            cr_mean = sum(cr * w for cr, w in zip(cr_factors, weights)) / max(weight_sum, _EPS)
            if cr_mean > _EPS:
                scale = c_bar / cr_mean
                cr_factors = [cr * scale for cr in cr_factors]
                ratios = [cr / c if c > _EPS else 1.0 for cr, c in zip(cr_factors, c_factors)]
        return {"c_factors": c_factors, "cr_factors": cr_factors, "ratios": ratios,
                "c_mean": round(c_bar, 6)}

    def account_dispatch_carbon(c_factors, cr_factors, baseline_grid_kw, optimized_grid_kw, step_hours):
        n = len(c_factors)
        bd = sum(baseline_grid_kw[t] * c_factors[t] * step_hours for t in range(n))
        od = sum(optimized_grid_kw[t] * c_factors[t] * step_hours for t in range(n))
        br = sum(baseline_grid_kw[t] * cr_factors[t] * step_hours for t in range(n))
        orr = sum(optimized_grid_kw[t] * cr_factors[t] * step_hours for t in range(n))
        return {
            "baseline_direct_kg": round(bd, 1), "optimized_direct_kg": round(od, 1),
            "direct_reduction_kg": round(bd - od, 1),
            "baseline_responsibility_kg": round(br, 1), "optimized_responsibility_kg": round(orr, 1),
            "responsibility_reduction_kg": round(br - orr, 1),
        }
from algorithms.storage import optimize_storage_dispatch
from algorithms.tariff import calculate_tariff
from core.config import (
    DATA_RAW_DIR,
    DEMAND_PRICE_CNY_PER_KW_MONTH,
    POINTS_PER_DAY,
    SIM_STEP_MINUTES,
    SITE_SOLAR_CAPACITY_KW,
    SITE_STORAGE_CAPACITY_KWH,
    SITE_STORAGE_POWER_KW,
    STORAGE_DEFAULTS,
)

DT_H = SIM_STEP_MINUTES / 60.0           # 0.25 h per 15-min step
POINTS = POINTS_PER_DAY                  # 96

# Site-level defaults surfaced to the notebook parameter panel.
DEFAULT_SOLAR_KW = SITE_SOLAR_CAPACITY_KW        # 19100
DEFAULT_STORAGE_KWH = SITE_STORAGE_CAPACITY_KWH  # 30000
DEFAULT_STORAGE_KW = SITE_STORAGE_POWER_KW       # 15000
DEFAULT_CARBON_PRICE = 80.0                       # CNY / ton CO2

# Storage economics (industry-typical defaults, adjustable from notebook).
STORAGE_CAPEX_CNY_PER_KWH = 1500.0   # 1.5 元/Wh 一次性投资
STORAGE_LIFETIME_YEARS = 10
STORAGE_DISCOUNT_RATE = 0.08

PERIOD_ORDER = ["sharp", "peak", "flat", "valley"]
PERIOD_CN = {"sharp": "尖峰", "peak": "峰", "flat": "平", "valley": "谷"}


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------
def date_range(start: date, end: date) -> list[date]:
    """Inclusive list of dates from start to end."""
    days = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def season_of(month: int) -> str:
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    return "winter"


SEASON_CN = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬"}


def pick_representative_days(days: list[date], per_season: int = 4) -> list[date]:
    """Pick per_season evenly-spaced dates within each season's window."""
    buckets: dict[str, list[date]] = {}
    for d in days:
        buckets.setdefault(season_of(d.month), []).append(d)
    picked: list[date] = []
    for season in ("spring", "summer", "autumn", "winter"):
        sdays = sorted(buckets.get(season, []))
        if not sdays:
            continue
        if len(sdays) <= per_season:
            picked.extend(sdays)
            continue
        idx = [int(round(i * (len(sdays) - 1) / (per_season - 1))) for i in range(per_season)]
        picked.extend(sdays[i] for i in idx)
    return sorted(set(picked))


# ---------------------------------------------------------------------------
# Spot price (Hubei real-time clearing)
# ---------------------------------------------------------------------------
def load_spot_prices() -> dict[str, float]:
    """Load Hubei real-time clearing prices into {timestamp_str: cny/kwh}.

    Source CSV stores yuan/MWh; divide by 1000 to get yuan/kWh.
    """
    folder = DATA_RAW_DIR / "hubei_rt_clearing"
    prices: dict[str, float] = {}
    if not folder.exists():
        return prices
    for fn in os.listdir(folder):
        if not fn.endswith(".csv"):
            continue
        path = folder / fn
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    ts = (row.get("time") or "").strip()
                    raw = row.get("实时出清价格")
                    if ts and raw not in (None, ""):
                        prices[ts] = float(raw) / 1000.0
        except Exception:
            continue
    return prices


def match_spot_price(d: date, spot: dict[str, float], tou: list[float]) -> tuple[list[float], bool]:
    """Build the 96-point price vector for date d from spot, TOU fallback."""
    out: list[float] = []
    hit = 0
    for i in range(POINTS):
        hh, mm = divmod(i, 4)
        ts = f"{d.isoformat()} {hh:02d}:{mm * 15:02d}:00"
        if ts in spot:
            out.append(spot[ts])
            hit += 1
        else:
            out.append(float(tou[i]))
    return out, hit == POINTS


# ---------------------------------------------------------------------------
# Battery config scaling (parallel packs) + blended price
# ---------------------------------------------------------------------------
def make_battery_config(storage_kwh: float, storage_kw: float) -> dict:
    """Build a storage config dict scaled to the requested capacity / power."""
    cfg = dict(STORAGE_DEFAULTS)
    cfg["capacity_kwh"] = storage_kwh
    cfg["max_charge_power_kw"] = storage_kw
    cfg["max_discharge_power_kw"] = storage_kw
    cfg["max_ramp_kw_per_step"] = storage_kw
    cfg["thermal_resistance_c_per_kw"] = (8.0 / storage_kw) if storage_kw > 0 else 1.0
    return cfg


def blend_price(price: list[float], c_factors: list[float], carbon_price: float) -> list[float]:
    """Blended price = electricity price + carbon cost of direct emissions.

    c_factors are kgCO2/kWh; carbon_price is CNY/ton CO2, so the per-kWh adder
    is c (kg/kWh) * price (CNY/ton) / 1000 (kg/ton).
    """
    adder = carbon_price / 1000.0
    return [price[t] + c_factors[t] * adder for t in range(len(price))]


# ---------------------------------------------------------------------------
# Day data preparation (run once per date, reused across modes)
# ---------------------------------------------------------------------------
def prepare_day(sim, d: date, spot: dict[str, float]) -> dict:
    """Generate one day and precompute carbon factors + both price vectors."""
    day = sim.generate_day(target_date=d, month=d.month)
    # 碳口径：C 信号已弃用，全程只用 Cr_example 台账责任因子（唯一碳信号）。
    cr = list(day["cr_factors"])
    tou = day["price_cny_per_kwh"]
    spot_price, spot_ok = match_spot_price(d, spot, tou)
    return {
        "date": d,
        "month": d.month,
        "season": day["season"],
        "load": day["load_kw"],
        "solar_base": day["solar_kw"],
        "c_factors": cr,
        "cr_factors": cr,
        "price_tou": tou,
        "price_spot": spot_price,
        "spot_available": spot_ok,
        "periods": day["tariff_periods"],
        "provenance": day["data_provenance"],
    }


def prepare_year(sim, days: list[date], spot: dict[str, float], verbose: bool = True) -> list[dict]:
    """Prepare every day once. Cached to disk so notebook re-runs are instant."""
    out: list[dict] = []
    t0 = time.perf_counter()
    for k, d in enumerate(days):
        out.append(prepare_day(sim, d, spot))
        if verbose and (k + 1) % 60 == 0:
            print(f"  data prep {k + 1}/{len(days)}  ({time.perf_counter() - t0:.0f}s)")
    if verbose:
        print(f"  data prep done in {time.perf_counter() - t0:.1f}s")
    return out


def prepare_year_cached(sim, days: list[date], spot: dict[str, float], verbose: bool = True) -> list[dict]:
    """Disk-cached version of prepare_year keyed on the date window + seed."""
    cache_path = ANALYSIS_DIR / ".cache_year.pkl"
    # v3: 碳口径仅用 Cr_example 台账（C 信号已弃用），旧缓存失效重算。
    sig = f"v3|{days[0]}|{days[-1]}|{len(days)}|{getattr(sim, '_rng', None) and '42' or 'rng'}"
    key = hashlib.md5(sig.encode()).hexdigest()[:10]
    if cache_path.exists():
        try:
            cached = pickle.loads(cache_path.read_bytes())
            if cached.get("key") == key:
                if verbose:
                    print(f"  loaded cached year data ({len(cached['data'])} days)")
                return cached["data"]
        except Exception:
            pass
    data = prepare_year(sim, days, spot, verbose=verbose)
    try:
        cache_path.write_bytes(pickle.dumps({"key": key, "data": data}))
    except Exception:
        pass
    return data


# ---------------------------------------------------------------------------
# Single-day optimisation (pure, no side effects)
# ---------------------------------------------------------------------------
OBJECTIVE_LABELS = {"cost": "电费优化", "synergy": "电碳协同", "carbon": "电碳因子"}
TARIFF_LABELS = {"tou": "分时电价", "spot": "峰谷现货"}


def run_day(
    pdata: dict,
    tariff_mode: str,
    objective: str,
    carbon_price: float,
    solar_kw_cap: float = DEFAULT_SOLAR_KW,
    storage_kwh: float = DEFAULT_STORAGE_KWH,
    storage_kw: float = DEFAULT_STORAGE_KW,
) -> dict:
    """Run reference vs baseline vs optimised for one prepared day."""
    load = pdata["load"]
    solar_scale = solar_kw_cap / DEFAULT_SOLAR_KW
    solar = [s * solar_scale for s in pdata["solar_base"]]

    price = pdata["price_spot"] if tariff_mode == "spot" else pdata["price_tou"]
    c = pdata["c_factors"]
    cr = pdata["cr_factors"]
    periods = pdata["periods"]

    ref_grid = list(load)  # no PV, no storage: pure grid import
    base_grid = [max(0.0, load[t] - solar[t]) for t in range(len(load))]

    if storage_kwh <= 1e-6 or storage_kw <= 1e-6:
        opt_grid = list(base_grid)
        power = [0.0] * len(load)
        soc = [0.5] * len(load)
        status = "no_storage"
    else:
        cfg = make_battery_config(storage_kwh, storage_kw)
        if objective == "carbon":
            obj = "min_carbon"
            opt_price = price
        elif objective == "synergy":
            obj = "min_cost"
            # 电碳协同：将 Cr（台账责任因子）成本注入电价，引导优化在节费同时兼顾减排
            opt_price = blend_price(price, c, carbon_price)
        else:  # cost
            obj = "min_cost"
            opt_price = price
        res = optimize_storage_dispatch(
            load_kw=load,
            price_cny_per_kwh=opt_price,
            carbon_factors=c,
            solar_kw=solar,
            battery_config=cfg,
            objective=obj,
            initial_soc=0.5,
            demand_price_cny_per_kw_month=DEMAND_PRICE_CNY_PER_KW_MONTH,
        )
        opt_grid = [max(0.0, float(g)) for g in res["grid_kw"]]
        power = res["power_kw"]
        soc = res["soc_ratio"]
        status = res["solver_status"]

    ref_tariff = calculate_tariff(ref_grid, price)
    base_tariff = calculate_tariff(base_grid, price)
    opt_tariff = calculate_tariff(opt_grid, price)
    carbon = account_dispatch_carbon(c, cr, base_grid, opt_grid, DT_H)
    ref_direct_kg = sum(ref_grid[t] * c[t] * DT_H for t in range(len(ref_grid)))

    return {
        "date": pdata["date"],
        "month": pdata["month"],
        "season": pdata["season"],
        "spot_available": pdata["spot_available"],
        "load": load,
        "solar": solar,
        "ref_grid": ref_grid,
        "base_grid": base_grid,
        "opt_grid": opt_grid,
        "power_kw": power,
        "soc": soc,
        "price": price,
        "c_factors": c,
        "cr_factors": cr,
        "periods": periods,
        "ref_total": ref_tariff["total_cost_cny"],
        "base_total": base_tariff["total_cost_cny"],
        "opt_total": opt_tariff["total_cost_cny"],
        "ref_energy": ref_tariff["energy_cost_cny"],
        "base_energy": base_tariff["energy_cost_cny"],
        "opt_energy": opt_tariff["energy_cost_cny"],
        "base_demand": base_tariff["demand_cost_cny"],
        "opt_demand": opt_tariff["demand_cost_cny"],
        "base_peak": base_tariff["peak_demand_kw"],
        "opt_peak": opt_tariff["peak_demand_kw"],
        "saving_cny": round(base_tariff["total_cost_cny"] - opt_tariff["total_cost_cny"], 2),
        "carbon_direct_red_kg": round(carbon["direct_reduction_kg"], 1),
        "carbon_resp_red_kg": round(carbon["responsibility_reduction_kg"], 1),
        "ref_direct_kg": round(ref_direct_kg, 1),
        "base_direct_kg": round(carbon["baseline_direct_kg"], 1),
        "opt_direct_kg": round(carbon["optimized_direct_kg"], 1),
        "status": status,
    }


# ---------------------------------------------------------------------------
# Scenario running over many days
# ---------------------------------------------------------------------------
def run_scenario(
    year_data: list[dict],
    tariff_mode: str,
    objective: str,
    carbon_price: float = DEFAULT_CARBON_PRICE,
    solar_kw_cap: float = DEFAULT_SOLAR_KW,
    storage_kwh: float = DEFAULT_STORAGE_KWH,
    storage_kw: float = DEFAULT_STORAGE_KW,
    verbose: bool = True,
) -> list[dict]:
    """Run one (tariff, objective) scenario across all prepared days."""
    results: list[dict] = []
    t0 = time.perf_counter()
    n = len(year_data)
    for k, pdata in enumerate(year_data):
        results.append(run_day(
            pdata, tariff_mode, objective, carbon_price,
            solar_kw_cap, storage_kwh, storage_kw,
        ))
        if verbose and (k + 1) % 50 == 0:
            elapsed = time.perf_counter() - t0
            eta = elapsed / (k + 1) * (n - k - 1)
            print(f"  {TARIFF_LABELS[tariff_mode]}/{OBJECTIVE_LABELS[objective]} "
                  f"{k + 1}/{n}  elapsed {elapsed:.0f}s  eta {eta:.0f}s")
    if verbose:
        print(f"  {TARIFF_LABELS[tariff_mode]}/{OBJECTIVE_LABELS[objective]} "
              f"done in {time.perf_counter() - t0:.1f}s")
    return results


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------
def totals(results: list[dict]) -> dict:
    """Sum cost / carbon across all days of a scenario."""
    keys = [
        "ref_total", "base_total", "opt_total", "saving_cny",
        "ref_energy", "base_energy", "opt_energy",
        "base_demand", "opt_demand",
        "carbon_direct_red_kg", "carbon_resp_red_kg",
        "ref_direct_kg", "base_direct_kg", "opt_direct_kg",
    ]
    out = {k: sum(r[k] for r in results) for k in keys}
    out["days"] = len(results)
    return out


def period_breakdown(grid: list[float], price: list[float], periods: list[str]) -> dict[str, float]:
    """Energy cost (yuan) grouped by tariff period."""
    costs = {p: 0.0 for p in PERIOD_ORDER}
    for t in range(len(grid)):
        p = periods[t]
        if p in costs:
            costs[p] += grid[t] * price[t] * DT_H
    return costs


def provenance_stats(year_data: list[dict]) -> dict[str, dict[str, int]]:
    """Count real vs fallback days per data channel."""
    channels = ["load", "generation_mix", "tariff"]
    stats: dict[str, dict[str, int]] = {ch: {"real": 0, "fallback": 0} for ch in channels}
    for pdata in year_data:
        prov = pdata["provenance"]
        for ch in channels:
            info = prov.get(ch, {})
            quality = str(info.get("quality", "")).lower()
            is_real = info.get("loaded") and "simulated" not in quality and "proxy" not in quality
            if is_real:
                stats[ch]["real"] += 1
            else:
                stats[ch]["fallback"] += 1
    return stats


# ---------------------------------------------------------------------------
# Representative-day annualisation + capacity sensitivity
# ---------------------------------------------------------------------------
_ANNUAL_METRICS = [
    "saving_cny", "carbon_direct_red_kg", "base_total", "opt_total",
    "ref_total", "base_direct_kg", "opt_direct_kg", "ref_direct_kg",
    "base_energy", "opt_energy", "base_demand", "opt_demand",
]


def annualize_rep(results: list[dict], year_days: list[date]) -> dict:
    """Scale representative-day results to a full year via season weighting.

    Each metric is averaged within a season over the representative days that
    fall in it, then multiplied by that season's day count in the full year.
    """
    season_counts: dict[str, int] = {}
    for d in year_days:
        s = season_of(d.month)
        season_counts[s] = season_counts.get(s, 0) + 1
    by_season: dict[str, list[dict]] = {}
    for r in results:
        by_season.setdefault(r["season"], []).append(r)
    out = {m: 0.0 for m in _ANNUAL_METRICS}
    for season, rs in by_season.items():
        ndays = season_counts.get(season, 0)
        if not rs:
            continue
        for m in _ANNUAL_METRICS:
            out[m] += (sum(r[m] for r in rs) / len(rs)) * ndays
    out["days"] = sum(season_counts.values())
    return out


def sensitivity_scan(
    rep_data: list[dict],
    year_days: list[date],
    tariff_mode: str = "tou",
    objective: str = "cost",
    carbon_price: float = DEFAULT_CARBON_PRICE,
    solar_scales: list[float] | None = None,
    storage_scales: list[float] | None = None,
) -> tuple[dict, list[float], list[float]]:
    """Grid scan over PV x storage multipliers on representative days.

    Returns ({(solar_scale, storage_scale): annualised_totals}, solar_scales,
    storage_scales).
    """
    if solar_scales is None:
        solar_scales = [0.0, 0.5, 1.0, 1.5, 2.0]
    if storage_scales is None:
        storage_scales = [0.0, 0.5, 1.0, 1.5, 2.0]
    grid: dict[tuple[float, float], dict] = {}
    for ss in solar_scales:
        for st in storage_scales:
            res = [
                run_day(
                    pd, tariff_mode, objective, carbon_price,
                    solar_kw_cap=DEFAULT_SOLAR_KW * ss,
                    storage_kwh=DEFAULT_STORAGE_KWH * st,
                    storage_kw=DEFAULT_STORAGE_KW * st,
                )
                for pd in rep_data
            ]
            grid[(ss, st)] = annualize_rep(res, year_days)
    return grid, solar_scales, storage_scales


def annual_storage_cost(storage_kwh: float,
                        capex: float = STORAGE_CAPEX_CNY_PER_KWH,
                        years: int = STORAGE_LIFETIME_YEARS,
                        rate: float = STORAGE_DISCOUNT_RATE) -> float:
    """Equal annual cost of storage investment (capital recovery factor).

    capex is yuan/kWh one-time; returns yuan/year.
    """
    if storage_kwh <= 0 or years <= 0:
        return 0.0
    investment = storage_kwh * capex
    if rate <= 0:
        return investment / years
    crf = rate * (1 + rate) ** years / ((1 + rate) ** years - 1)
    return investment * crf


def storage_capacity_scan(
    rep_data: list[dict],
    year_days: list[date],
    tariff_mode: str = "tou",
    objective: str = "cost",
    carbon_price: float = DEFAULT_CARBON_PRICE,
    solar_kw_cap: float = DEFAULT_SOLAR_KW,
    storage_kwh_list: list[float] | None = None,
    capex: float = STORAGE_CAPEX_CNY_PER_KWH,
    years: int = STORAGE_LIFETIME_YEARS,
    rate: float = STORAGE_DISCOUNT_RATE,
) -> list[dict]:
    """Scan storage capacity on representative days; return annualised net-benefit.

    Each result dict: {storage_kwh, saving_cny, carbon_red_kg, capex_cny,
    annual_cost_cny, net_benefit_cny}.
    """
    if storage_kwh_list is None:
        # 0 ~ 60 MWh in 5 MWh steps
        storage_kwh_list = [i * 5000 for i in range(13)]
    results: list[dict] = []
    for kwh in storage_kwh_list:
        kw = kwh / 2.0  # 2h duration: power = capacity / 2
        res = [
            run_day(pd, tariff_mode, objective, carbon_price,
                    solar_kw_cap=solar_kw_cap, storage_kwh=kwh, storage_kw=kw)
            for pd in rep_data
        ]
        ann = annualize_rep(res, year_days)
        ac = annual_storage_cost(kwh, capex, years, rate)
        results.append({
            "storage_kwh": kwh,
            "storage_mwh": kwh / 1e3,
            "saving_cny": ann["saving_cny"],
            "carbon_red_kg": ann["carbon_direct_red_kg"],
            "investment_cny": kwh * capex,
            "annual_cost_cny": ac,
            "net_benefit_cny": ann["saving_cny"] - ac,
        })
    return results
