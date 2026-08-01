"""Append-only JSON/CSV archive for reports, approvals and dispatch evidence."""
from __future__ import annotations

import csv
import json
import os
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.config import DATA_DIR


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class ArchiveStore:
    """Small-data archive using immutable JSON events and a per-run CSV stream."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("ENERGY_ARCHIVE_DIR", DATA_DIR / "archive"))
        self._lock = threading.RLock()

    @staticmethod
    def _safe_segment(value: str) -> str:
        cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
        return cleaned or "unknown"

    def _run_dir(self, sim_date: date, run_id: str) -> Path:
        return self.root / sim_date.isoformat() / self._safe_segment(run_id)

    def _write_event(self, category: str, event_type: str, payload: Any, sim_date: date, run_id: str) -> Path:
        with self._lock:
            directory = self._run_dir(sim_date, run_id) / category
            directory.mkdir(parents=True, exist_ok=True)
            event_id = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
            target = directory / f"{event_id}.json"
            temp = target.with_suffix(".tmp")
            envelope = {
                "event_type": event_type,
                "run_id": run_id,
                "sim_date": sim_date.isoformat(),
                "archived_at": datetime.now().astimezone().isoformat(),
                "payload": _jsonable(payload),
            }
            temp.write_text(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            os.replace(temp, target)
            return target

    def archive_report(self, report: Any, sim_date: date, run_id: str, event_type: str = "report_created") -> Path:
        return self._write_event("reports", event_type, report, sim_date, run_id)

    def archive_approval(self, approval: Any, sim_date: date, run_id: str) -> Path:
        return self._write_event("approvals", "approval_decided", approval, sim_date, run_id)

    def archive_command(self, command: Any, sim_date: date, run_id: str) -> Path:
        return self._write_event("commands", "dispatch_command", command, sim_date, run_id)

    def archive_ack(self, ack: Any, sim_date: date, run_id: str) -> Path:
        return self._write_event("acks", "dispatch_ack", ack, sim_date, run_id)

    def archive_feedback(self, feedback: Any, sim_date: date, run_id: str) -> Path:
        return self._write_event("feedback", "dispatch_feedback", feedback, sim_date, run_id)

    def archive_workflow(self, state: dict[str, Any], sim_date: date, run_id: str) -> Path:
        return self._write_event("workflow", "workflow_snapshot", state, sim_date, run_id)

    def archive_control_action(
        self,
        action: Any,
        sim_date: date,
        run_id: str,
        event_type: str = "control_action_accepted",
    ) -> Path:
        return self._write_event("control_actions", event_type, action, sim_date, run_id)

    def append_realtime(self, row: dict[str, Any], sim_date: date, run_id: str) -> Path:
        """Append one physical-time row; header is created exactly once per run."""
        with self._lock:
            directory = self._run_dir(sim_date, run_id)
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "realtime.csv"
            fields = list(row.keys())
            write_header = not target.exists() or target.stat().st_size == 0
            with target.open("a", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                if write_header:
                    writer.writeheader()
                writer.writerow({key: _jsonable(value) for key, value in row.items()})
            return target
