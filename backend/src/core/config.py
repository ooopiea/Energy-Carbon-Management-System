"""全局配置：站点参数、时间引擎、Agent 默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # backend/
DATA_DIR = BASE_DIR / "data"
PROCESSED_DIR = DATA_DIR / "processed"
SIMULATED_DIR = DATA_DIR / "simulated"
AGENTS_CONFIG_DIR = BASE_DIR / "agents_config"
BUNDLED_RAW_DIR = DATA_DIR / "raw"
LEGACY_RAW_DIR = BASE_DIR.parent.parent / "data_raw"
DATA_RAW_DIR = Path(
    os.getenv(
        "ENERGY_DATA_RAW_DIR",
        str(BUNDLED_RAW_DIR if BUNDLED_RAW_DIR.exists() else LEGACY_RAW_DIR),
    )
)

# 站点：长沙黄花园区
SITE_ID = "huanghua"
SITE_NAME = "黄花工业园区"
REGION = "cn-hunan"
# Simulator compatibility anchor. The runtime clock itself is resolved from the
# earliest date in the load ledger by SimulationEngine.
SIMULATION_START_DATE = date.fromisoformat(
    os.getenv("ENERGY_SIMULATION_START_DATE", "2025-07-15")
)

# 项目参考文件确认的站点资产容量。单位口径必须在全链路保持一致。
SITE_SOLAR_CAPACITY_KW = 19_100.0
SITE_STORAGE_POWER_KW = 15_000.0
SITE_STORAGE_CAPACITY_KWH = 30_000.0

# 时间引擎：200x 现实速度，15min 模拟粒度
TIME_SCALE = 200
SIM_STEP_MINUTES = 15
POINTS_PER_DAY = 96  # 24h * 4

# 物理约束默认值
STORAGE_DEFAULTS = {
    "capacity_kwh": SITE_STORAGE_CAPACITY_KWH,
    "max_charge_power_kw": SITE_STORAGE_POWER_KW,
    "max_discharge_power_kw": SITE_STORAGE_POWER_KW,
    "min_soc_ratio": 0.10,
    "max_soc_ratio": 0.90,
    "charge_efficiency_ratio": 0.90,
    "discharge_efficiency_ratio": 0.90,
    # 轻量正则项：抑制同价值方案中的无意义充/放模式反复切换。
    "mode_switch_penalty_cny": 10.0,
    "max_ramp_kw_per_step": SITE_STORAGE_POWER_KW,
    "max_cell_temperature_c": 45.0,
    # 工程回退值：额定功率时稳态温升约 8°C；上线前应由 BMS 实测标定。
    "thermal_resistance_c_per_kw": 8.0 / SITE_STORAGE_POWER_KW,
    "thermal_time_constant_min": 30.0,
    "ambient_temperature_c": 25.0,
    "max_equivalent_full_cycles": 1.5,
}

HVAC_DEFAULTS = {
    "chiller_count": 37,
    # 原始台账数值外形对应制冷量而非电输入功率，暂按额定制冷量使用。
    "total_rated_cooling_kw": 256708.0,
    "chilled_water_temp_setpoint_c": 7.0,
    "return_water_temp_max_c": 12.0,
    "cop_nominal": 4.5,
    "cop_min": 3.0,
}

COMPRESSOR_DEFAULTS = {
    "unit_count": 38,
    "total_rated_power_kw": 27600,
    "min_load_ratio": 0.3,
    "max_load_ratio": 1.0,
}

# 排放因子库 (kgCO2/kWh)
EMISSION_FACTORS = {
    "coal": 0.85,
    "gas": 0.40,
    "oil": 0.75,
    "hydro": 0.0,
    "wind": 0.0,
    "solar": 0.0,
    "nuclear": 0.0,
    "biomass": 0.0,
    "other_renewable": 0.0,
    "purchase": 0.5366,
}

# 电价时段映射 (15min 粒度, 96点)
# 低谷: 00:00-06:00, 12:00-14:00
# 平段: 06:00-12:00, 14:00-16:00
# 高峰: 16:00-24:00
# 尖峰(夏季7-8月): 20:00-24:00

TARIFF_PRICES = {
    # 2026-01 黄花底稿“合计单价”，用于找不到目标月份账单时的代理值。
    # 政府性基金与基本电费仍在 tariff.py 中单列核算。
    "sharp": 1.11022,   # 尖 (元/kWh)
    "peak": 0.93098,    # 峰
    "flat": 0.59490,    # 平
    "valley": 0.25881,  # 谷
}

DEMAND_PRICE_CNY_PER_KW_MONTH = 30.6
GOV_FUND_RATE = 0.04625
REACTIVE_ADJUST_RATIO = -0.0075


def get_tariff_period(hour: float, month: int = 7) -> str:
    """根据小时和月份返回电价时段标签。"""
    # 低谷: 00:00-06:00, 12:00-14:00
    if (0 <= hour < 6) or (12 <= hour < 14):
        return "valley"
    # 平段: 06:00-12:00, 14:00-16:00
    if (6 <= hour < 12) or (14 <= hour < 16):
        return "flat"
    # 高峰: 16:00-24:00
    # 尖峰(夏季7-8月): 20:00-24:00; 冬季(1,12月): 18:00-22:00
    is_summer = month in (7, 8)
    is_winter = month in (1, 12)
    if is_summer and 20 <= hour < 24:
        return "sharp"
    if is_winter and 18 <= hour < 22:
        return "sharp"
    return "peak"


def build_period_map(month: int = 7) -> list[str]:
    """生成 96 点时段标签序列。"""
    labels = []
    for i in range(POINTS_PER_DAY):
        hour = i * SIM_STEP_MINUTES / 60.0
        labels.append(get_tariff_period(hour, month))
    return labels


def build_price_series(
    month: int = 7,
    prices: dict[str, float] | None = None,
) -> list[float]:
    """生成 96 点电价序列。"""
    period_map = build_period_map(month)
    rate_map = prices or TARIFF_PRICES
    return [float(rate_map[p]) for p in period_map]
