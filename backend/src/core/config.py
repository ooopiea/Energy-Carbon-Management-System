"""全局配置：站点参数、时间引擎、Agent 默认值。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # backend/
DATA_DIR = BASE_DIR / "data"
PROCESSED_DIR = DATA_DIR / "processed"
SIMULATED_DIR = DATA_DIR / "simulated"
AGENTS_CONFIG_DIR = BASE_DIR / "agents_config"

# 站点：长沙黄花园区
SITE_ID = "huanghua"
SITE_NAME = "黄花工业园区"
REGION = "cn-hunan"

# 时间引擎：200x 现实速度，15min 模拟粒度
TIME_SCALE = 200
SIM_STEP_MINUTES = 15
POINTS_PER_DAY = 96  # 24h * 4

# 物理约束默认值
STORAGE_DEFAULTS = {
    "capacity_kwh": 2000.0,
    "max_charge_power_kw": 500.0,
    "max_discharge_power_kw": 500.0,
    "min_soc_ratio": 0.10,
    "max_soc_ratio": 0.90,
    "charge_efficiency_ratio": 0.95,
    "discharge_efficiency_ratio": 0.95,
    "max_ramp_kw_per_step": 300.0,
    "max_cell_temperature_c": 45.0,
    "thermal_resistance_c_per_kw": 0.002,
    "thermal_time_constant_min": 30.0,
    "ambient_temperature_c": 25.0,
}

HVAC_DEFAULTS = {
    "chiller_count": 20,
    "total_rated_power_kw": 180000,
    "chilled_water_temp_setpoint_c": 7.0,
    "return_water_temp_max_c": 12.0,
    "cop_nominal": 4.5,
    "cop_min": 3.0,
}

COMPRESSOR_DEFAULTS = {
    "total_rated_power_kw": 25900,
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
    "sharp": 0.81518,   # 尖 (元/kWh)
    "peak": 0.67932,    # 峰
    "flat": 0.42457,    # 平
    "valley": 0.16983,  # 谷
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


def build_period_map() -> list[str]:
    """生成 96 点时段标签序列。"""
    labels = []
    for i in range(POINTS_PER_DAY):
        hour = i * SIM_STEP_MINUTES / 60.0
        labels.append(get_tariff_period(hour))
    return labels


def build_price_series() -> list[float]:
    """生成 96 点电价序列。"""
    period_map = build_period_map()
    return [TARIFF_PRICES[p] for p in period_map]
