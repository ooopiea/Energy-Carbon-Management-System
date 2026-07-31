"""JSON Schema 集中定义。

配合 client.chat_json(response_schema=...) 实现双层 JSON 输出约束：
  1. API 层：response_format={"type": "json_object"} 强制 JSON mode
  2. Prompt 层：schema 嵌入 system prompt，让 LLM 知道输出结构
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 数据清洗 agent：原始文件 -> LLM 判类型 + 字段映射 + 质量检测
# ---------------------------------------------------------------------------
DATA_INGEST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "file_type": {
            "type": "string",
            "description": "判断出的文件类型",
            "enum": ["schedule", "load_forecast", "tariff", "battery_params", "generation_mix", "unknown"],
        },
        "site_id": {"type": "string", "description": "场站编号"},
        "target_date": {"type": "string", "description": "目标日期 ISO 格式 YYYY-MM-DD"},
        "time_granularity_minutes": {"type": "integer", "description": "时间粒度（分钟）", "enum": [15, 30, 60]},
        "field_mappings": {
            "type": "array",
            "description": "原始列名到标准字段的映射列表",
            "items": {
                "type": "object",
                "properties": {
                    "raw_column": {"type": "string"},
                    "standard_field": {
                        "type": "string",
                        "enum": ["timestamp", "load_kw", "price_cny_per_kwh", "soc_ratio",
                                 "power_kw", "generation_source", "generation_kw", "temperature_c"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["raw_column", "standard_field", "confidence"],
            },
        },
        "quality_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_type": {"type": "string", "enum": ["missing_data", "outlier", "format_error", "time_gap", "unit_mismatch"]},
                    "description": {"type": "string"},
                    "severity": {"type": "string", "enum": ["warning", "error"]},
                },
                "required": ["issue_type", "description", "severity"],
            },
        },
        "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["file_type", "field_mappings", "quality_issues", "overall_confidence"],
}

# ---------------------------------------------------------------------------
# 数据封存 agent：清洗结果 -> LLM 生成血缘记录
# ---------------------------------------------------------------------------
DATA_ARCHIVE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "provenance_description": {"type": "string", "description": "数据血缘的一句话描述"},
        "transformations_applied": {
            "type": "array",
            "description": "数据经过的处理步骤列表",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string"},
                    "description": {"type": "string"},
                    "reversible": {"type": "boolean"},
                },
                "required": ["step", "description", "reversible"],
            },
        },
        "compliance_tags": {"type": "array", "items": {"type": "string"}, "description": "合规标签"},
        "retention_recommendation": {"type": "string", "description": "留存建议"},
    },
    "required": ["provenance_description", "transformations_applied", "compliance_tags"],
}

# ---------------------------------------------------------------------------
# 异常工况 agent：异常信号 -> LLM 研判告警
# ---------------------------------------------------------------------------
ANOMALY_MONITOR_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "alert_level": {"type": "string", "enum": ["info", "warning", "critical"], "description": "告警级别"},
        "alert_summary": {"type": "string", "description": "一句话告警概述"},
        "root_cause_analysis": {"type": "string", "description": "根因分析"},
        "affected_assets": {"type": "array", "items": {"type": "string"}, "description": "受影响的设备/场站"},
        "recommended_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "处置建议"},
                    "priority": {"type": "string", "enum": ["immediate", "short_term", "monitor"]},
                    "auto_trigger": {"type": "boolean", "description": "是否允许自动触发 re-optimize"},
                },
                "required": ["action", "priority", "auto_trigger"],
            },
        },
        "should_reoptimize": {"type": "boolean", "description": "是否需要触发重新优化"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["alert_level", "alert_summary", "recommended_actions", "should_reoptimize", "confidence"],
}

# ---------------------------------------------------------------------------
# 复盘蒸馏 agent：revise_pairs 记录 -> LLM 提炼规律
# ---------------------------------------------------------------------------
DISTILLATION_REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "整体复盘总结"},
        "patterns": {
            "type": "array",
            "description": "反复出现的修改模式",
            "items": {
                "type": "object",
                "properties": {
                    "pattern_name": {"type": "string"},
                    "frequency": {"type": "string", "description": "出现频率描述"},
                    "description": {"type": "string"},
                    "affected_slots": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["pattern_name", "frequency", "description"],
            },
        },
        "prompt_improvement_suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "suggestion": {"type": "string"},
                    "target_agent": {"type": "string", "enum": ["parse_revision", "data_ingest", "anomaly_monitor", "general"]},
                    "expected_impact": {"type": "string"},
                },
                "required": ["suggestion", "target_agent", "expected_impact"],
            },
        },
        "new_few_shot_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "代表性工程师修改"},
                    "expected_output": {"type": "string", "description": "期望解析结果"},
                },
                "required": ["input", "expected_output"],
            },
        },
    },
    "required": ["summary", "patterns", "prompt_improvement_suggestions"],
}

# ---------------------------------------------------------------------------
# parse_revision （已有 schema，保持 API 兼容）
# ---------------------------------------------------------------------------
REVISION_SLOTS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "slots": {
            "type": "object",
            "properties": {
                "terminal_soc_min_ratio": {"type": "object"},
                "reserve_soc_min_ratio": {"type": "object"},
                "max_discharge_power_kw": {"type": "object"},
                "max_charge_power_kw": {"type": "object"},
                "blocked_intervals": {"type": "object"},
                "objective": {"type": "object"},
                "max_cycles_per_day": {"type": "object"},
                "max_cell_temperature_c": {"type": "object"},
            },
        },
    },
    "required": ["slots"],
}

# ---------------------------------------------------------------------------
# 储能审批 agent：工程师指令 -> 物理含义解读 + 结构化约束 + 重算决策
# ---------------------------------------------------------------------------
STORAGE_APPROVAL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "reject", "revise"]},
        "physical_interpretation": {"type": "string", "description": "工程师指令的物理含义解读"},
        "should_reoptimize": {"type": "boolean", "description": "是否需要触发重新优化"},
        "needs_clarification": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["decision", "physical_interpretation", "should_reoptimize", "confidence"],
}


ALL_SCHEMAS: dict[str, dict] = {
    "data_ingest": DATA_INGEST_SCHEMA,
    "data_archive": DATA_ARCHIVE_SCHEMA,
    "anomaly_monitor": ANOMALY_MONITOR_SCHEMA,
    "distillation_review": DISTILLATION_REVIEW_SCHEMA,
    "revision_slots": REVISION_SLOTS_SCHEMA,
    "storage_approval": STORAGE_APPROVAL_SCHEMA,
}
