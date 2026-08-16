"""Phase types and tick data structures shared across day-ahead and real-time.

The energy management system operates in two distinct phases:

1. DAY_AHEAD: Collects data, generates predictions, optimizes storage and HVAC
   plans in parallel, and gates everything behind three engineer approvals.
   No physical commands are issued in this phase.

2. REALTIME: Once the day-ahead plan is fully approved, each 15-minute tick
   reads live measurements, resolves setpoints from the approved plan,
   validates physical constraints, executes commands via the device adapter,
   and archives evidence.  This phase is idempotent per (run_id, step).

The Phase enum and TickResult/TickInput dataclasses formalize the boundary
between these phases so that the SimulationEngine can delegate cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Phase(str, Enum):
    """Operational phase of the energy management system."""

    IDLE = "idle"
    DAY_AHEAD = "day_ahead"
    REALTIME = "realtime"

    def __str__(self) -> str:
        return self.value


@dataclass
class TickInput:
    """All inputs needed to process one real-time tick.

    This is the contract between SimulationEngine and RealtimeTickEngine.
    The engine assembles this from day-ahead outputs + current device state;
    the tick engine consumes it and returns a TickResult.
    """

    run_id: str
    step: int
    sim_time: str
    day_data: dict[str, Any]
    storage_plan: dict[str, Any] | None
    hvac_plan: dict[str, Any] | None
    storage_report_id: str
    storage_report_hash: str
    hvac_report_id: str
    hvac_report_hash: str
    previous_values: dict[str, float]
    demand_cap_kw: float | None
    pending_overrides: dict[str, Any]


@dataclass
class TickResult:
    """Structured output from one real-time tick.

    Contains everything the engine needs to update its state: the execution
    result (if dispatch was active), the computed measurements, daily metric
    deltas, and any alerts that were raised.
    """

    step: int
    execution: Any | None = None
    measurements: dict[str, float] = field(default_factory=dict)
    metrics_delta: dict[str, float] = field(default_factory=dict)
    alerts: list[dict[str, str]] = field(default_factory=list)
    dispatched: bool = False
    skipped: bool = False  # True if tick was idempotently skipped