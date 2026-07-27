"""每日试运行入口（V2.1 spec section 7）。

用法:
  python cases/daily_trial/run_daily.py                      # 今日, min_cost
  python cases/daily_trial/run_daily.py --date 2026-07-28
  python cases/daily_trial/run_daily.py --objective min_carbon
  python cases/daily_trial/run_daily.py --mock               # 用 MockLLM 离线演示
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from energy_agent_v2.llm.client import MockLLMClient, OpenAICompatibleClient  # noqa: E402
from energy_agent_v2.llm.parse_revision import RevisionParser  # noqa: E402
from energy_agent_v2.runner import (  # noqa: E402
    create_app_context, make_initial_state, run_dispatch, print_result, save_result_json,
)


def build_revision_parser(use_mock: bool) -> RevisionParser:
    if use_mock:
        print("  [LLM] Mock 模式 - LLM 解析将使用预置 Mock 响应")
        mock = MockLLMClient([{"slots": {
            "reserve_soc_min_ratio": {"value": 0.3, "confidence": 0.95, "reasoning": "mock"},
            "blocked_intervals": {"value": [{"start_index": 0, "end_index": 24}], "confidence": 0.9, "reasoning": "mock"},
        }}])
        return RevisionParser(client=mock)
    api_key = os.environ.get("LLM_API_KEY", "")
    base_url = os.environ.get("LLM_BASE_URL", "")
    model = os.environ.get("LLM_MODEL", "glm-5")
    if not api_key:
        print("  [LLM] 未检测到 LLM_API_KEY，revise 将回退到结构化表单")
        return RevisionParser(client=MockLLMClient([{"slots": {}}]))
    print(f"  [LLM] 使用 {model} via {base_url or 'default endpoint'}")
    return RevisionParser(client=OpenAICompatibleClient(api_key=api_key, base_url=base_url, model=model))


def interactive_decision(result: dict) -> tuple[str, object | None, str]:
    summary = result.get("factory_summary", {})
    benefits = summary.get("expected_benefits", [])
    risks = summary.get("risks", [])
    print(f"\n{'='*50}")
    print(f"  方案 V{result.get('current_plan_version', 1)} 摘要:")
    if summary.get("executive_summary"):
        print(f"  {summary['executive_summary']}")
    for b in benefits:
        print(f"    + {b}")
    for r in risks:
        print(f"    ! {r}")
    print(f"{'='*50}")
    while True:
        choice = input("\n  请选择 [approve/reject/revise]: ").strip().lower()
        if choice in ("approve", "a"):
            return "approve", None, "approved"
        if choice in ("reject", "r"):
            return "reject", None, "rejected"
        if choice in ("revise", "v"):
            comment = input("  请输入修改意见（自然语言）: ").strip()
            if comment:
                return "revise", None, comment
            print("  修改意见不能为空")


def interactive_clarify(result: dict) -> str:
    interrupts = result.get("__interrupt__", [])
    for item in interrupts:
        val = item.value if hasattr(item, "value") else item
        if isinstance(val, dict) and val.get("type") == "clarify":
            print(f"\n  [解析澄清] 槽位: {val.get('slot', '')}")
            print(f"  {val.get('question', '')}")
            return input("  回答: ").strip() or "yes"
    return "yes"


def main() -> None:
    parser = argparse.ArgumentParser(description="V2.1 每日试运行")
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--objective", type=str, default="min_cost",
                        choices=["min_cost", "min_carbon", "limit_peak_demand"])
    parser.add_argument("--site-id", type=str, default="huanghua")
    parser.add_argument("--requested-by", type=str, default="engineer.daily")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()
    print(f"\n{'='*60}")
    print(f"  V2.1 每日试运行")
    print(f"  日期: {target} | 目标: {args.objective} | 场站: {args.site_id}")
    print(f"{'='*60}")

    rp = build_revision_parser(use_mock=args.mock)
    ctx = create_app_context(revision_parser=rp)
    state = make_initial_state(
        site_id=args.site_id, target_date=target, objective=args.objective,
        requested_by=args.requested_by,
    )
    result = asyncio.run(run_dispatch(
        ctx, state, decision_fn=interactive_decision, clarify_fn=interactive_clarify,
    ))
    print_result(result, label=f"每日试运行完成 ({target})")
    out_path = ROOT / "cases" / "daily_trial" / "results" / f"run_{target}.json"
    save_result_json(result, out_path)
    print(f"\n  运行状态: {result.get('run_status', 'unknown')}")
    if result.get("revision_resolved"):
        print(f"  经验已采集: data/distillation/revise_pairs_{target}.jsonl")


if __name__ == "__main__":
    main()
