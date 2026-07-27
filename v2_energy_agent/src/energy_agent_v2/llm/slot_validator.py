"""Three-layer slot validation for parse_revision LLM output.

Layer 1: Pydantic -- each non-null value must construct a valid field.
Layer 2: Semantic tightening -- SOC fields must not decrease, power must not increase.
Layer 3: Confidence -- below threshold marks slot for clarification (not failure).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from energy_agent_v2.contracts import DispatchRevision, TimeInterval


@dataclass
class SlotInfo:
    value: Any
    confidence: float
    reasoning: str = ""


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    low_confidence_slots: list[str] = field(default_factory=list)
    cleaned_slots: dict[str, SlotInfo] = field(default_factory=dict)

    @property
    def needs_clarification(self) -> bool:
        return self.valid and len(self.low_confidence_slots) > 0

    @property
    def all_resolved(self) -> bool:
        return self.valid and len(self.low_confidence_slots) == 0


_NUMERIC_SLOTS = {"terminal_soc_min_ratio", "reserve_soc_min_ratio", "max_discharge_power_kw"}


def validate_slots(
    raw_slots: dict[str, Any],
    *,
    current_min_soc: float = 0.0,
    current_max_discharge_kw: float = float("inf"),
    confidence_threshold: float = 0.7,
) -> ValidationResult:
    errors: list[str] = []
    low_conf: list[str] = []
    cleaned: dict[str, SlotInfo] = {}

    for slot_name, raw in raw_slots.items():
        value = raw.get("value") if isinstance(raw, dict) else None
        confidence = float(raw.get("confidence", 0.0)) if isinstance(raw, dict) else 0.0
        reasoning = str(raw.get("reasoning", "")) if isinstance(raw, dict) else ""

        if value is None:
            cleaned[slot_name] = SlotInfo(value=None, confidence=confidence, reasoning=reasoning)
            if confidence < confidence_threshold and confidence > 0:
                low_conf.append(slot_name)
            continue

        # Layer 1: Pydantic / type validation
        try:
            if slot_name in ("terminal_soc_min_ratio", "reserve_soc_min_ratio"):
                v = float(value)
                if not (0.0 <= v <= 1.0):
                    errors.append(f"{slot_name}={v} 超出 [0,1] 范围")
                    continue
                # Layer 2: SOC must tighten (not decrease below current)
                if v < current_min_soc:
                    errors.append(f"{slot_name}={v} 低于当前最小SOC {current_min_soc}，只允许收紧")
                    continue
                cleaned[slot_name] = SlotInfo(value=v, confidence=confidence, reasoning=reasoning)

            elif slot_name == "max_discharge_power_kw":
                v = float(value)
                if v <= 0:
                    errors.append(f"{slot_name}={v} 必须 > 0")
                    continue
                # Layer 2: discharge must tighten (not increase above current)
                if v > current_max_discharge_kw:
                    errors.append(f"{slot_name}={v} 超过当前上限 {current_max_discharge_kw}，只允许收紧")
                    continue
                cleaned[slot_name] = SlotInfo(value=v, confidence=confidence, reasoning=reasoning)

            elif slot_name == "blocked_intervals":
                intervals: list[TimeInterval] = []
                for iv in value:
                    ti = TimeInterval(
                        start_index=int(iv["start_index"]),
                        end_index=int(iv["end_index"]),
                    )
                    intervals.append(ti)
                cleaned[slot_name] = SlotInfo(
                    value=intervals, confidence=confidence, reasoning=reasoning
                )

            elif slot_name == "objective":
                cleaned[slot_name] = SlotInfo(
                    value=str(value), confidence=confidence, reasoning=reasoning
                )
            elif slot_name == "max_cycles_per_day":
                v = int(value)
                if not (1 <= v <= 8):
                    errors.append(f"{slot_name}={v} 超出 [1,8] 范围")
                    continue
                cleaned[slot_name] = SlotInfo(
                    value=v, confidence=confidence, reasoning=reasoning
                )
            else:
                cleaned[slot_name] = SlotInfo(
                    value=value, confidence=confidence, reasoning=reasoning
                )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{slot_name} 校验失败: {exc}")
            continue

        # Layer 3: confidence check
        if confidence < confidence_threshold:
            low_conf.append(slot_name)

    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        low_confidence_slots=low_conf,
        cleaned_slots=cleaned,
    )


def build_revision(result: ValidationResult) -> DispatchRevision:
    """Construct a DispatchRevision from validated slots (non-null values only)."""
    kwargs: dict[str, Any] = {}
    for name, info in result.cleaned_slots.items():
        if info.value is None:
            continue
        if name == "blocked_intervals":
            kwargs[name] = list(info.value)
        else:
            kwargs[name] = info.value
    return DispatchRevision(**kwargs)
