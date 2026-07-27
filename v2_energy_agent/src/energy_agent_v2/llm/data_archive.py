"""数据封存 agent：清洗结果 -> 血缘记录 + 落盘封存"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from energy_agent_v2.contracts import (
    DataArchiveResult,
    DataIngestResult,
    DataProvenance,
)
from energy_agent_v2.llm.base import BaseLLMAgent
from energy_agent_v2.llm.json_schemas import DATA_ARCHIVE_SCHEMA

_DEFAULT_ARCHIVE_DIR = Path(__file__).resolve().parents[2] / "data" / "archived"


class DataArchiveAgent(BaseLLMAgent):
    _prompt_name = "data_archive_system.md"
    _schema = DATA_ARCHIVE_SCHEMA

    def __init__(self, *args: Any, archive_dir: Path | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.archive_dir = archive_dir or _DEFAULT_ARCHIVE_DIR

    def archive(
        self, ingest_result: DataIngestResult, data: dict[str, Any] | None = None,
    ) -> DataArchiveResult:
        data = data or ingest_result.parsed_data
        user_msg = self._build_user_message(ingest_result)
        raw = self._call_llm(user_msg)
        return self._build_and_write(ingest_result, data, raw)

    def _build_user_message(self, ingest_result: DataIngestResult) -> str:
        parts = [
            f"## 清洗结果概览\n文件名: {ingest_result.source_file.file_name}\n"
            f"类型: {ingest_result.file_type}\n置信度: {ingest_result.overall_confidence:.2f}",
        ]
        if ingest_result.field_mappings:
            mapping_lines = "\n".join(
                f"- {m.raw_column} -> {m.standard_field} (conf={m.confidence:.2f})"
                for m in ingest_result.field_mappings
            )
            parts.append(f"## 字段映射\n{mapping_lines}")
        if ingest_result.quality_issues:
            issue_lines = "\n".join(
                f"- [{i.severity}] {i.issue_type}: {i.description}"
                for i in ingest_result.quality_issues
            )
            parts.append(f"## 质量问题\n{issue_lines}")
        parts.append("请生成数据血缘记录并输出 JSON。")
        return "\n\n".join(parts)

    def _build_and_write(
        self, ingest_result: DataIngestResult, data: dict[str, Any], raw: dict[str, Any],
    ) -> DataArchiveResult:
        source = ingest_result.source_file
        ts = int(datetime.now(UTC).timestamp())
        data_version = f"archive-{source.file_name}-{ts}"
        provenance = DataProvenance(
            source_file=source,
            data_version=data_version,
            provenance_description=raw.get("provenance_description", ""),
            transformations_applied=raw.get("transformations_applied", []),
            compliance_tags=raw.get("compliance_tags", []),
            retention_recommendation=raw.get("retention_recommendation", ""),
        )
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.archive_dir / f"{data_version}.json"
        payload = {"provenance": provenance.model_dump(mode="json"), "data": data}
        archive_content = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        archive_path.write_text(archive_content, encoding="utf-8")
        checksum = hashlib.sha256(archive_content.encode("utf-8")).hexdigest()[:16]
        provenance.archived_path = str(archive_path)
        provenance.archived_at = datetime.now(UTC)
        return DataArchiveResult(
            provenance=provenance,
            archive_path=str(archive_path),
            archive_version=data_version,
            checksum=checksum,
            success=True,
        )
