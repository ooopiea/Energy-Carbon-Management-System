"""Tests for the four new LLM agents (data ingest / archive / anomaly / distillation)
and the standalone anomaly monitoring graph.

All tests inject MockLLMClient so no real API calls are made.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from energy_agent_v2.contracts import AnomalySignal, RawDataFile
from energy_agent_v2.llm.anomaly_monitor import AnomalyMonitorAgent
from energy_agent_v2.llm.client import MockLLMClient
from energy_agent_v2.llm.data_archive import DataArchiveAgent
from energy_agent_v2.llm.data_ingest import DataIngestAgent
from energy_agent_v2.llm.distillation_review import DistillationReviewAgent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _raw_file() -> RawDataFile:
    return RawDataFile(
        file_name="load_forecast.csv",
        file_path="/tmp/load_forecast.csv",
        file_format="csv",
        uploaded_by="engineer.zhang",
        uploaded_at=datetime.now(UTC),
        raw_content_preview="time,load\n00:00,520.0\n00:15,510.0",
        file_size_bytes=1024,
    )


def _ingest_response() -> dict:
    return {
        "file_type": "load_forecast",
        "site_id": "site-001",
        "target_date": "2026-07-27",
        "time_granularity_minutes": 15,
        "field_mappings": [
            {"raw_column": "time", "standard_field": "timestamp", "confidence": 0.98},
            {"raw_column": "load", "standard_field": "load_kw", "confidence": 0.95},
        ],
        "quality_issues": [
            {"issue_type": "missing_data", "description": "03:00 缺一行", "severity": "warning"},
        ],
        "overall_confidence": 0.93,
    }


def _archive_response() -> dict:
    return {
        "provenance_description": "负荷预测 CSV，经自动列名映射清洗",
        "transformations_applied": [
            {"step": "column_map", "description": "原始列名映射到标准字段", "reversible": True},
        ],
        "compliance_tags": ["auto-derived"],
        "retention_recommendation": "1-year",
    }


def _signal() -> AnomalySignal:
    return AnomalySignal(
        signal_id="sig-1",
        source="battery_bms",
        severity="critical",
        timestamp=datetime.now(UTC),
        description="电芯温度突升至 48C",
    )


def _anomaly_response() -> dict:
    return {
        "alert_level": "critical",
        "alert_summary": "电芯温度超限，存在热失控风险",
        "root_cause_analysis": "冷却系统故障导致散热不足",
        "affected_assets": ["battery-pack-A"],
        "recommended_actions": [
            {"action": "限制放电功率至 50%", "priority": "immediate", "auto_trigger": True},
        ],
        "should_reoptimize": True,
        "confidence": 0.88,
    }


# ---------------------------------------------------------------------------
# DataIngestAgent
# ---------------------------------------------------------------------------

def test_data_ingest_parses_field_mappings():
    agent = DataIngestAgent(client=MockLLMClient(responses=[_ingest_response()]))
    result = agent.ingest(_raw_file())

    assert result.file_type == "load_forecast"
    assert result.site_id == "site-001"
    assert result.overall_confidence == pytest.approx(0.93)
    assert len(result.field_mappings) == 2
    assert result.field_mappings[0].standard_field == "timestamp"
    assert len(result.quality_issues) == 1
    assert result.quality_issues[0].severity == "warning"


def test_data_ingest_fallback_on_empty_llm():
    """MockLLMClient with no canned responses returns {'slots': {}} -> safe defaults."""
    agent = DataIngestAgent(client=MockLLMClient())
    result = agent.ingest(_raw_file())
    assert result.file_type == "unknown"
    assert result.overall_confidence == 0.0


def test_data_ingest_passes_json_schema_to_client():
    mock = MockLLMClient(responses=[_ingest_response()])
    agent = DataIngestAgent(client=mock)
    agent.ingest(_raw_file())
    assert mock.call_log, "agent should have called the LLM client"


# ---------------------------------------------------------------------------
# DataArchiveAgent
# ---------------------------------------------------------------------------

def test_data_archive_writes_file_and_checksum(tmp_path: Path):
    mock = MockLLMClient(responses=[_ingest_response(), _archive_response()])
    ingest_agent = DataIngestAgent(client=mock)
    ingest_result = ingest_agent.ingest(_raw_file())

    archive_agent = DataArchiveAgent(client=mock, archive_dir=tmp_path)
    result = archive_agent.archive(ingest_result)

    assert result.success
    assert result.checksum
    assert result.archive_path.startswith(str(tmp_path))
    assert Path(result.archive_path).exists()
    written = Path(result.archive_path).read_text(encoding="utf-8")
    assert "provenance" in written


# ---------------------------------------------------------------------------
# AnomalyMonitorAgent
# ---------------------------------------------------------------------------

def test_anomaly_monitor_critical_triggers_reoptimize():
    agent = AnomalyMonitorAgent(client=MockLLMClient(responses=[_anomaly_response()]))
    alert = agent.analyze([_signal()])

    assert alert.alert_level == "critical"
    assert alert.should_reoptimize is True
    assert len(alert.recommended_actions) == 1
    assert alert.recommended_actions[0]["auto_trigger"] is True


# ---------------------------------------------------------------------------
# DistillationReviewAgent
# ---------------------------------------------------------------------------

def test_distillation_empty_records_short_circuits():
    agent = DistillationReviewAgent(client=MockLLMClient())
    insight = agent.review(records=None, distillation_dir=Path("/nonexistent"))
    assert "无 revise_pairs" in insight.summary
    assert insight.total_records_analyzed == 0


def test_distillation_summarizes_records():
    records = [
        {"final_revision": {"terminal_soc_min_ratio": 0.25}, "resolved_via": "direct",
         "engineer_comment": "末端 SOC 调高到 0.25"},
        {"final_revision": {"max_discharge_power_kw": 400}, "resolved_via": "clarify",
         "engineer_comment": "放电功率限制 400kW"},
    ]
    response = {
        "summary": "工程师主要调整 SOC 和放电功率",
        "patterns": [{"pattern_name": "soc_adjustment", "frequency": "frequent", "description": "x"}],
        "prompt_improvement_suggestions": [
            {"suggestion": "add SOC few-shot", "target_agent": "parse_revision", "expected_impact": "higher accuracy"},
        ],
    }
    agent = DistillationReviewAgent(client=MockLLMClient(responses=[response]))
    insight = agent.review(records=records, review_period="2026-07")

    assert insight.total_records_analyzed == 2
    assert insight.review_period == "2026-07"
    assert len(insight.patterns) == 1
    assert len(insight.prompt_improvement_suggestions) == 1


# ---------------------------------------------------------------------------
# Monitoring graph (standalone)
# ---------------------------------------------------------------------------

async def test_monitoring_graph_triggers_reoptimize():
    from energy_agent_v2.monitoring import MonitorContext, build_monitoring_graph

    agent = AnomalyMonitorAgent(client=MockLLMClient(responses=[_anomaly_response()]))
    ctx = MonitorContext(monitor_agent=agent)
    graph = build_monitoring_graph()

    result = await graph.ainvoke(
        {"signals": [_signal().model_dump(mode="json")]},
        context=ctx,
    )
    assert result["should_reoptimize"] is True
    event_types = [e["event_type"] for e in result["events"]]
    assert "monitor.reoptimize_trigger" in event_types


async def test_monitoring_graph_no_signals_skips():
    from energy_agent_v2.monitoring import MonitorContext, build_monitoring_graph

    agent = AnomalyMonitorAgent(client=MockLLMClient(responses=[_anomaly_response()]))
    ctx = MonitorContext(monitor_agent=agent)
    graph = build_monitoring_graph()

    result = await graph.ainvoke({"signals": []}, context=ctx)
    assert result["should_reoptimize"] is False
    event_types = [e["event_type"] for e in result["events"]]
    assert "monitor.done" in event_types
