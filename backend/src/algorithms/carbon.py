"""电碳计量：直接碳排放因子 C(τ) 与江亿动态碳排放责任因子 Cr(τ)。

复用自 v2 CarbonAccountant，适配 v3 数据结构。

C(τ): 直接碳排放因子，发电结构加权平均强度
Cr(τ): 江亿动态碳排放责任因子，Cr=C·(1+λ(C−C̄)/C̄) 后乘法归一化至守恒
"""
from __future__ import annotations

from core.config import EMISSION_FACTORS

_EPS = 1e-9


def compute_carbon_factors(
    generation_mix: list[dict[str, float]],
    response_lambda: float = 0.5,
    external_cr_factors: list[float] | None = None,
) -> dict:
    """计算 C(τ) 和 Cr(τ) 碳因子序列。

    Args:
        generation_mix: 每个时步的发电结构 [{coal, hydro, wind, solar, total}, ...]
        response_lambda: 责任调节强度

    Returns:
        {c_factors, cr_factors, ratios, c_mean, cr_mean}
    """
    ef = EMISSION_FACTORS
    purchase_f = ef["purchase"]

    c_factors = []
    conservation_weights = []
    known_generation_ratios = []
    for gm in generation_mix:
        coal = max(0.0, gm.get("coal", 0))
        hydro = max(0.0, gm.get("hydro", 0))
        wind = max(0.0, gm.get("wind", 0))
        solar = max(0.0, gm.get("solar", 0))
        known = coal + hydro + wind + solar
        total = max(float(gm.get("total", 0)), known)
        if total <= _EPS:
            c_factors.append(purchase_f)
            conservation_weights.append(1.0)
            known_generation_ratios.append(0.0)
            continue
        residual = max(0.0, float(gm.get("other", 0)), total - known)
        weighted = (
            coal * ef["coal"]
            + hydro * ef["hydro"]
            + wind * ef["wind"]
            + solar * ef["solar"]
            + residual * purchase_f
        )
        c_factors.append(weighted / total)
        conservation_weights.append(total)
        known_generation_ratios.append(min(1.0, known / total))

    # 江亿 Cr(τ)
    weight_sum = sum(conservation_weights)
    c_bar = (
        sum(c * weight for c, weight in zip(c_factors, conservation_weights, strict=True)) / weight_sum
        if c_factors and weight_sum > _EPS
        else 0.0
    )
    if external_cr_factors is not None and len(external_cr_factors) == len(c_factors):
        cr_factors = [float(v) for v in external_cr_factors]
        ratios = [
            cr / c if c > _EPS else 1.0
            for cr, c in zip(cr_factors, c_factors, strict=True)
        ]
    else:
        denom = max(c_bar, _EPS)

        cr_factors = []
        ratios = []
        for c in c_factors:
            adjust = 1.0 + response_lambda * (c - c_bar) / denom
            cr_factors.append(c * adjust)
            ratios.append(adjust)

        # 守恒归一化：mean(Cr) = mean(C) = C̄
        if cr_factors:
            cr_mean = sum(
                cr * weight
                for cr, weight in zip(cr_factors, conservation_weights, strict=True)
            ) / max(weight_sum, _EPS)
            if cr_mean > _EPS:
                scale = c_bar / cr_mean
                cr_factors = [cr * scale for cr in cr_factors]
                ratios = [
                    cr / c if c > _EPS else 1.0
                    for cr, c in zip(cr_factors, c_factors, strict=True)
                ]
    return {
        "c_factors": c_factors,
        "cr_factors": cr_factors,
        "ratios": ratios,
        "c_mean": round(c_bar, 6),
        "cr_mean": round(
            sum(cr * weight for cr, weight in zip(cr_factors, conservation_weights, strict=True))
            / max(weight_sum, _EPS)
            if cr_factors else 0,
            6,
        ),
        "known_generation_ratio_mean": round(
            sum(known_generation_ratios) / len(known_generation_ratios)
            if known_generation_ratios else 0.0,
            4,
        ),
        "residual_factor_assumption": "unclassified generation uses purchase factor",
    }


def account_dispatch_carbon(
    c_factors: list[float],
    cr_factors: list[float],
    baseline_grid_kw: list[float],
    optimized_grid_kw: list[float],
    step_hours: float,
) -> dict:
    """核算储能方案碳排放（直接 + 责任两套）。"""
    n = len(c_factors)
    baseline_direct = sum(baseline_grid_kw[t] * c_factors[t] * step_hours for t in range(n))
    optimized_direct = sum(optimized_grid_kw[t] * c_factors[t] * step_hours for t in range(n))
    baseline_resp = sum(baseline_grid_kw[t] * cr_factors[t] * step_hours for t in range(n))
    optimized_resp = sum(optimized_grid_kw[t] * cr_factors[t] * step_hours for t in range(n))
    return {
        "baseline_direct_kg": round(baseline_direct, 1),
        "optimized_direct_kg": round(optimized_direct, 1),
        "direct_reduction_kg": round(baseline_direct - optimized_direct, 1),
        "baseline_responsibility_kg": round(baseline_resp, 1),
        "optimized_responsibility_kg": round(optimized_resp, 1),
        "responsibility_reduction_kg": round(baseline_resp - optimized_resp, 1),
    }
