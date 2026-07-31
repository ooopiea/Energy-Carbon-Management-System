"""储能审批 Agent：理解工程师指令物理含义，转为 MILP 约束，决定是否重算。

职责范围（V2.3 扩展）：
  1. 接收工程师审批决策（approve / reject / revise + 自然语言 comment）
  2. 若 revise：调用 RevisionParser 提取 slot，理解物理含义
  3. 将自然语言指令转为结构化 DispatchRevision 约束（7 个可解析槽位）
  4. 决定是否触发 re-optimize

可被图节点调用，也可通过独立 API 端点调用（POST /api/agents/approval/interpret）。
"""
from __future__ import annotations

from typing import Any

from energy_agent_v2.contracts import DispatchRevision, StorageOptimizationResult
from energy_agent_v2.llm.base import BaseLLMAgent
from energy_agent_v2.llm.client import LLMClientProtocol
from energy_agent_v2.llm.json_schemas import STORAGE_APPROVAL_SCHEMA
from energy_agent_v2.llm.parse_revision import RevisionParser
from energy_agent_v2.llm.slot_validator import build_revision


class StorageApprovalAgent(BaseLLMAgent):
    """储能审批 Agent：统一入口，封装审批决策 + 修订指令解析 + 重算触发。"""

    _prompt_name = "storage_approval_system.md"
    _schema = STORAGE_APPROVAL_SCHEMA

    def __init__(
        self,
        client: LLMClientProtocol | None = None,
        prompt_dir=None,
        revision_parser: RevisionParser | None = None,
    ) -> None:
        super().__init__(client=client, prompt_dir=prompt_dir)
        self.parser = revision_parser or RevisionParser(
            client=self.client, prompt_dir=self.prompt_dir
        )

    @property
    def revision_parser(self) -> RevisionParser:
        return self.parser

    def interpret_command(
        self,
        comment: str,
        storage_result: StorageOptimizationResult | dict[str, Any],
        clarification_history: list[dict[str, Any]] | None = None,
        validation_errors: list[str] | None = None,
    ) -> dict[str, Any]:
        """单轮解析工程师指令，返回结构化审批结果。"""
        result = self.parser.parse(
            comment, storage_result, clarification_history, validation_errors
        )
        revision = build_revision(result)
        has_revision = self._has_constraints(revision)
        interpretation = self._build_physical_interpretation(
            comment, revision, storage_result
        )
        return {
            "decision": "revise" if has_revision else "approve",
            "revision": revision.model_dump(mode="json"),
            "physical_interpretation": interpretation,
            "should_reoptimize": has_revision,
            "needs_clarification": not result.all_resolved and has_revision,
            "low_confidence_slots": result.low_confidence_slots,
            "all_resolved": result.all_resolved,
            "confidence": self._overall_confidence(result),
            "llm_model": self.llm_model,
        }

    def parse(self, comment: str, storage_result: Any, **kwargs: Any):
        """向下兼容：供图节点直接调用 RevisionParser。"""
        return self.parser.parse(comment, storage_result, **kwargs)

    def generate_clarification(self, result: Any) -> dict[str, Any] | None:
        return self.parser.generate_clarification(result)

    def write_distillation(self, distillation_dir: Any, record: dict[str, Any]):
        """向下兼容：委托内部 parser 的 distillation 写入。"""
        return self.parser.write_distillation(distillation_dir, record)

    @staticmethod
    def _has_constraints(rev: DispatchRevision) -> bool:
        return any(
            v is not None
            for v in [
                rev.terminal_soc_min_ratio,
                rev.reserve_soc_min_ratio,
                rev.max_discharge_power_kw,
                rev.max_charge_power_kw,
                rev.max_cell_temperature_c,
                rev.max_cycles_per_day,
                rev.objective,
            ]
        ) or bool(rev.blocked_intervals)

    @staticmethod
    def _build_physical_interpretation(
        comment: str,
        revision: DispatchRevision,
        storage_result: StorageOptimizationResult | dict[str, Any],
    ) -> str:
        sr = RevisionParser._coerce_storage(storage_result)
        parts: list[str] = []
        if revision.terminal_soc_min_ratio is not None:
            parts.append(
                f"末端SOC下限调至 {revision.terminal_soc_min_ratio:.0%}"
                f"（当前末端 {sr.terminal_soc_ratio:.1%}）"
            )
        if revision.reserve_soc_min_ratio is not None:
            parts.append(f"保留SOC下限 {revision.reserve_soc_min_ratio:.0%}")
        if revision.max_discharge_power_kw is not None:
            cur_max = max((p for p in sr.battery_power_kw if p > 0), default=0)
            parts.append(
                f"放电功率上限 {revision.max_discharge_power_kw:.0f} kW（当前峰值 {cur_max:.0f} kW）"
            )
        if revision.max_charge_power_kw is not None:
            cur_chg = max((-p for p in sr.battery_power_kw if p < 0), default=0)
            parts.append(
                f"充电功率上限 {revision.max_charge_power_kw:.0f} kW（当前峰值 {cur_chg:.0f} kW）"
            )
        if revision.max_cell_temperature_c is not None:
            parts.append(
                f"温度上限 {revision.max_cell_temperature_c:.0f}C（当前最高 {sr.max_cell_temperature_c:.1f}C）"
            )
        if revision.max_cycles_per_day is not None:
            parts.append(f"日循环次数上限 {revision.max_cycles_per_day}")
        if revision.blocked_intervals:
            parts.append(f"禁充放时段 {len(revision.blocked_intervals)} 段")
        if revision.objective is not None:
            parts.append(f"优化目标切换为 {revision.objective.value}")
        if not parts:
            return f"工程师指令: {comment}（未提取到明确约束）"
        return "；".join(parts)

    @staticmethod
    def _overall_confidence(result: Any) -> float:
        slots = getattr(result, "cleaned_slots", {})
        if not slots:
            return 0.0
        confs = [s.confidence for s in slots.values() if s.value is not None]
        return sum(confs) / len(confs) if confs else 0.0
