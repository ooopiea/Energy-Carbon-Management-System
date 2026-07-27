# -*- coding: utf-8 -*-
"""读取两个 case 的真实结果（含完整 events 事件链），注入 template.html 生成可视化网页。
步骤不再硬编码，而是从 LangGraph 真实 events 回放生成。
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
VIZ = Path(r"C:\\Users\\HW\\.codex\\visualizations\\2026\\07\\27\\019fa2de-0cb5-7b52-a2e7-203b196c2972")

NODE_TITLES = {
    "initialize_run": "初始化运行",
    "load_inputs": "加载日前数据",
    "compute_tariff": "电费核算",
    "optimize_storage": "MILP 储能优化",
    "compute_carbon": "电碳双因子计量",
    "factory_summary": "审批摘要",
    "human_approval": "人工审批",
    "apply_revision": "合并修改",
    "freeze_plan": "冻结方案",
    "close_rejected": "关闭拒绝",
    "fail_run": "失败关闭",
    "parse_revision": "LLM 修改解析",
}


def load_case(case_dir: str, fname: str) -> dict:
    p = BASE / "cases" / case_dir / fname
    r = json.loads(p.read_text(encoding="utf-8"))
    sr = r["storage_result"]
    cr = r["carbon_result"]
    ca = cr["carbon_accounting"]
    tf = r["tariff_result"]
    slim = {
        "objective": r["objective"],
        "run_status": r["run_status"],
        "plan_version": r["current_plan_version"],
        "price": r["input_bundle"]["electricity_price_cny_per_kwh"],
        "load": r["input_bundle"]["load_forecast_kw"],
        "battery_power": sr["battery_power_kw"],
        "soc": sr["soc_ratio"],
        "temp": sr["cell_temperature_c"],
        "C_factor": ca["direct_factor_c_kg_per_kwh"],
        "Cr_factor": ca["responsibility_factor_cr_kg_per_kwh"],
        "grid_base": sr["baseline_grid_import_power_kw"],
        "grid_opt": sr["grid_import_power_kw"],
        "saving": sr["energy_cost_saving_cny"],
        "peak_cut": sr["peak_reduction_kw"],
        "peak_base": sr["baseline_peak_demand_kw"],
        "max_temp": sr["max_cell_temperature_c"],
        "direct_reduction": cr["direct_carbon_reduction_kg"],
        "resp_reduction": cr["responsibility_carbon_reduction_kg"],
        "tariff_total": tf["total_cost_cny"],
        "tariff_energy": tf["energy_cost_cny"],
        "tariff_gov_fund": tf.get("gov_fund_cost_cny", 0),
        "tariff_reactive": tf.get("reactive_adjustment_cny", 0),
        "tariff_demand": tf["demand_cost_cny"],
        "events": r.get("events", []),
    }
    slim["steps"] = build_steps(slim)
    return slim


def build_steps(d: dict) -> list[dict]:
    steps = []
    for i, ev in enumerate(d["events"], 1):
        node = ev.get("node_id", "?")
        title = NODE_TITLES.get(node, node)
        summary = ev.get("summary", "")
        dur = ev.get("duration_ms")
        detail = summary + (f" · {dur}ms" if dur else "")
        status = ev.get("status", "succeeded")
        steps.append({"id": node, "title": f"{i}. {title}", "detail": detail, "status": status})
    return steps


def main() -> None:
    c1 = load_case("case1_storage_dispatch", "case1_result.json")
    c2 = load_case("case2_carbon_aware", "case2_result.json")
    c3 = load_case("case3_llm_revise", "case3_result.json")
    payload = json.dumps({"case1": c1, "case2": c2, "case3": c3}, ensure_ascii=False)
    tpl = (BASE / "template.html").read_text(encoding="utf-8")
    html = tpl.replace("__DATA__", payload)
    local_out = BASE / "index.html"
    local_out.write_text(html, encoding="utf-8")
    viz_out = VIZ / "index.html"
    viz_out.write_text(html, encoding="utf-8")
    print(f"done: {local_out} ({local_out.stat().st_size // 1024} KB)")
    print(f"case1 steps={len(c1['steps'])} | case2 steps={len(c2['steps'])} | case3 steps={len(c3['steps'])}")


if __name__ == "__main__":
    main()
