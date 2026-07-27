# 案例2：电碳感知调度 —— 含 revise 审批回路的完整流程。
# 目标 objective=min_carbon，先优化出 V1，工程师要求收紧备用 SOC 后 revise，
# 系统重新优化出 V2，最终 approve。演示 revise 回路：
# optimize -> human_approval(revise) -> apply_revision -> optimize(重新) -> human_approval(approve) -> freeze
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from datetime import date  # noqa: E402

from energy_agent_v2.contracts import DispatchObjective  # noqa: E402
from energy_agent_v2.runner import (  # noqa: E402
    create_app_context,
    make_initial_state,
    print_result,
    run_dispatch,
    save_result_json,
)


async def main() -> None:
    print("=" * 60)
    print("  案例2：电碳感知调度（min_carbon -> revise -> re-optimize -> approve）")
    print("=" * 60)

    ctx = create_app_context()
    initial = make_initial_state(
        site_id="huanghua",
        target_date=date(2026, 7, 27),
        objective=DispatchObjective.MIN_CARBON.value,
        requested_by="engineer.li",
    )
    print(f"\n  初始任务: {initial['dispatch_run_id']}")
    print(f"  目标: 最低碳排放（电碳双因子感知）")
    print(f"  审批策略: V1 revise（收紧备用SOC）后重新优化，V2 approve")

    # 用 decision_fn 在同一个 checkpointer 内连续 resume：
    # V1 -> revise（收紧备用 SOC 到 0.3）-> V2 -> approve
    def decide(state):
        if state.get("current_plan_version", 1) == 1:
            return "revise", {"terminal_soc_min_ratio": 0.5, "reserve_soc_min_ratio": 0.3}
        return "approve", None

    result = await run_dispatch(ctx, initial, decision_fn=decide)
    print_result(result, label="案例2 完成：电碳感知调度")

    output_path = Path(__file__).parent / "case2_result.json"
    save_result_json(result, output_path)

    # 验收断言
    assert result["run_status"] == "approved", f"期待 approved, 实际 {result['run_status']}"
    storage = result["storage_result"]
    assert storage["solver_status"] == "Optimal"
    assert storage["constraint_check"]["passed"]
    carbon = result.get("carbon_result")
    if carbon:
        assert carbon["direct_carbon_reduction_kg"] >= 0, "直接碳减排应为非负"
    print("\n  [验收] 全部断言通过 ✓")


if __name__ == "__main__":
    asyncio.run(main())
