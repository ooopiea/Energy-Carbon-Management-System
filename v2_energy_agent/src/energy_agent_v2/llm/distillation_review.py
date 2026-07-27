"""复盘蒸馏 agent：revise_pairs 记录 -> LLM 提炼规律和改进建议"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from energy_agent_v2.contracts import DistillationInsight
from energy_agent_v2.llm.base import BaseLLMAgent
from energy_agent_v2.llm.json_schemas import DISTILLATION_REVIEW_SCHEMA

_DEFAULT_DISTILLATION_DIR = Path(__file__).resolve().parents[2] / "data" / "distillation"


class DistillationReviewAgent(BaseLLMAgent):
    _prompt_name = "distillation_review_system.md"
    _schema = DISTILLATION_REVIEW_SCHEMA

    def review(
        self,
        records: list[dict[str, Any]] | None = None,
        *,
        distillation_dir: Path | None = None,
        review_period: str = "",
    ) -> DistillationInsight:
        if records is None:
            records = self._load_records(distillation_dir)
        if not records:
            return DistillationInsight(
                summary="无 revise_pairs 记录可分析",
                review_period=review_period,
                generated_at=datetime.now(UTC),
                llm_model=self.llm_model,
            )
        stats = self._summarize(records)
        user_msg = self._build_user_message(stats)
        raw = self._call_llm(user_msg)
        return self._build_insight(raw, len(records), review_period)

    def _load_records(self, distillation_dir: Path | None) -> list[dict[str, Any]]:
        d = distillation_dir or _DEFAULT_DISTILLATION_DIR
        records: list[dict[str, Any]] = []
        if d.exists():
            for f in sorted(d.glob("revise_pairs_*.jsonl")):
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        return records

    def _summarize(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        slot_freq: dict[str, int] = {}
        resolved_via: dict[str, int] = {}
        for r in records:
            revision = r.get("final_revision", {})
            for k, v in revision.items():
                if v is not None:
                    slot_freq[k] = slot_freq.get(k, 0) + 1
            via = r.get("resolved_via", "unknown")
            resolved_via[via] = resolved_via.get(via, 0) + 1
        comments = [
            r.get("engineer_comment", "")[:80]
            for r in records
            if r.get("engineer_comment")
        ]
        return {
            "total": len(records),
            "slot_frequency": slot_freq,
            "resolved_via": resolved_via,
            "sample_comments": comments[:10],
        }

    def _build_user_message(self, stats: dict[str, Any]) -> str:
        parts = [
            "## revise_pairs 统计概览",
            f"记录总数: {stats['total']}",
            f"槽位频率: {json.dumps(stats['slot_frequency'], ensure_ascii=False)}",
            f"解析路径: {json.dumps(stats['resolved_via'], ensure_ascii=False)}",
            "## 工程师修改样本",
        ]
        for c in stats.get("sample_comments", []):
            parts.append(f"- {c}")
        parts.append("请分析以上数据并输出 JSON。")
        return "\n".join(parts)

    def _build_insight(
        self, raw: dict[str, Any], total: int, review_period: str,
    ) -> DistillationInsight:
        return DistillationInsight(
            summary=raw.get("summary", ""),
            patterns=raw.get("patterns", []),
            prompt_improvement_suggestions=raw.get("prompt_improvement_suggestions", []),
            new_few_shot_candidates=raw.get("new_few_shot_candidates", []),
            review_period=review_period,
            total_records_analyzed=total,
            llm_model=self.llm_model,
            generated_at=datetime.now(UTC),
        )
