"""Core parse_revision logic: LLM call + slot extraction + clarification + distillation.

Spec sections 3, 5, 6.  Called by the orchestration parse_revision node.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from energy_agent_v2.contracts import StorageOptimizationResult
from energy_agent_v2.llm.client import LLMClientProtocol, MockLLMClient
from energy_agent_v2.llm.slot_validator import (
    SlotInfo,
    ValidationResult,
    build_revision,
    validate_slots,
)
from energy_agent_v2.llm.json_schemas import REVISION_SLOTS_SCHEMA

logger = logging.getLogger(__name__)
_DEFAULT_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


class RevisionParser:
    """Orchestrates LLM-based revise-comment parsing. Stateless between calls."""

    def __init__(
        self,
        client: LLMClientProtocol | None = None,
        prompt_dir: Path | None = None,
        confidence_threshold: float = 0.7,
        max_rounds: int = 2,
    ) -> None:
        self.client = client or MockLLMClient()
        self.prompt_dir = prompt_dir or _DEFAULT_PROMPT_DIR
        self.confidence_threshold = confidence_threshold
        self.max_rounds = max_rounds
        self._system_prompt = self._load_prompt("parse_revision_system.md")
        self._clarification_templates = self._load_json("clarification_templates.json")
        self._few_shot = self._load_few_shot()

    def parse(
        self,
        comment: str,
        storage_result: StorageOptimizationResult | dict[str, Any],
        clarification_history: list[dict[str, Any]] | None = None,
        validation_errors: list[str] | None = None,
    ) -> ValidationResult:
        """One LLM round. Returns ValidationResult with cleaned slots."""
        user_msg = self._build_user_message(
            comment, storage_result, clarification_history, validation_errors
        )
        raw = self.client.chat_json(
            system_prompt=self._system_prompt,
            user_message=user_msg,
            few_shot=self._few_shot,
            response_schema=REVISION_SLOTS_SCHEMA,
        )
        slots = raw.get("slots", raw)
        sr = self._coerce_storage(storage_result)
        return validate_slots(
            slots,
            current_min_soc=min(sr.soc_ratio) if sr.soc_ratio else 0.0,
            current_max_discharge_kw=max(
                (p for p in sr.battery_power_kw if p > 0), default=float("inf")
            ),
            confidence_threshold=self.confidence_threshold,
        )

    def generate_clarification(self, result: ValidationResult) -> dict[str, Any] | None:
        """Pick the lowest-confidence slot and build a clarification question."""
        if not result.low_confidence_slots:
            return None
        slot_name = min(
            result.low_confidence_slots,
            key=lambda s: result.cleaned_slots.get(s, SlotInfo(None, 0.0)).confidence,
        )
        info = result.cleaned_slots.get(slot_name, SlotInfo(None, 0.0))
        template = self._clarification_templates.get(
            slot_name, f"请进一步说明你对 {slot_name} 的要求。"
        )
        question = template.format(current_max=info.value or "")
        return {
            "slot": slot_name,
            "question": question,
            "current_guess": info.value,
            "guess_reasoning": info.reasoning,
        }

    def write_distillation(self, distillation_dir: Path, record: dict[str, Any]) -> Path:
        distillation_dir.mkdir(parents=True, exist_ok=True)
        date_str = record.get("timestamp", "")[:10] or "unknown"
        path = distillation_dir / f"revise_pairs_{date_str}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return path

    @staticmethod
    def _coerce_storage(sr: StorageOptimizationResult | dict[str, Any]) -> StorageOptimizationResult:
        if isinstance(sr, dict):
            try:
                return StorageOptimizationResult.model_validate(sr)
            except Exception:
                return StorageOptimizationResult.model_validate({
                    "plan_id": "unknown", "plan_version": 0, "site_id": "unknown",
                    "target_date": "2026-01-01", "start_at": "2026-01-01T00:00",
                    "time_step_minutes": 15, "point_count": 0, "timestamps": [],
                    "load_forecast_kw": [], "battery_power_kw": [], "soc_ratio": [],
                    "cell_temperature_c": [], "baseline_grid_import_power_kw": [],
                    "grid_import_power_kw": [], "electricity_price_cny_per_kwh": [],
                    "baseline_energy_cost_cny": 0, "optimized_energy_cost_cny": 0,
                    "energy_cost_saving_cny": 0, "baseline_peak_demand_kw": 0,
                    "optimized_peak_demand_kw": 0, "peak_reduction_kw": 0,
                    "terminal_soc_ratio": 0, "max_cell_temperature_c": 0,
                    "constraint_check": {"passed": True, "violations": []},
                    "solver_status": "Unknown", "solve_duration_ms": 0,
                    "algorithm_version": "unknown", "agent_version": "unknown",
                    "data_version": "unknown", "objective": "min_cost",
                })
        return sr

    def _build_user_message(
        self, comment: str, storage_result: StorageOptimizationResult | dict[str, Any],
        clarification_history: list[dict[str, Any]] | None, validation_errors: list[str] | None,
    ) -> str:
        sr = self._coerce_storage(storage_result)
        parts: list[str] = []
        parts.append(f"## 工程师修改意见\n{comment}\n")
        parts.append("## 当前方案摘要")
        parts.append(f"- 优化目标: {sr.objective.value}")
        parts.append(f"- 方案版本: V{sr.plan_version}")
        parts.append(f"- 电费节省: {sr.energy_cost_saving_cny:.0f} 元/日")
        parts.append(f"- 峰值削减: {sr.peak_reduction_kw:.0f} kW")
        parts.append(f"- 最高温度: {sr.max_cell_temperature_c:.1f} C")
        parts.append(f"- 末端SOC: {sr.terminal_soc_ratio:.3f}")
        # 关键摘要值（不传完整序列，减少 token 加速 LLM 响应）
        soc_values = sr.soc_ratio or [0]
        parts.append(f"- 最低SOC: {min(soc_values):.3f}")
        discharge_values = [p for p in (sr.battery_power_kw or []) if p > 0]
        parts.append(f"- 最大放电功率: {max(discharge_values, default=0):.0f} kW")
        parts.append(f"- 充放电时段数: {sum(1 for p in (sr.battery_power_kw or []) if abs(p) > 1)}")
        parts.append("")
        if clarification_history:
            parts.append("## 澄清历史")
            for qa in clarification_history:
                parts.append(f"Q: {qa.get('question', '')}")
                parts.append(f"A: {qa.get('answer', '')}")
            parts.append("")
        if validation_errors:
            parts.append("## 上轮校验错误（请修正）")
            for err in validation_errors:
                parts.append(f"- {err}")
            parts.append("")
        parts.append("请解析工程师意见并输出 JSON。")
        return "\n".join(parts)

    def _load_prompt(self, name: str) -> str:
        path = self.prompt_dir / name
        if path.exists():
            return path.read_text(encoding="utf-8")
        return "你是储能调度审批修改解析器。解析工程师意见为结构化JSON。"

    def _load_json(self, name: str) -> dict[str, Any]:
        path = self.prompt_dir / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}

    def _load_few_shot(self) -> str:
        path = self.prompt_dir / "few_shot_examples.json"
        if not path.exists():
            return ""
        examples = json.loads(path.read_text(encoding="utf-8"))
        if not examples:
            return ""
        return json.dumps(examples, ensure_ascii=False, indent=2)


def build_distillation_record(
    *, dispatch_run_id: str, plan_version: int, engineer_comment: str,
    storage_result: StorageOptimizationResult | dict[str, Any],
    llm_rounds: list[dict[str, Any]], final_revision: dict[str, Any],
    resolved_via: str, total_rounds: int, llm_model: str,
) -> dict[str, Any]:
    sr = RevisionParser._coerce_storage(storage_result)
    return {
        "timestamp": sr.start_at.isoformat() if hasattr(sr.start_at, "isoformat") else str(sr.start_at),
        "dispatch_run_id": dispatch_run_id,
        "plan_version": plan_version,
        "engineer_comment": engineer_comment,
        "storage_result_summary": {
            "objective": sr.objective.value,
            "current_min_soc": min(sr.soc_ratio) if sr.soc_ratio else 0.0,
            "current_max_discharge_kw": max((p for p in sr.battery_power_kw if p > 0), default=0.0),
            "max_cell_temp_c": sr.max_cell_temperature_c,
        },
        "llm_rounds": llm_rounds,
        "final_revision": final_revision,
        "resolved_via": resolved_via,
        "total_rounds": total_rounds,
        "llm_model": llm_model,
    }
