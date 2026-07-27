"""数据清洗 agent：原始文件 -> LLM 判类型 + 字段映射 + 质量检测"""
from __future__ import annotations

from typing import Any

from energy_agent_v2.contracts import (
    DataIngestResult,
    DataQualityIssue,
    FieldMapping,
    RawDataFile,
)
from energy_agent_v2.llm.base import BaseLLMAgent
from energy_agent_v2.llm.json_schemas import DATA_INGEST_SCHEMA


class DataIngestAgent(BaseLLMAgent):
    _prompt_name = "data_ingest_system.md"
    _schema = DATA_INGEST_SCHEMA

    def ingest(self, raw_file: RawDataFile) -> DataIngestResult:
        user_msg = self._build_user_message(raw_file)
        raw = self._call_llm(user_msg)
        return self._build_result(raw_file, raw)

    def _build_user_message(self, raw_file: RawDataFile) -> str:
        parts = [
            f"## 原始文件信息\n文件名: {raw_file.file_name}\n格式: {raw_file.file_format}\n"
            f"大小: {raw_file.file_size_bytes} bytes\n上传者: {raw_file.uploaded_by}"
        ]
        if raw_file.raw_content_preview:
            parts.append(f"## 内容预览\n{raw_file.raw_content_preview}")
        parts.append("请分析以上文件并输出 JSON。")
        return "\n\n".join(parts)

    def _build_result(self, raw_file: RawDataFile, raw: dict[str, Any]) -> DataIngestResult:
        mappings = [
            FieldMapping(
                raw_column=m.get("raw_column", ""),
                standard_field=m.get("standard_field", ""),
                confidence=float(m.get("confidence", 0.0)),
            )
            for m in raw.get("field_mappings", [])
        ]
        issues = [
            DataQualityIssue(
                issue_type=i.get("issue_type", "format_error"),
                description=i.get("description", ""),
                severity=i.get("severity", "warning"),
            )
            for i in raw.get("quality_issues", [])
        ]
        return DataIngestResult(
            source_file=raw_file,
            file_type=raw.get("file_type", "unknown"),
            site_id=raw.get("site_id", ""),
            target_date=raw.get("target_date"),
            time_granularity_minutes=raw.get("time_granularity_minutes", 15),
            field_mappings=mappings,
            quality_issues=issues,
            overall_confidence=float(raw.get("overall_confidence", 0.0)),
            llm_model=self.llm_model,
            parsed_data=raw,
        )
