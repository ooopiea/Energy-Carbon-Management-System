"""案例1：储能经济调度 —— 从优化到审批的完整 LangGraph 流程。

目标 objective=min_cost，MILP 求解储能充放电计划，经人工审批(approve)后冻结。
演示节点链路：initialize_run -> load_inputs -> compute_tariff -> optimize_storage
-> compute_carbon -> factory_summary -> human_approval(approve) -> freeze_plan -> END
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 将 src 加入路径
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
    print("  案例1：储能经济调度（min_cost → approve）")
    print("=" * 60)

    ctx = create_app_context()
    initial = make_initial_state(
        site_id="huanghua",
        target_date=date(2026, 7, 27),
        objective=DispatchObjective.MIN_COST.value,
        requested_by="engineer.zhang",
    )
    print(f"\n  初始任务: {initial['dispatch_run_id']}")
    print(f"  目标: 最低电费")
    print(f"  场站: 黄花园区 | 日期: 2026-07-27")

    result = await run_dispatch(ctx, initial, approval_decision="approve")
    print_result(result, label="案例1 完成：储能经济调度")

    output_path = Path(__file__).parent / "case1_result.json"
    save_result_json(result, output_path)

    # 验收断言
    assert result["run_status"] == "approved", f"期望 approved, 实际 {result['run_status']}"
    storage = result["storage_result"]
    assert storage["solver_status"] == "Optimal"
    assert storage["energy_cost_saving_cny"] > 0, "优化后电费应低于基线"
    assert storage["constraint_check"]["passed"], "硬约束校验应通过"
    print("\n  [验收] 全部断言通过 ✓")


if __name__ == "__main__":
    asyncio.run(main())
