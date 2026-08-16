"""Generate dispatch_analysis.ipynb via nbformat.

Run:  python analysis/_build_notebook.py
Heavy logic lives in analysis_lib.py; this only assembles notebook cells.
"""
import nbformat as nbf
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dispatch_analysis.ipynb"

CELLS = [

# ----------------------------------------------------------------- 0 title
("m", r"""# 储能 + 光伏 调度效果分析

黄花工业园区 · 全年（2025-06-15 ~ 2026-06-14）调度节费与节碳效果评估。

本 notebook 复用 `backend/src` 的储能优化、电费核算与碳排算法，**不修改任何源码**；重逻辑在 `analysis/analysis_lib.py`，notebook 只负责参数与可视化。

- 参考（ref）= 无光伏无储能
- 仅光伏（base）= 负荷 − 光伏
- 光伏+储能（opt）= 负荷 − 光伏 − 储能出力

三档同口径计费对比，公平体现光伏与储能各自的贡献。"""),

# ----------------------------------------------------------------- 1 setup
("c", r"""import sys, os
from pathlib import Path
from datetime import date

# 定位项目根目录（notebook 可从根目录或 analysis/ 启动）
_here = Path(os.getcwd()).resolve()
_root = _here
for _p in [_here, *_here.parents]:
    if (_p / "backend" / "src" / "core" / "config.py").exists():
        _root = _p
        break
for _sub in ("backend/src", "analysis"):
    _d = str(_root / _sub)
    if _d not in sys.path:
        sys.path.insert(0, _d)

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from IPython.display import display

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 110

# 紫色系全局主题
plt.rcParams["axes.prop_cycle"] = plt.cycler(color=[
    "#7b1fa2", "#9c27b0", "#ba68c8", "#e91e63", "#ff8f00",
    "#26c6da", "#4a148c", "#ce93d8", "#ad1457", "#880e4f",
])
plt.rcParams["axes.edgecolor"] = "#4a148c"
plt.rcParams["axes.labelcolor"] = "#4a148c"
plt.rcParams["xtick.color"] = "#6a1b9a"
plt.rcParams["ytick.color"] = "#6a1b9a"
plt.rcParams["axes.titlecolor"] = "#4a148c"
plt.rcParams["grid.color"] = "#e1bee7"
plt.rcParams["figure.facecolor"] = "#faf5fc"
plt.rcParams["axes.facecolor"] = "#faf5fc"

import analysis_lib as L
from data.simulator import DataSimulator

print("项目根目录:", _root)
print("就绪。")"""),

# ----------------------------------------------------------------- 2 param md
("m", r"""## 1 · 参数面板

顶部参数可直接修改：光伏 / 储能容量、碳价、分析周期。默认取站级配置（光伏 19.1 MW、储能 15 MW / 30 MWh），碳价 80 元/吨CO2。"""),

# ----------------------------------------------------------------- 3 params
("c", r"""# ====== 可调参数 ======
SOLAR_KW     = L.DEFAULT_SOLAR_KW      # 光伏装机 (kW)
STORAGE_KWH  = L.DEFAULT_STORAGE_KWH   # 储能容量 (kWh)
STORAGE_KW   = L.DEFAULT_STORAGE_KW    # 储能功率 (kW)
CARBON_PRICE = L.DEFAULT_CARBON_PRICE  # 碳价 (元/吨CO2)

START_DATE = date(2025, 6, 15)
END_DATE   = date(2026, 6, 14)

print(f"光伏 {SOLAR_KW/1e3:.1f} MW | 储能 {STORAGE_KWH/1e3:.0f} MWh / {STORAGE_KW/1e3:.0f} MW | 碳价 {CARBON_PRICE:.0f} 元/吨")
print(f"周期 {START_DATE} ~ {END_DATE}")"""),

# ----------------------------------------------------------------- 4 data md
("m", r"""## 2 · 数据装载

逐日生成全年数据（真实优先、缺失月份工程回退），结果磁盘缓存，二次运行秒级载入。电价备两套：分时电价（尖/峰/平/谷）与湖北实时出清价（15min，元/MWh → 元/kWh）。碳因子采用 Cr_example 台账责任因子（系统唯一碳信号，C 已弃用）。"""),

# ----------------------------------------------------------------- 5 data
("c", r"""sim = DataSimulator(seed=42)
SPOT = L.load_spot_prices()
print(f"湖北现货价格条目: {len(SPOT):,}")

DAYS = L.date_range(START_DATE, END_DATE)
print(f"分析天数: {len(DAYS)} 天")
YEAR_DATA = L.prepare_year_cached(sim, DAYS, SPOT)

# 代表性日：每季 4 天，共约 16 天，用于快速多模式 / 敏感性扫描
REP_DATES = set(L.pick_representative_days(DAYS, per_season=4))
REP_DATA = [d for d in YEAR_DATA if d["date"] in REP_DATES]
print(f"代表性日 {len(REP_DATA)} 天:", [d["date"].isoformat() for d in REP_DATA])"""),

# ----------------------------------------------------------------- 6 dispatch md
("m", r"""## 3 · 全年调度（分时电价 · 电费优化）

对全年 365 天逐日求解 96 点 MILP 储能调度，三档（参考 / 仅光伏 / 光伏+储能）同口径计费。"""),

# ----------------------------------------------------------------- 7 dispatch
("c", r"""print("全年调度中（分时电价 / 电费优化）…")
RES = L.run_scenario(YEAR_DATA, "tou", "cost", CARBON_PRICE,
                     SOLAR_KW, STORAGE_KWH, STORAGE_KW)
TOT = L.totals(RES)
print("完成。")"""),

# ----------------------------------------------------------------- 8 summary
("c", r"""t = TOT
ref, base, opt = t["ref_total"], t["base_total"], t["opt_total"]
print("=" * 54)
print("  全年调度效果（分时电价 · 电费优化）")
print("=" * 54)
print(f"  参考（无项目）  电费  {ref/1e4:>10.1f} 万元")
print(f"  仅光伏          电费  {base/1e4:>10.1f} 万元")
print(f"  光伏+储能       电费  {opt/1e4:>10.1f} 万元")
print("-" * 54)
print(f"  系统节费（对参考）   {(ref-opt)/1e4:>9.1f} 万元  ({(ref-opt)/ref*100:.2f} %)")
print(f"  储能节费（对仅光伏） {t['saving_cny']/1e4:>9.1f} 万元  ({t['saving_cny']/base*100:.2f} %)")
print("-" * 54)
print(f"  碳减排（对仅光伏） {t['carbon_direct_red_kg']/1e3:>7.1f} 吨CO2")
print(f"  仅光伏碳排 {t['base_direct_kg']/1e3:.0f} 吨 -> 优化后 {t['opt_direct_kg']/1e3:.0f} 吨")"""),

# ----------------------------------------------------------------- 9 viz md
("m", r"""## 4 · 可视化

### 4.1 三档电费与碳排对比
灰色 = 参考、橙色 = 仅光伏、绿色 = 光伏+储能。"""),

# -----------------------------------------------------------------10 3-tier
("c", r"""labels = ["参考\n(无项目)", "仅光伏", "光伏+储能"]
cols = ["#b39ddb", "#d1c4e9", "#7b1fa2"]
fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.3))
for ax, vals, title, unit, scale in [
    (axes[0], [ref, base, opt], "全年电费", "万元", 1e4),
    (axes[1], [t["ref_direct_kg"], t["base_direct_kg"], t["opt_direct_kg"]], "全年直接碳排", "吨CO2", 1e3),
]:
    bars = ax.bar(labels, [v / scale for v in vals], color=cols)
    ax.set_title(title)
    ax.set_ylabel(unit)
    for r, v in zip(bars, vals):
        ax.text(r.get_x() + r.get_width() / 2, r.get_height(), f"{v/scale:.0f}",
                ha="center", va="bottom", fontsize=9)
plt.tight_layout()
plt.show()"""),

# -----------------------------------------------------------------11 pb
("m", r"""### 4.2 各时段电度成本（峰平谷占比）
储能把峰段用电搬到谷段，直接体现在峰段成本下降、谷段成本上升。"""),

("c", r"""base_pb = {p: 0.0 for p in L.PERIOD_ORDER}
opt_pb = {p: 0.0 for p in L.PERIOD_ORDER}
for r in RES:
    for p, v in L.period_breakdown(r["base_grid"], r["price"], r["periods"]).items():
        base_pb[p] += v
    for p, v in L.period_breakdown(r["opt_grid"], r["price"], r["periods"]).items():
        opt_pb[p] += v

x = np.arange(len(L.PERIOD_ORDER))
w = 0.38
fig, ax = plt.subplots(figsize=(8.6, 4.3))
ax.bar(x - w/2, [base_pb[p]/1e4 for p in L.PERIOD_ORDER], w, label="仅光伏", color="#d1c4e9")
ax.bar(x + w/2, [opt_pb[p]/1e4 for p in L.PERIOD_ORDER], w, label="光伏+储能", color="#7b1fa2")
ax.set_xticks(x)
ax.set_xticklabels([L.PERIOD_CN[p] for p in L.PERIOD_ORDER])
ax.set_ylabel("万元")
ax.set_title("各时段电度成本（全年合计）")
ax.legend()
plt.tight_layout()
plt.show()"""),

# -----------------------------------------------------------------12 typical
("m", r"""### 4.3 典型日调度曲线
负荷 / 光伏 / 电网购电 / 储能充放电 / SOC 曲线，直观展示能量搬运过程。"""),

("c", r"""# 选储能动作最显著的夏季代表性日
_summer = [L.run_day(d, "tou", "cost", CARBON_PRICE, SOLAR_KW, STORAGE_KWH, STORAGE_KW)
           for d in REP_DATA if d["season"] == "summer"]
td = max(_summer, key=lambda r: max(abs(p) for p in r["power_kw"]))
h = np.arange(96) * 0.25
pw = np.array(td["power_kw"])

fig, ax = plt.subplots(figsize=(10.5, 4.4))
ax.plot(h, np.array(td["load"]) / 1e3, color="#4527a0", lw=1.5, label="负荷")
ax.plot(h, np.array(td["solar"]) / 1e3, color="#ce93d8", lw=1.5, label="光伏")
ax.plot(h, np.array(td["opt_grid"]) / 1e3, color="#7b1fa2", lw=1.5, label="电网购电")
ax.fill_between(h, pw / 1e3, 0, where=(pw >= 0), color="#e91e63", alpha=0.35, label="放电")
ax.fill_between(h, pw / 1e3, 0, where=(pw < 0), color="#26c6da", alpha=0.35, label="充电")
ax.set_xlim(0, 24)
ax.set_xlabel("小时")
ax.set_ylabel("MW")
ax.set_title(f"典型日调度曲线（{td['date']}）")
ax.legend(loc="upper left", ncol=5, fontsize=8)
ax2 = ax.twinx()
ax2.plot(h, td["soc"], color="#4a148c", lw=1.2, ls="--", label="SOC")
ax2.set_ylabel("SOC", color="#4a148c")
ax2.set_ylim(0, 1)
plt.tight_layout()
plt.show()
print(f"当日节费 {td['saving_cny']:.0f} 元；直排碳变化 {td['carbon_direct_red_kg']/1e3:+.2f} 吨")"""),

# -----------------------------------------------------------------13 monthly
("m", r"""### 4.4 月度趋势
全年节费与碳减排按月分布，体现季节性（光伏夏强冬弱）。"""),

("c", r"""mdf = pd.DataFrame([{"年月": r["date"].strftime("%Y-%m"), "节费": r["saving_cny"],
                        "碳减排_kg": r["carbon_direct_red_kg"]} for r in RES])
mg = mdf.groupby("年月").sum().sort_index()
xlabs = list(mg.index)
x = np.arange(len(xlabs))
fig, ax = plt.subplots(figsize=(11, 4.2))
ax.bar(x, mg["节费"] / 1e4, color="#7b1fa2", alpha=0.85, label="储能节费")
ax.set_xticks(x)
ax.set_xticklabels(xlabs, rotation=45, ha="right")
ax.set_xlabel("年月")
ax.set_ylabel("节费 (万元)", color="#7b1fa2")
ax2 = ax.twinx()
ax2.plot(x, mg["碳减排_kg"] / 1e3, color="#e91e63", marker="o", lw=1.5, label="碳减排")
ax2.axhline(0, color="#e0e0e0", lw=0.7)
ax2.set_ylabel("碳减排 (吨CO2)", color="#ad1457")
ax.set_title("月度节费与碳减排趋势")
fig.legend(loc="upper right", bbox_to_anchor=(0.9, 0.95))
plt.tight_layout()
plt.show()"""),

# -----------------------------------------------------------------14 scatter
("m", r"""### 4.5 电价 vs 碳因子
揭示「低价时段往往高碳」的结构性矛盾 —— 这正是电碳协同优化的价值所在。"""),

("c", r"""pc = {"sharp": "#880e4f", "peak": "#ff8f00", "flat": "#b39ddb", "valley": "#7b1fa2"}
xs, ys, cs = [], [], []
for d in REP_DATA:
    for i in range(96):
        xs.append(d["price_tou"][i])
        ys.append(d["c_factors"][i])
        cs.append(pc.get(d["periods"][i], "#b39ddb"))
fig, ax = plt.subplots(figsize=(8.6, 4.6))
ax.scatter(xs, ys, c=cs, s=10, alpha=0.55)
ax.set_xlabel("分时电价 (元/kWh)")
ax.set_ylabel("碳因子 Cr (kgCO2/kWh)")
ax.set_title("电价 vs 碳因子 Cr（按时段着色）")
_handles = [Line2D([0], [0], marker="o", color="w", markerfacecolor=pc[p], markersize=8)
            for p in L.PERIOD_ORDER]
ax.legend(handles=_handles, labels=[L.PERIOD_CN[p] for p in L.PERIOD_ORDER],
          fontsize=8, title="时段")
plt.tight_layout()
plt.show()"""),

# -----------------------------------------------------------------15 modes md
("m", r"""## 5 · 三种优化模式对比

电费优化（`min_cost`）· 电碳协同（混合价格法 `price + 碳价/1000 × Cr(t)`）· 电碳因子（`min_carbon`）。
用代表性日年化快速对比。**核心叙事**：纯电费优化可能增碳，协同模式兼顾节费与减排。"""),

("c", r"""MODES = [("cost", "电费优化"), ("synergy", "电碳协同"), ("carbon", "电碳因子")]
mode_tot = {}
for key, _name in MODES:
    rr = [L.run_day(d, "tou", key, CARBON_PRICE, SOLAR_KW, STORAGE_KWH, STORAGE_KW)
          for d in REP_DATA]
    mode_tot[key] = L.annualize_rep(rr, DAYS)

x = np.arange(len(MODES))
w = 0.4
fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.3))
axes[0].bar(x, [mode_tot[k]["saving_cny"] / 1e4 for k, _ in MODES], w, color="#7b1fa2")
axes[0].set_xticks(x)
axes[0].set_xticklabels([n for _, n in MODES])
axes[0].set_ylabel("年化节费 (万元)")
axes[0].set_title("年化节费")
axes[1].bar(x, [mode_tot[k]["carbon_direct_red_kg"] / 1e3 for k, _ in MODES], w, color="#7b1fa2")
axes[1].axhline(0, color="#bdbdbd", lw=0.8)
axes[1].set_xticks(x)
axes[1].set_xticklabels([n for _, n in MODES])
axes[1].set_ylabel("年化直排减排 (吨CO2)")
axes[1].set_title("年化碳减排")
plt.tight_layout()
plt.show()
for k, n in MODES:
    print(f"{n}: 节费 {mode_tot[k]['saving_cny']/1e4:7.1f} 万元, "
          f"直排减排 {mode_tot[k]['carbon_direct_red_kg']/1e3:8.1f} 吨")"""),

# -----------------------------------------------------------------16 tariff md
("m", r"""## 6 · 电价模式对比
分时电价 vs 湖北峰谷现货（代表性日年化），看不同电价信号下的调度节费空间。"""),

("c", r"""TARIFFS = [("tou", "分时电价"), ("spot", "峰谷现货")]
tar_tot = {}
for key, _name in TARIFFS:
    rr = [L.run_day(d, key, "cost", CARBON_PRICE, SOLAR_KW, STORAGE_KWH, STORAGE_KW)
          for d in REP_DATA]
    tar_tot[key] = L.annualize_rep(rr, DAYS)

x = np.arange(len(TARIFFS))
fig, ax = plt.subplots(figsize=(6.8, 4.3))
vals = [tar_tot[k]["saving_cny"] / 1e4 for k, _ in TARIFFS]
bars = ax.bar(x, vals, 0.45, color=["#26c6da", "#d1c4e9"])
ax.set_xticks(x)
ax.set_xticklabels([n for _, n in TARIFFS])
ax.set_ylabel("年化储能节费 (万元)")
ax.set_title("电价模式对调度节费的影响")
for r, v in zip(bars, vals):
    ax.text(r.get_x() + r.get_width() / 2, r.get_height(), f"{v:.0f}",
            ha="center", va="bottom")
plt.tight_layout()
plt.show()"""),

# -----------------------------------------------------------------17 sens md
("m", r"""## 7 · 储能容量优化
光伏固定 19.1 MW，扫描储能容量 0 ~ 60 MWh（2h 时长），扣储能投资成本算年化净收益，找最优配置。"""),

("c", r"""print("储能容量扫描中（代表性日年化）…")
SC = L.storage_capacity_scan(REP_DATA, DAYS, "tou", "cost", CARBON_PRICE)
xs = [r["storage_mwh"] for r in SC]
saving = [r["saving_cny"] / 1e4 for r in SC]
cost = [r["annual_cost_cny"] / 1e4 for r in SC]
net = [r["net_benefit_cny"] / 1e4 for r in SC]
best = max(SC, key=lambda r: r["net_benefit_cny"])

fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.5))
ax = axes[0]
ax.plot(xs, saving, "o-", color="#7b1fa2", lw=2, label="年化节费", markersize=4)
ax.plot(xs, cost, "s-", color="#e91e63", lw=2, label="年化投资成本", markersize=4)
ax.plot(xs, net, "^-", color="#7b1fa2", lw=2.5, label="年化净收益", markersize=5)
ax.axhline(0, color="#e0e0e0", lw=0.7)
ax.axvline(best["storage_mwh"], color="#ff8f00", ls="--", lw=1.2,
           label=f"最优 {best['storage_mwh']:.0f} MWh")
ax.set_xlabel("储能容量 (MWh, 2h)")
ax.set_ylabel("万元 / 年")
ax.set_title("储能容量 vs 年化节费/成本/净收益")
ax.legend(fontsize=8)

ax2 = axes[1]
bars = ax2.bar(xs, net, width=3.5, color=["#7b1fa2" if n > 0 else "#e91e63" for n in net])
ax2.axhline(0, color="#e0e0e0", lw=0.7)
ax2.set_xlabel("储能容量 (MWh, 2h)")
ax2.set_ylabel("年化净收益 (万元)", color="#7b1fa2")
ax2.set_title(f"年化净收益 (最优 {best['storage_mwh']:.0f} MWh = {best['net_benefit_cny']/1e4:.0f} 万元/年)")
ax3 = ax2.twinx()
ax3.plot(xs, [r["carbon_red_kg"] / 1e3 for r in SC], "D-", color="#ff8f00", lw=1.5, markersize=4)
ax3.set_ylabel("年化碳减排 (吨CO2)", color="#ff8f00")
plt.tight_layout()
plt.show()

print(f"最优储能配置: {best['storage_mwh']:.0f} MWh ({best['storage_mwh']/2:.0f} MW)")
print(f"  年化节费 {best['saving_cny']/1e4:.0f} 万元 | 年化成本 {best['annual_cost_cny']/1e4:.0f} 万元")
print(f"  年化净收益 {best['net_benefit_cny']/1e4:.0f} 万元")
cur = [r for r in SC if r["storage_mwh"] == 30]
if cur:
    print(f"  当前站级 30 MWh: 净收益 {cur[0]['net_benefit_cny']/1e4:.0f} 万元/年")
print(f"  投资参数: {L.STORAGE_CAPEX_CNY_PER_KWH:.0f} 元/kWh, {L.STORAGE_LIFETIME_YEARS}年, 折现率{L.STORAGE_DISCOUNT_RATE:.0%}")
"""),

# -----------------------------------------------------------------18 prov md
("m", r"""## 8 · 数据来源统计
每日数据自带 `data_provenance`，区分真实数据与工程回退，透明可查。"""),

("c", r"""ps = L.provenance_stats(YEAR_DATA)
chname = {"load": "负荷", "generation_mix": "发电结构", "tariff": "电价"}
rows = []
for ch in ["load", "generation_mix", "tariff"]:
    s = ps[ch]
    total = s["real"] + s["fallback"]
    rows.append({"数据通道": chname.get(ch, ch), "真实天数": s["real"],
                 "回退天数": s["fallback"],
                 "真实占比": f"{s['real']/total*100:.1f}%" if total else "-"})
display(pd.DataFrame(rows))"""),

("m", r"""---
## 说明
- 全部优化 / 计费 / 碳排算法来自 `backend/src`，本 notebook 不修改任何源码。
- 全年主配置逐日求解（约 3 分钟）；多模式 / 敏感性用代表性日年化（每季 4 天）。
- 电碳协同采用混合价格法 `price_blend = price + 碳价/1000 × Cr(t)`，无需改求解器。
- 碳因子口径与系统一致：唯一碳信号为 Cr_example 台账责任因子（C 已弃用）。
- 重新运行时数据装配走磁盘缓存，秒级载入；优化重新计算。"""),
]


def main():
    nb = nbf.v4.new_notebook()
    nb["metadata"] = {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python"},
    }
    for kind, src in CELLS:
        if kind == "m":
            nb.cells.append(nbf.v4.new_markdown_cell(src))
        else:
            nb.cells.append(nbf.v4.new_code_cell(src))
    nbf.write(nb, str(OUT))
    print("wrote", OUT, "cells:", len(nb.cells))


if __name__ == "__main__":
    main()
