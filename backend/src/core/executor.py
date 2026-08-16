"""Fail-closed simulated physical executor with command, ACK and feedback."""
from __future__ import annotations

import random
import uuid
import math
from datetime import datetime

from core.config import HVAC_DEFAULTS, STORAGE_DEFAULTS
from core.state import DispatchCommand, DispatchExecution, DispatchFeedback, ExecutionAck


class ExecutionRejected(ValueError):
    pass


class SimulationExecutor:
    """Deterministic simulator standing in for future BMS/BAS/PLC adapters."""

    def __init__(self, seed: int = 20260802, deviation_tolerance_ratio: float = 0.03):
        self._rng = random.Random(seed)
        self.deviation_tolerance_ratio = deviation_tolerance_ratio

    async def execute(
        self,
        *,
        run_id: str,
        step: int,
        sim_time: datetime,
        storage_power_kw: float,
        hvac_power_kw: float,
        hvac_supply_temp_c: float,
        hvac_return_temp_c: float,
        previous_storage_power_kw: float,
        previous_storage_soc: float,
        previous_storage_temp_c: float,
        ambient_temp_c: float,
        storage_report_id: str,
        storage_report_hash: str,
        hvac_report_id: str,
        hvac_report_hash: str,
        manual_override: dict | None = None,
    ) -> DispatchExecution:
        max_charge = float(STORAGE_DEFAULTS["max_charge_power_kw"])
        max_discharge = float(STORAGE_DEFAULTS["max_discharge_power_kw"])
        if not -max_charge <= storage_power_kw <= max_discharge:
            raise ExecutionRejected("储能功率越过执行器安全边界")
        max_ramp = float(STORAGE_DEFAULTS["max_ramp_kw_per_step"])
        if abs(storage_power_kw - previous_storage_power_kw) > max_ramp + 1e-6:
            raise ExecutionRejected(f"储能功率变化超过单步爬坡边界 {max_ramp:g} kW")
        hvac_power_limit = float(
            HVAC_DEFAULTS.get(
                "total_rated_power_kw",
                HVAC_DEFAULTS["total_rated_cooling_kw"] / HVAC_DEFAULTS["cop_min"],
            )
        )
        if not 0 <= hvac_power_kw <= hvac_power_limit:
            raise ExecutionRejected("HVAC 功率越过执行器安全边界")
        if not 5.0 <= hvac_supply_temp_c <= 12.0:
            raise ExecutionRejected("HVAC 供水温度越过执行器安全边界")
        if not all((storage_report_id, storage_report_hash, hvac_report_id, hvac_report_hash)):
            raise ExecutionRejected("物理命令缺少已审批报告绑定")

        command_id = f"cmd-{uuid.uuid4().hex[:12]}"
        now = datetime.now().astimezone()
        command = DispatchCommand(
            command_id=command_id,
            run_id=run_id,
            step=step,
            sim_time=sim_time,
            storage_power_kw=round(storage_power_kw, 3),
            hvac_power_kw=round(hvac_power_kw, 3),
            hvac_supply_temp_c=round(hvac_supply_temp_c, 3),
            storage_report_id=storage_report_id,
            storage_report_hash=storage_report_hash,
            hvac_report_id=hvac_report_id,
            hvac_report_hash=hvac_report_hash,
            created_at=now,
            manual_override=manual_override,
        )
        ack = ExecutionAck(
            command_id=command_id,
            accepted=True,
            status="executed",
            message="模拟执行器已接收并执行命令",
            acknowledged_at=now,
        )

        storage_measured = storage_power_kw * (1 + self._rng.uniform(-0.01, 0.01))
        hvac_measured = hvac_power_kw * (1 + self._rng.uniform(-0.01, 0.01))
        supply_measured = hvac_supply_temp_c + self._rng.uniform(-0.05, 0.05)
        return_measured = max(supply_measured, hvac_return_temp_c + self._rng.uniform(-0.08, 0.08))
        dt_h = 0.25
        capacity = float(STORAGE_DEFAULTS["capacity_kwh"])
        eta_ch = float(STORAGE_DEFAULTS["charge_efficiency_ratio"])
        eta_dis = float(STORAGE_DEFAULTS["discharge_efficiency_ratio"])
        if storage_measured >= 0:
            measured_soc = previous_storage_soc - storage_measured * dt_h / (eta_dis * capacity)
        else:
            measured_soc = previous_storage_soc + (-storage_measured) * dt_h * eta_ch / capacity
        measured_soc = max(float(STORAGE_DEFAULTS["min_soc_ratio"]), min(
            float(STORAGE_DEFAULTS["max_soc_ratio"]), measured_soc
        ))
        tau = float(STORAGE_DEFAULTS["thermal_time_constant_min"])
        alpha = math.exp(-15.0 / tau)
        resistance = float(STORAGE_DEFAULTS["thermal_resistance_c_per_kw"])
        measured_temp = (
            ambient_temp_c
            + (previous_storage_temp_c - ambient_temp_c) * alpha
            + abs(storage_measured) * resistance * (1 - alpha)
        )
        storage_dev = storage_measured - storage_power_kw
        hvac_dev = hvac_measured - hvac_power_kw
        storage_ratio = abs(storage_dev) / max(abs(storage_power_kw), 1.0)
        hvac_ratio = abs(hvac_dev) / max(abs(hvac_power_kw), 1.0)
        feedback = DispatchFeedback(
            command_id=command_id,
            measured_storage_power_kw=round(storage_measured, 3),
            measured_hvac_power_kw=round(hvac_measured, 3),
            measured_storage_soc=round(measured_soc, 6),
            measured_storage_temp_c=round(measured_temp, 3),
            measured_hvac_supply_temp_c=round(supply_measured, 3),
            measured_hvac_return_temp_c=round(return_measured, 3),
            storage_deviation_kw=round(storage_dev, 3),
            hvac_deviation_kw=round(hvac_dev, 3),
            max_deviation_ratio=round(max(storage_ratio, hvac_ratio), 6),
            measured_at=now,
        )
        return DispatchExecution(command=command, ack=ack, feedback=feedback)
