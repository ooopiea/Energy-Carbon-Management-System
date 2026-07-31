"""电碳计量模块：直接碳排放因子 C(τ) 与江亿动态碳排放责任因子 Cr(τ)。

C(τ) 是碳流的物理强度，按发电结构加权平均；Cr(τ) 在 C(τ) 基础上叠加江亿院士
提出的动态责任调节，激励负荷与低碳发电时段相匹配。
"""

from __future__ import annotations

from collections.abc import Sequence

from energy_agent_v2.contracts import (
    CarbonAccountingResult,
    CarbonDispatchResult,
    CarbonMethod,
    EmissionFactorLibrary,
    GenerationMixPoint,
    GenerationSource,
)

# 湖南电网 2022 官方校准的因子库版本标识
_EF_LIBRARY_VERSION = "ef-national-grid-avg-2022-v1"
# 防止除零的极小正数
_EPS = 1e-9


class CarbonAccountant:
    """电碳计量师：基于发电结构计算直接因子 C(τ) 与江亿动态责任因子 Cr(τ)。

    江亿动态碳排放责任方法核心思想：碳排放责任不应简单按"用电量 × 平均因子"，
    而应按用电行为对系统碳排放的责任贡献动态分配——低碳发电富余时段(风光大发)
    用电责任降低，高碳时段(火电高峰)用电责任加重，从而激励负荷与低碳发电时段匹配。
    """

    def __init__(self, response_lambda: float = 0.5):
        # 责任调节强度 λ：越大则低碳/高碳时段的责任减免与加重越显著
        self.response_lambda = response_lambda

    # ------------------------------------------------------------------
    # C(τ) 直接因子 与 Cr(τ) 动态责任因子
    # ------------------------------------------------------------------
    def compute_factors(
        self,
        generation_mix: Sequence[GenerationMixPoint],
        ef_lib: EmissionFactorLibrary,
        region: str,
    ) -> CarbonAccountingResult:
        factors = ef_lib.factors_kg_per_kwh
        purchase_factor = factors.get(GenerationSource.PURCHASE.value, 0.5366)

        timestamps = [p.timestamp for p in generation_mix]

        # 1) 直接碳排放因子 C(τ)：发电结构加权平均排放强度
        c_factors: list[float] = []
        for point in generation_mix:
            total = point.total_generation_kw
            if total <= _EPS:
                # 总发电为 0 的时步以 purchase 因子兜底（视为外购电）
                c_factors.append(purchase_factor)
                continue
            weighted = sum(
                power * factors.get(src, 0.0)
                for src, power in point.generation_by_source_kw.items()
            )
            c_factors.append(weighted / total)

        # 2) 江亿动态碳排放责任因子 Cr(τ)
        c_bar = sum(c_factors) / len(c_factors) if c_factors else 0.0
        denom = max(c_bar, _EPS)

        cr_factors: list[float] = []
        ratios: list[float] = []
        for c in c_factors:
            # adjust(t) = 1 + λ·(C(τ)−C̄)/C̄
            adjust = 1.0 + self.response_lambda * (c - c_bar) / denom
            cr_factors.append(c * adjust)
            ratios.append(adjust)

        # V2.1 修复 S4：守恒归一化 —— 乘法缩放使 mean(Cr) = mean(C) = C̄
        # 原 Cr 乘法公式导致 mean(Cr) = C̄ + λ·Var(C)/C̄ > C̄（非守恒）。
        # 乘法归一化 scale=C̄/mean(Cr_raw) 保持 Cr>0 和方向性，同时消除总量偏差。
        if cr_factors:
            cr_mean = sum(cr_factors) / len(cr_factors)
            if cr_mean > _EPS:
                scale = c_bar / cr_mean
                cr_factors = [cr * scale for cr in cr_factors]
                ratios = [cr / c if c > _EPS else 1.0 for cr, c in zip(cr_factors, c_factors)]

        notes = [
            "C(τ): 直接碳排放因子，发电结构加权平均强度 Σ(gen·ef)/Σgen",
            "Cr(τ): 江亿动态碳排放责任因子，Cr=C·(1+λ(C−C̄)/C̄) 后乘法归一化至守恒",
            f"response_lambda={self.response_lambda}：低碳发电富余时段责任减免，高碳时段加重",
            "V2.1 守恒修复：mean(Cr)=mean(C)=C̄，总量与直接碳一致（乘法归一化）",
            "注意：Cr(τ) 是激励信号，不是物理减排计量；用于优化方向引导而非碳交易/碳报告",
            "注意：min_carbon 目标当前用平均因子 C(τ)，边际排放因子 MEF 更精确但需真实数据",
            "总发电为 0 的时步以 purchase 因子兜底（外购电）",
        ]

        return CarbonAccountingResult(
            method=CarbonMethod.RESPONSIBILITY,
            timestamps=timestamps,
            direct_factor_c_kg_per_kwh=c_factors,
            responsibility_factor_cr_kg_per_kwh=cr_factors,
            responsibility_adjustment_ratio=ratios,
            emission_factor_library_version=ef_lib.source_label or _EF_LIBRARY_VERSION,
            region=region,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # 储能方案碳排放核算（直接 + 责任两套）
    # ------------------------------------------------------------------
    def account_dispatch(
        self,
        carbon_result: CarbonAccountingResult,
        baseline_grid_kw: Sequence[float],
        optimized_grid_kw: Sequence[float],
        step_hours: float,
    ) -> CarbonDispatchResult:
        n = len(carbon_result.direct_factor_c_kg_per_kwh)
        if len(baseline_grid_kw) != n or len(optimized_grid_kw) != n:
            raise ValueError(
                f"grid 序列长度({len(baseline_grid_kw)}, {len(optimized_grid_kw)})"
                f"与碳因子序列长度({n})不一致"
            )

        c = carbon_result.direct_factor_c_kg_per_kwh
        cr = carbon_result.responsibility_factor_cr_kg_per_kwh

        baseline_direct = sum(baseline_grid_kw[t] * c[t] * step_hours for t in range(n))
        optimized_direct = sum(optimized_grid_kw[t] * c[t] * step_hours for t in range(n))
        baseline_resp = sum(baseline_grid_kw[t] * cr[t] * step_hours for t in range(n))
        optimized_resp = sum(optimized_grid_kw[t] * cr[t] * step_hours for t in range(n))

        return CarbonDispatchResult(
            carbon_accounting=carbon_result,
            baseline_direct_carbon_kg=baseline_direct,
            optimized_direct_carbon_kg=optimized_direct,
            direct_carbon_reduction_kg=baseline_direct - optimized_direct,
            baseline_responsibility_carbon_kg=baseline_resp,
            optimized_responsibility_carbon_kg=optimized_resp,
            responsibility_carbon_reduction_kg=baseline_resp - optimized_resp,
        )
