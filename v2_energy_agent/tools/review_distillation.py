"""复盘蒸馏 agent 的离线 CLI 工具，批量分析 revise_pairs 记录。

用法：
    python tools/review_distillation.py [--output path.json] [--review-period "2026-07"]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from energy_agent_v2.llm.client import create_llm_client
from energy_agent_v2.llm.distillation_review import DistillationReviewAgent


def main() -> None:
    parser = argparse.ArgumentParser(description="离线复盘 revise_pairs 记录")
    parser.add_argument("--output", "-o", default=None, help="输出 JSON 路径")
    parser.add_argument("--review-period", "-p", default="", help="复盘周期标签")
    args = parser.parse_args()

    client = create_llm_client()
    agent = DistillationReviewAgent(client=client) if client else DistillationReviewAgent()
    insight = agent.review(review_period=args.review_period)

    output = insight.model_dump(mode="json")
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
