"""Real-time tick engine: processes exactly one 15-minute step.

This module extracts the real-time logic from SimulationEngine._update_realtime_locked()
into a self-contained pipeline (revise_guide G3, section 12.3):

  1. Read real-time data (load, solar, weather, current device state)
  2. Resolve setpoints from approved day-ahead plan + overrides
  3. Physical constraint check + execution via device adapter
  4. ACK + measurement feedback
  5. Deviation check
  6. Metric accumulation
  7. Alert generation (monitoring)

The engine calls await tick_engine.run_tick(tick_input) and receives a TickResult.
All side effects are mediated through the executor seam.
"""
from __future__ import annotations

import random
from typing import Any

from core.config import HVAC_DEFAULTS, STORAGE_DEFAULTS
from core.executor import ExecutionRejected, SimulationExecutor
from workflows.phase import TickInput, TickResult


class RealtimeTickEngine:
    """Processes one real-time tick in a structured pipeline.

    Stateless aside from the random generator (for reproducible noise).
    All persistent state lives in SimulationEngine; this module receives
    a TickInput snapshot and returns a TickResult.
    """

    def __init__(
        self,
        executor: SimulationExecutor | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._executor = executor or SimulationExecutor()
        self._rng = rng or random.Random(42)

    @property
    def executor(self) -> SimulationExecutor:
        return self._executor

    @property
    def deviation_tolerance_ratio(self) -> float:
        return self._executor.deviation_tolerance_ratio

    async def run_tick(self, inp: TickInput) -> TickResult:
        """Execute one tick and return structured results."""
        dd = inp.day_data
        step = inp.step
        ts = dd["timestamps"][step]

        # --- Step 1: Read real-time data ---
        load = dd["load_kw"][step] * (1 + self._rng.gauss(0, 0.01))
        solar = dd["solar_kw"][step]
        previous_storage_power = float(inp.previous_values.get("storage_power_kw", 0.0))
        soc = float(inp.previous_values.get("storage_soc", 0.5))
        storage_temp = float(inp.previous_values.get(
            "storage_temp_c", STORAGE_DEFAULTS["ambient_temperature_c"]
        ))

        baseline_hvac = self._resolve_baseline_hvac(inp, dd, step)
        hvac_power = baseline_hvac * (1 + self._rng.gauss(0, 0.004))
        hvac_supply = float(inp.previous_values.get("hvac_supply_temp_c", 7.0))
        hvac_return = float(inp.previous_values.get("hvac_return_temp_c", 12.0))

        result = TickResult(step=step)

        # --- Steps 2-7: Only if dispatch is active (plans approved) ---
        dispatch_active = (
            inp.storage_plan is not None
            and inp.hvac_plan is not None
            and bool(inp.storage_report_id)
            and bool(inp.hvac_report_id)
        )

        if dispatch_active:
            storage_sp, hvac_sp, hvac_supply_sp, hvac_return_sp = (
                self._resolve_setpoints(inp, step, load, solar, baseline_hvac)
            )
            override_payload = self._build_override_payload(inp)

            # --- Steps 3-4: Constraint check + execution ---
            try:
                execution = await self._executor.execute(
                    run_id=inp.run_id,
                    step=step,
                    sim_time=ts,
                    storage_power_kw=storage_sp,
                    hvac_power_kw=hvac_sp,
                    hvac_supply_temp_c=hvac_supply_sp,
                    hvac_return_temp_c=hvac_return_sp,
                    previous_storage_power_kw=previous_storage_power,
                    previous_storage_soc=soc,
                    previous_storage_temp_c=storage_temp,
                    ambient_temp_c=float(dd["weather"]["temp_c"][step]),
                    storage_report_id=inp.storage_report_id,
                    storage_report_hash=inp.storage_report_hash,
                    hvac_report_id=inp.hvac_report_id,
                    hvac_report_hash=inp.hvac_report_hash,
                    manual_override=override_payload,
                )
            except ExecutionRejected as exc:
                result.alerts.append({
                    "severity": "critical",
                    "source": "executor",
                    "message": str(exc),
                })
            else:
                # --- Steps 5-6: ACK + feedback ---
                result.execution = execution
                result.dispatched = True
                storage_power = execution.feedback.measured_storage_power_kw
                hvac_power = execution.feedback.measured_hvac_power_kw
                soc = execution.feedback.measured_storage_soc
                storage_temp = execution.feedback.measured_storage_temp_c
                hvac_supply = execution.feedback.measured_hvac_supply_temp_c
                hvac_return = execution.feedback.measured_hvac_return_temp_c

                # --- Step 7: Deviation check ---
                if execution.feedback.max_deviation_ratio > self.deviation_tolerance_ratio:
                    result.alerts.append({
                        "severity": "warning",
                        "source": "executor",
                        "message": (
                            f"deviation "
                            f"{execution.feedback.max_deviation_ratio:.1%} "
                            f"exceeds threshold"
                        ),
                    })

        # --- Step 8: Compute metrics ---
        storage_power_out = storage_power if dispatch_active and result.dispatched else 0.0
        hvac_delta = baseline_hvac - hvac_power if dispatch_active and result.dispatched else 0.0
        effective_load = load - hvac_delta
        grid = max(0.0, effective_load - solar - storage_power_out)

        carbon_factors = inp.day_data.get("carbon_c_factors")
        c_factor = carbon_factors[step] if carbon_factors and step < len(carbon_factors) else 0.5
        price = dd["price_cny_per_kwh"][step]

        dt_h = 0.25
        result.metrics_delta = {
            "energy": effective_load * dt_h,
            "cost": grid * price * dt_h,
            "carbon": grid * c_factor * dt_h,
            "peak": effective_load,
        }

        # --- Step 9: Measurements snapshot ---
        result.measurements = {
            "load_kw": round(load, 1),
            "solar_kw": round(solar, 1),
            "grid_kw": round(grid, 1),
            "storage_power_kw": round(storage_power_out, 1),
            "storage_soc": round(soc, 4),
            "storage_temp_c": round(storage_temp, 2),
            "hvac_power_kw": round(hvac_power, 1),
            "hvac_supply_temp_c": round(hvac_supply, 1),
            "hvac_return_temp_c": round(hvac_return, 1),
            "hvac_delta_kw": round(hvac_delta, 1),
            "carbon_factor": round(c_factor, 6),
            "price": round(price, 6),
        }

        return result

    def _resolve_baseline_hvac(
        self, inp: TickInput, dd: dict[str, Any], step: int
    ) -> float:
        if inp.hvac_plan and inp.hvac_plan.get("baseline_power_kw"):
            return inp.hvac_plan["baseline_power_kw"][step]
        return dd["hvac_load_kw"][step] / max(float(HVAC_DEFAULTS["cop_nominal"]), 1.0)

    def _resolve_setpoints(
        self, inp: TickInput, step: int, load: float, solar: float, baseline_hvac: float
    ) -> tuple[float, float, float, float]:
        storage_sp = inp.storage_plan["power_kw"][step]
        hvac_sp = inp.hvac_plan["power_kw"][step]
        hvac_supply_sp = inp.hvac_plan["supply_temp_c"][step]
        hvac_return_sp = inp.hvac_plan["return_temp_c"][step]

        if inp.demand_cap_kw is not None:
            planned_grid = load - solar - storage_sp - (baseline_hvac - hvac_sp)
            storage_sp += max(0.0, planned_grid - inp.demand_cap_kw)
            storage_sp = min(float(STORAGE_DEFAULTS["max_discharge_power_kw"]), storage_sp)

        for override in inp.pending_overrides.values():
            target = override.target.lower()
            if override.system == "storage":
                storage_sp = override.value
            elif "power" in target:
                hvac_sp = override.value
            elif "supply" in target:
                hvac_supply_sp = override.value

        return storage_sp, hvac_sp, hvac_supply_sp, hvac_return_sp

    @staticmethod
    def _build_override_payload(inp: TickInput) -> dict[str, Any] | None:
        pending = list(inp.pending_overrides.values())
        if not pending:
            return None
        return {"actions": [item.model_dump(mode="json") for item in pending]}