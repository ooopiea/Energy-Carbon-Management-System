"""异常工况 agent：异常信号 -> LLM 研判告警并决定是否触发重新优化"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from energy_agent_v2.contracts import AnomalyAlert, AnomalySignal
from energy_agent_v2.llm.base import BaseLLMAgent
from energy_agent_v2.llm.json_schemas import ANOMALY_MONITOR_SCHEMA


class AnomalyMonitorAgent(BaseLLMAgent):
    _prompt_name = "anomaly_monitor_system.md"
    _schema = ANOMALY_MONITOR_SCHEMA

    def analyze(self, signals: list[AnomalySignal]) -> AnomalyAlert:
        user_msg = self._build_user_message(signals)
        raw = self._call_llm(user_msg)
        return self._build_alert(signals, raw)

    def _build_user_message(self, signals: list[AnomalySignal]) -> str:
        parts = ["## 异常信号列表"]
        for s in signals:
            parts.append(
                f"- [{s.severity}] {s.source}: {s.description} "
                f"@ {s.timestamp:%Y-%m-%d %H:%M}"
            )
        parts.append("请研判以上信号并输出 JSON。")
        return "\n".join(parts)

    def _build_alert(
        self, signals: list[AnomalySignal], raw: dict[str, Any],
    ) -> AnomalyAlert:
        return AnomalyAlert(
            alert_id=f"alert-{uuid.uuid4().hex[:12]}",
            alert_level=raw.get("alert_level", "info"),
            alert_summary=raw.get("alert_summary", ""),
            root_cause_analysis=raw.get("root_cause_analysis", ""),
            affected_assets=raw.get("affected_assets", []),
            signals=signals,
            recommended_actions=raw.get("recommended_actions", []),
            should_reoptimize=bool(raw.get("should_reoptimize", False)),
            confidence=float(raw.get("confidence", 0.0)),
            llm_model=self.llm_model,
            created_at=datetime.now(UTC),
        )
