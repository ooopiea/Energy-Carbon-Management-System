"""空压机运行能力评估。

原始资料只有设备台账，没有压力、流量和储气罐状态。缺少这些状态时，系统只能
计算可调潜力并保持监测模式，不能把移峰策略伪装成可下发指令。
"""
from __future__ import annotations

from typing import Any

from core.config import COMPRESSOR_DEFAULTS


def assess_compressor_flexibility(
    load_kw: list[float],
    pressure_bar: list[float] | None = None,
    minimum_pressure_bar: float = 6.5,
) -> dict[str, Any]:
    if not load_kw:
        raise ValueError("load_kw must not be empty")
    if pressure_bar is not None and len(pressure_bar) != len(load_kw):
        raise ValueError("pressure_bar length must match load_kw")

    capacity = float(COMPRESSOR_DEFAULTS["total_rated_power_kw"])
    utilization = [max(0.0, float(value)) / capacity for value in load_kw]
    violations = [
        {"step": step, "code": "COMPRESSOR_CAPACITY_EXCEEDED", "value": round(float(value), 1)}
        for step, value in enumerate(load_kw)
        if float(value) > capacity
    ]
    if pressure_bar is not None:
        violations.extend(
            {"step": step, "code": "AIR_PRESSURE_BELOW_MIN", "value": round(float(value), 2)}
            for step, value in enumerate(pressure_bar)
            if float(value) < minimum_pressure_bar
        )

    dispatch_ready = pressure_bar is not None and not any(
        item["code"] == "AIR_PRESSURE_BELOW_MIN" for item in violations
    )
    return {
        "installed_power_kw": capacity,
        "unit_count": COMPRESSOR_DEFAULTS["unit_count"],
        "load_kw": [round(float(value), 1) for value in load_kw],
        "utilization_ratio": [round(value, 4) for value in utilization],
        "peak_load_kw": round(max(load_kw), 1),
        "average_load_kw": round(sum(load_kw) / len(load_kw), 1),
        "dispatch_ready": dispatch_ready,
        "dispatch_mode": "advisory" if dispatch_ready else "monitor_only",
        "violations": violations,
        "missing_inputs": [] if pressure_bar is not None else ["pressure_bar", "flow_m3_min", "receiver_state"],
    }
