"""State repository seam: separates recoverable runtime state from append-only archive.

G4 fix: ArchiveStore continues as immutable audit evidence; StateRepository
owns the current checkpoint that enables restart-resume.
"""
from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


SCHEMA_VERSION = 1


class RuntimeCheckpoint(BaseModel):
    """Snapshot of recoverable runtime state, saved after every transition."""

    schema_version: int = SCHEMA_VERSION
    run_id: str
    sim_time: str = ""
    day: int = 0
    step: int = -1
    workflow_status: str = "new"
    current_node: str = "idle"
    subgraph: str = "day_ahead"

    # Approval bindings (report_id -> status)
    approval_bindings: dict[str, dict[str, str]] = Field(default_factory=dict)

    # Last completed tick
    last_completed_tick: int | None = None
    last_command_id: str | None = None
    last_ack_accepted: bool | None = None

    # Dispatch state
    dispatch_enabled: bool = False

    # Acked tick steps (for G3 cross-restart idempotency)
    acked_steps: list[int] = Field(default_factory=list)

    # Recovery
    pending_recovery_reason: str = ""
    updated_at: datetime = Field(default_factory=datetime.now)


class StateRepository(ABC):
    """Abstract seam for checkpoint load/save with pluggable adapters."""

    @abstractmethod
    def load(self, run_id: str) -> RuntimeCheckpoint | None:
        """Load the latest checkpoint for a run, or None if not found."""

    @abstractmethod
    def save(self, run_id: str, checkpoint: RuntimeCheckpoint) -> None:
        """Atomically save a checkpoint."""

    @abstractmethod
    def append_event(self, run_id: str, event: dict[str, Any]) -> str:
        """Append an event to the event log and return its event_id."""

    @abstractmethod
    def load_events(self, run_id: str) -> list[dict[str, Any]]:
        """Load all events for a run (chronological order)."""


class InMemoryStateRepository(StateRepository):
    """In-memory adapter for fast unit tests."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, RuntimeCheckpoint] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}

    def load(self, run_id: str) -> RuntimeCheckpoint | None:
        return self._checkpoints.get(run_id)

    def save(self, run_id: str, checkpoint: RuntimeCheckpoint) -> None:
        self._checkpoints[run_id] = checkpoint

    def append_event(self, run_id: str, event: dict[str, Any]) -> str:
        event_id = f"evt-{uuid.uuid4().hex[:12]}"
        self._events.setdefault(run_id, []).append({**event, "event_id": event_id})
        return event_id

    def load_events(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._events.get(run_id, []))


class FileStateRepository(StateRepository):
    """Atomic-file adapter suitable for single-instance demo deployment."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.getenv("ENERGY_STATE_DIR", "data/state"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _cp_path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.checkpoint.json"

    def _events_path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.events.jsonl"

    def load(self, run_id: str) -> RuntimeCheckpoint | None:
        path = self._cp_path(run_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("checkpoint root must be a JSON object")
            if data.get("schema_version") != SCHEMA_VERSION:
                raise ValueError(
                    f"incompatible schema_version {data.get('schema_version')}"
                )
            return RuntimeCheckpoint.model_validate(data)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError, AttributeError):
            # Fail-closed: corrupt or incompatible checkpoint returns None
            # so the caller can start fresh rather than resume garbage.
            return None

    def save(self, run_id: str, checkpoint: RuntimeCheckpoint) -> None:
        target = self._cp_path(run_id)
        temp = target.with_suffix(".tmp")
        temp.write_text(
            checkpoint.model_dump_json(indent=2),
            encoding="utf-8",
        )
        os.replace(temp, target)

    def append_event(self, run_id: str, event: dict[str, Any]) -> str:
        event_id = f"evt-{uuid.uuid4().hex[:12]}"
        path = self._events_path(run_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**event, "event_id": event_id}, ensure_ascii=False) + "\n")
        return event_id

    def load_events(self, run_id: str) -> list[dict[str, Any]]:
        path = self._events_path(run_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events
