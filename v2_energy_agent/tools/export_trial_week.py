"""试运行回放导出工具（V2.1 spec section 8）。

扫描 cases/daily_trial/results/run_*.json + data/distillation/revise_pairs_*.jsonl，
汇总为:
  - trial_week_summary.json  每日运行状态、解析成功率、平均轮数、fallback 次数
  - trial_week_pairs.jsonl   全部蒸馏配对合并（可直接灌入 few_shot_examples.json）
  - trial_week_events.jsonl  全部 events 事件链合并（可回放）

用法:
  python tools/export_trial_week.py
  python tools/export_trial_week.py --output-dir outputs/trial_export
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "cases" / "daily_trial" / "results"
DISTILL_DIR = ROOT / "data" / "distillation"


def load_run_files() -> list[dict]:
    files = sorted(RESULTS_DIR.glob("run_*.json"))
    runs = []
    for f in files:
        try:
            runs.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"  [warn] 跳过 {f.name}: {exc}")
    return runs


def load_distill_files() -> list[dict]:
    files = sorted(DISTILL_DIR.glob("revise_pairs_*.jsonl"))
    pairs = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    pairs.append(json.loads(line))
                except Exception:
                    pass
    return pairs


def build_summary(runs: list[dict], pairs: list[dict]) -> dict:
    daily = []
    for r in runs:
        events = r.get("events", [])
        parse_events = [e for e in events if e.get("node_id") == "parse_revision"]
        storage = r.get("storage_result", {})
        daily.append({
            "run_id": r.get("dispatch_run_id", ""),
            "target_date": r.get("target_date", ""),
            "objective": r.get("objective", ""),
            "run_status": r.get("run_status", ""),
            "plan_version": r.get("current_plan_version", 1),
            "energy_saving_cny": storage.get("energy_cost_saving_cny", 0),
            "had_revise": r.get("revision_resolved", False) or bool(parse_events),
            "parse_summary": [e.get("summary", "") for e in parse_events],
        })
    resolved = [p for p in pairs if p.get("resolved_via") and p["resolved_via"] != "fallback"]
    fallback = [p for p in pairs if p.get("resolved_via") == "fallback"]
    total_rounds = [p.get("total_rounds", 0) for p in pairs]
    return {
        "total_runs": len(runs),
        "total_distill_pairs": len(pairs),
        "parse_success_rate": round(len(resolved) / len(pairs), 3) if pairs else None,
        "fallback_count": len(fallback),
        "avg_rounds": round(sum(total_rounds) / len(total_rounds), 2) if total_rounds else None,
        "daily_breakdown": daily,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="试运行回放导出")
    parser.add_argument("--output-dir", type=str, default="outputs/trial_export")
    args = parser.parse_args()

    out_dir = ROOT / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = load_run_files()
    pairs = load_distill_files()
    if not runs and not pairs:
        print("  未找到任何试运行数据。请先运行 cases/daily_trial/run_daily.py")
        sys.exit(0)

    summary = build_summary(runs, pairs)
    (out_dir / "trial_week_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    with open(out_dir / "trial_week_pairs.jsonl", "w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")

    with open(out_dir / "trial_week_events.jsonl", "w", encoding="utf-8") as f:
        for r in runs:
            for ev in r.get("events", []):
                ev_tagged = {**ev, "_run_id": r.get("dispatch_run_id", ""), "_date": r.get("target_date", "")}
                f.write(json.dumps(ev_tagged, ensure_ascii=False, default=str) + "\n")

    print(f"\n  导出完成 -> {out_dir}")
    print(f"  运行数: {summary['total_runs']} | 蒸馏配对: {summary['total_distill_pairs']}")
    if summary["parse_success_rate"] is not None:
        print(f"  解析成功率: {summary['parse_success_rate']*100:.0f}% | "
              f"平均轮数: {summary['avg_rounds']} | fallback: {summary['fallback_count']}")
    print(f"  输出: trial_week_summary.json, trial_week_pairs.jsonl, trial_week_events.jsonl")


if __name__ == "__main__":
    main()
