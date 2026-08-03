"""Deterministic safety kernel: hard constraints that no LLM can override (MA-4)."""
from __future__ import annotations

from collaboration.contracts import (
    JointProposal,
    OperationalSnapshot,
    SafetyOutcome,
    SafetyVerdict,
)

# Physical constraint thresholds (from PhysicsConstraints).
SOC_MIN = 0.10
SOC_MAX = 0.90
TEMP_MAX_C = 45.0
SUPPLY_TEMP_MIN_C = 5.0
SUPPLY_TEMP_MAX_C = 12.0
COP_MIN = 3.0


class SafetyKernel:
    """Validates proposals against immutable physical and operational constraints.

    Never calls LLM.  Never softens a hard constraint.
    """

    def validate(
        self,
        proposal: JointProposal,
        current_snapshot: OperationalSnapshot,
    ) -> SafetyVerdict:
        # Stale snapshot check.
        if proposal.snapshot_id != current_snapshot.snapshot_id:
            return SafetyVerdict(
                outcome=SafetyOutcome.STALE_SNAPSHOT,
                violations=[],
                validated_snapshot_id=current_snapshot.snapshot_id,
                details=(
                    f"proposal references snapshot {proposal.snapshot_id} "
                    f"but current is {current_snapshot.snapshot_id}"
                ),
            )

        violations: list[str] = []
        facts = current_snapshot.facts

        soc = facts.get("storage_soc")
        if soc is not None and not (SOC_MIN <= soc <= SOC_MAX):
            violations.append(f"SOC {soc:.1%} outside [{SOC_MIN:.0%}, {SOC_MAX:.0%}]")

        temp = facts.get("storage_temp_c")
        if temp is not None and temp > TEMP_MAX_C:
            violations.append(f"storage temp {temp:.1f}C exceeds {TEMP_MAX_C}C")

        supply = facts.get("hvac_supply_temp_c")
        if supply is not None and not (SUPPLY_TEMP_MIN_C <= supply <= SUPPLY_TEMP_MAX_C):
            violations.append(
                f"supply temp {supply:.1f}C outside [{SUPPLY_TEMP_MIN_C}, {SUPPLY_TEMP_MAX_C}]"
            )

        # Unapproved dispatch check.
        gates = facts.get("approval_gates", {})
        if isinstance(gates, dict):
            for gate_id, gate in gates.items():
                gate_status = gate.get("status", gate) if isinstance(gate, dict) else gate
                if gate_status == "pending_approval" and proposal.actions:
                    violations.append(f"gate {gate_id} still pending for proposal with actions")

        if violations:
            return SafetyVerdict(
                outcome=SafetyOutcome.REJECT,
                violations=violations,
                validated_snapshot_id=current_snapshot.snapshot_id,
            )

        return SafetyVerdict(
            outcome=SafetyOutcome.PASS,
            validated_snapshot_id=current_snapshot.snapshot_id,
        )
