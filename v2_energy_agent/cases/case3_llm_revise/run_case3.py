"""Case 3: LLM-powered revise intent parsing (V2.1 c-line validation).

Tests the full parse_revision loop.
优先接真实 GLM-5（读环境变量 LLM_API_KEY + LLM_BASE_URL）；
未配置时回退 MockLLMClient 并明确告知。
- V1 generated (min_cost)
- Engineer gives natural-language revise comment
- LLM parses to structured DispatchRevision (with 1 clarification round)
- V2 re-optimized and approved
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BREAK = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BREAK / "src"))

from datetime import UTC, date, datetime, timedelta  # noqa: E402

from energy_agent_v2.contracts import RunStatus  # noqa: E402
from energy_agent_v2.llm.client import MockLLMClient, create_llm_client  # noqa: E402
from energy_agent_v2.llm.parse_revision import RevisionParser  # noqa: E402
from energy_agent_v2.runner import (  # noqa: E402
    create_app_context,
    make_initial_state,
    run_dispatch,
    print_result,
    save_result_json,
)

TARGET = date(2026, 7, 27)

# Mock LLM responses simulating engineer comment:
# "SOC 太低了改到 0.3，夜里别放电"
MOCK_RESPONSES = [
    # Round 1: parse comment
    {
        "slots": {
            "terminal_soc_min_ratio": {"value": None, "confidence": 0.0, "reasoning": "未提及"},
            "reserve_soc_min_ratio": {"value": 0.3, "confidence": 0.98, "reasoning": "工程师明确说改到0.3"},
            "max_discharge_power_kw": {"value": None, "confidence": 0.0, "reasoning": "未提及"},
            "blocked_intervals": {
                "value": [{"start_index": 0, "end_index": 28}, {"start_index": 92, "end_index": 95}],
                "confidence": 0.6,
                "reasoning": "工程师说夜里，推断23:00-07:00，但夜里起止有歧义",
            },
            "objective": {"value": None, "confidence": 0.0, "reasoning": "未提及"},
        }
    },
    # Round 2: after clarification "confirm 23:00-07:00"
    {
        "slots": {
            "terminal_soc_min_ratio": {"value": None, "confidence": 0.0, "reasoning": "未提及"},
            "reserve_soc_min_ratio": {"value": 0.3, "confidence": 0.99, "reasoning": "工程师明确说改到0.3"},
            "max_discharge_power_kw": {"value": None, "confidence": 0.0, "reasoning": "未提及"},
            "blocked_intervals": {
                "value": [{"start_index": 0, "end_index": 28}, {"start_index": 92, "end_index": 95}],
                "confidence": 0.95,
                "reasoning": "工程师确认23:00-07:00",
            },
            "objective": {"value": None, "confidence": 0.0, "reasoning": "未提及"},
        }
    },
]


async def main() -> None:
    # 优先接真实大模型；未配置环境变量时回退 Mock
    real_client = create_llm_client()
    if real_client is not None:
        print("[case3] 使用真实 GLM-5 大模型解析")
        parser = RevisionParser(client=real_client, confidence_threshold=0.7, max_rounds=2)
    else:
        print("[case3] LLM_API_KEY 未配置，回退 MockLLMClient（预设响应）")
        mock = MockLLMClient(MOCK_RESPONSES)
        parser = RevisionParser(client=mock, confidence_threshold=0.7, max_rounds=2)
    ctx = create_app_context(revision_parser=parser)

    state = make_initial_state(
        site_id="huanghua", target_date=TARGET, objective="min_cost",
        requested_by="engineer.case3",
    )

    approval_count = [0]

    def decision_fn(result):
        approval_count[0] += 1
        if approval_count[0] == 1:
            return "revise", None, "SOC 太低了改到 0.3，夜里别放电"
        return "approve", None

    def clarify_fn(result):
        return "确认 23:00-07:00"

    result = await run_dispatch(
        ctx, state,
        decision_fn=decision_fn,
        clarify_fn=clarify_fn,
    )

    # Build custom decision sequence: we need revise on first approval,
    # then clarify answer, then approve on second approval
    # The runner handles interrupt types automatically

    print_result(result, "Case 3: LLM Revise Parsing")

    # Assertions
    assert result["run_status"] == RunStatus.APPROVED.value, f"Expected approved, got {result['run_status']}"
    assert result["current_plan_version"] == 2, f"Expected V2, got V{result['current_plan_version']}"
    sr = result["storage_result"]
    assert sr["solver_status"] == "Optimal"
    assert sr["constraint_check"]["passed"]
    assert any(ev.get("node_id") == "parse_revision" for ev in result.get("events", [])), "Missing parse_revision event"
    assert result.get("revision_resolved") is True, "revision_resolved should be True"
    rev = result.get("revision_request", {})
    assert rev.get("reserve_soc_min_ratio") == 0.3, f"Expected SOC 0.3, got {rev.get('reserve_soc_min_ratio')}"

    out = BREAK / "cases" / "case3_llm_revise" / "case3_result.json"
    save_result_json(result, out)

    print("\n  [验收] 全部断言通过 ✓")


if __name__ == "__main__":
    asyncio.run(main())
