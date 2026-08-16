"""Day-ahead phase: tracks the planning workflow state machine.

The day-ahead phase covers:
  data collection -> prediction -> forecast approval
  -> parallel (storage optimization || hvac optimization)
  -> dispatch approvals (storage + hvac)
  -> physical dispatch activation (handoff to real-time)

This module provides:
- DayAheadPlan: frozen snapshot of approved plans + bindings handed to real-time.
- DayAheadPhase: state machine that validates transitions and approval completeness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Day-ahead workflow statuses (the values used in workflow_status state field).
DA_NEW = "new"
DA_AWAITING_FORECAST = "awaiting_forecast_approval"
DA_FORECAST_APPROVED = "forecast_approved"
DA_STORAGE_REVISION = "storage_revision"
DA_HVAC_REVISION = "hvac_revision"
DA_FORECAST_REVISION = "forecast_revision"
DA_DISPATCH_APPROVED = "dispatch_approved"
DA_REJECTED = "rejected"
DA_ACTIVE = "active"  # physical dispatch activated, transitioning to real-time


# Statuses that belong to the day-ahead phase (before physical dispatch).
DAY_AHEAD_STATUSES = frozenset({
    DA_NEW, DA_AWAITING_FORECAST, DA_FORECAST_APPROVED,
    DA_STORAGE_REVISION, DA_HVAC_REVISION, DA_FORECAST_REVISION,
    DA_DISPATCH_APPROVED, DA_REJECTED,
})


@dataclass(frozen=True)
class DayAheadPlan:
    """Immutable handoff from day-ahead phase to real-time phase.

    Contains the approved storage/HVAC plans and the report bindings
    that authorize physical dispatch.  Once created, this object
    must not change -- it is the contract between the two phases.
    """

    run_id: str
    day: int
    storage_plan: dict[str, Any]
    hvac_plan: dict[str, Any]
    storage_report_id: str
    storage_report_hash: str
    hvac_report_id: str
    hvac_report_hash: str


class DayAheadPhase:
    """Validates the day-ahead approval chain and phase transitions.

    This is a pure-logic helper with no side effects.  The engine calls
    these methods to check whether a transition is valid before mutating
    its own state.
    """

    @staticmethod
    def is_day_ahead_status(workflow_status: str) -> bool:
        """True if the workflow is still in the day-ahead planning phase."""
        return workflow_status in DAY_AHEAD_STATUSES

    @staticmethod
    def is_realtime_status(workflow_status: str) -> bool:
        """True if the workflow has entered the real-time execution phase."""
        return workflow_status == DA_ACTIVE

    @staticmethod
    def can_activate_dispatch(
        storage_gate_status: str,
        hvac_gate_status: str,
    ) -> bool:
        """Both storage and HVAC gates must be approved to activate dispatch."""
        return storage_gate_status == "approved" and hvac_gate_status == "approved"

    @staticmethod
    def build_plan(
        run_id: str,
        day: int,
        storage_plan: dict[str, Any] | None,
        hvac_plan: dict[str, Any] | None,
        storage_report_id: str | None,
        storage_report_hash: str | None,
        hvac_report_id: str | None,
        hvac_report_hash: str | None,
    ) -> DayAheadPlan | None:
        """Create a DayAheadPlan if all required components are present.

        Returns None if any plan or binding is missing, which means
        the day-ahead phase is not yet complete.
        """
        if not storage_plan or not hvac_plan:
            return None
        if not storage_report_id or not storage_report_hash:
            return None
        if not hvac_report_id or not hvac_report_hash:
            return None
        return DayAheadPlan(
            run_id=run_id,
            day=day,
            storage_plan=storage_plan,
            hvac_plan=hvac_plan,
            storage_report_id=storage_report_id,
            storage_report_hash=storage_report_hash,
            hvac_report_id=hvac_report_id,
            hvac_report_hash=hvac_report_hash,
        )