"""真实数据模拟器：基于实际负荷曲线和湖南电网发电结构生成 15min 粒度数据。

数据来源校准：
- 负荷曲线：黄花园区四季实际用电负荷参考（85-111 MW 级别）
- 发电结构：湖南电网 2025-06 实时出清数据（火电/水电/风电/光伏）
- 电价：湖南分时电价（尖峰平谷）+ 需量电费 + 政府基金
- 气象：长沙夏季典型日变化曲线
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Any

from core.config import (
    COMPRESSOR_DEFAULTS,
    HVAC_DEFAULTS,
    POINTS_PER_DAY,
    SIMULATION_START_DATE,
    SIM_STEP_MINUTES,
    SITE_SOLAR_CAPACITY_KW,
    TARIFF_PRICES,
)
from data.raw_loader import (
    load_asset_registry,
    load_generation_mix,
    load_load_profile,
    load_tariff_prices,
)


# 实际四季负荷基线（kW，96点），来自黄花园区用电负荷参考
SEASONAL_LOAD_BASELINE = {
    "summer": [
        92329.6, 91801.6, 92611.2, 91203.2, 91238.4, 93878.4, 95568.0, 95145.6,
        95990.4, 97081.6, 96940.8, 97152.0, 97046.4, 95497.6, 95180.8, 96201.6,
        95744.0, 95990.4, 97292.8, 98102.4, 97363.2, 96905.6, 96588.8, 96764.8,
        96588.8, 96940.8, 94969.6, 95462.4, 95040.0, 93913.6, 91942.4, 90745.6,
        92576.0, 95110.4, 97011.2, 96588.8, 98806.4, 96729.6, 95638.4, 93104.0,
        93315.2, 93244.8, 92963.2, 92681.6, 92118.4, 92892.8, 91520.0, 89654.4,
        87014.4, 85606.4, 86838.4, 87120.0, 87225.6, 89267.2, 93808.0, 93491.2,
        94441.6, 101305.6, 99475.2, 94265.6, 96201.6, 97433.6, 99264.0, 99968.0,
        98172.8, 99369.6, 100742.4, 102643.2, 103488.0, 105987.2, 105670.4, 105670.4,
        104121.6, 104121.6, 103276.8, 102995.2, 101763.2, 100214.4, 98419.2, 97750.4,
        96201.6, 97820.8, 99334.4, 98876.8, 99792.0, 98841.6, 98947.2, 98208.0,
        97715.2, 97468.8, 97398.4, 97292.8, 96835.2, 95180.8, 95356.8, 94019.2,
    ],
    "autumn": [
        71913.6, 72265.6, 71280.0, 71913.6, 72265.6, 72652.8, 73708.8, 74905.6,
        73744.0, 74976.0, 74905.6, 75433.6, 75926.4, 75468.8, 76032.0, 74870.4,
        75856.0, 76208.0, 76102.4, 75609.6, 75926.4, 76700.8, 76208.0, 75891.2,
        76454.4, 76102.4, 76208.0, 75046.4, 74976.0, 76278.4, 76560.0, 78320.0,
        81488.0, 85500.8, 87507.2, 88985.6, 89126.4, 89760.0, 90182.4, 90675.2,
        91660.8, 91555.2, 91203.2, 91977.6, 91414.4, 90886.4, 90358.4, 89091.2,
        86169.6, 85148.8, 85606.4, 84550.4, 83776.0, 86240.0, 87366.4, 89443.2,
        91625.6, 93315.2, 94054.4, 92752.0, 92611.2, 92224.0, 92540.8, 92188.8,
        93420.8, 92400.0, 93772.8, 93385.6, 93702.4, 94054.4, 92963.2, 91344.0,
        92153.6, 91238.4, 91379.2, 90816.0, 88563.2, 88176.0, 87366.4, 85254.4,
        83740.8, 87331.2, 88281.6, 87929.6, 88316.8, 89302.4, 88774.4, 88809.6,
        89302.4, 88598.4, 88563.2, 88387.2, 87718.4, 87964.8, 86380.8, 85219.2,
    ],
    "winter": [
        91238.4, 91097.6, 91203.2, 90534.4, 91097.6, 91555.2, 93456.0, 94441.6,
        95040.0, 95286.4, 95990.4, 95814.4, 95110.4, 95884.8, 96483.2, 94934.4,
        96694.4, 95955.2, 94688.0, 96131.2, 95321.6, 95110.4, 96272.0, 94371.2,
        94512.0, 94089.6, 94723.2, 93209.6, 91625.6, 90288.0, 92048.0, 91555.2,
        96236.8, 100284.8, 101411.2, 103241.6, 102784.0, 103312.0, 102995.2, 105072.0,
        105036.8, 103804.8, 104966.4, 105107.2, 102256.0, 101376.0, 101200.0, 97961.6,
        96131.2, 95075.2, 95216.0, 94758.4, 96412.8, 98560.0, 100355.2, 101446.4,
        103875.2, 105283.2, 104755.2, 105459.2, 105388.8, 105529.6, 105177.6, 105740.8,
        105318.4, 105881.6, 105353.6, 105177.6, 106374.4, 105740.8, 103382.4, 103382.4,
        102185.6, 99932.8, 99580.8, 99334.4, 98032.0, 94793.6, 93772.8, 93737.6,
        94160.0, 96166.4, 96624.0, 95920.0, 96659.2, 97011.2, 95920.0, 95814.4,
        95744.0, 96025.6, 95321.6, 95321.6, 94934.4, 94547.2, 92998.4, 92470.4,
    ],
    "spring": [
        102115.2, 102960.0, 103488.0, 103276.8, 102115.2, 104438.4, 106198.4, 106339.2,
        108380.8, 108873.6, 109155.2, 108944.0, 107747.2, 107923.2, 108275.2, 106972.8,
        107324.8, 108134.4, 107571.2, 107888.0, 106444.8, 106304.0, 107008.0, 106867.2,
        106128.0, 105600.0, 104473.6, 104720.0, 103136.0, 103769.6, 102819.2, 101516.8,
        102819.2, 106726.4, 110739.2, 110000.0, 109190.4, 108345.6, 107747.2, 108697.6,
        107465.6, 106339.2, 110352.0, 111267.2, 110281.6, 109084.8, 103699.2, 103593.6,
        102150.4, 103734.4, 104016.0, 104086.4, 104086.4, 104860.8, 106515.2, 106726.4,
        109436.8, 109648.0, 109507.2, 109190.4, 110316.8, 107324.8, 110387.2, 109648.0,
        110000.0, 110950.4, 110352.0, 111091.2, 111091.2, 109648.0, 109683.2, 110176.0,
        108838.4, 109155.2, 108838.4, 107747.2, 105881.6, 103382.4, 102326.4, 102678.4,
        101164.8, 105952.0, 107113.6, 107113.6, 106057.6, 106726.4, 107465.6, 106585.6,
        105705.6, 105107.2, 105283.2, 105388.8, 104684.8, 103664.0, 102854.4, 102643.2,
    ],
}


def _get_season(month: int) -> str:
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    if month in (12, 1, 2):
        return "winter"
    return "spring"


class DataSimulator:
    """生成一个完整模拟日的所有时间序列数据。"""

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)

    def generate_day(self, day_index: int = 0, month: int = 7) -> dict[str, Any]:
        """生成一天完整的 96 点数据。"""
        season = _get_season(month)
        anchored_date = SIMULATION_START_DATE + timedelta(days=day_index)
        # engine 传入的是全局 day_index；跨月时必须以统一锚点推进，避免二次加日。
        if anchored_date.month == month:
            sim_date = datetime.combine(anchored_date, datetime.min.time())
        else:
            sim_date = datetime(2025, month, 15)
        timestamps = [
            sim_date.replace(hour=i // 4, minute=(i % 4) * 15, second=0, microsecond=0)
            for i in range(POINTS_PER_DAY)
        ]

        # --- 负荷 ---
        load, load_source = load_load_profile(sim_date.date())
        if load is None:
            load = self._gen_load(season, day_index)
            load_source = {
                **load_source,
                "source": "embedded_seasonal_profile_fallback",
                "loaded": True,
                "quality": "simulated",
            }

        # --- 气象 ---
        weather = self._gen_weather(month, day_index)

        # --- 光伏发电（厂区屋顶光伏）---
        solar = self._gen_solar(weather["solar_irradiance_wm2"], weather["cloud_cover"])

        # --- 发电结构（湖南电网）---
        gen_mix, generation_source = load_generation_mix(sim_date.date())
        if gen_mix is None:
            gen_mix = self._gen_generation_mix(day_index)
            generation_source = {
                **generation_source,
                "source": "calibrated_generation_fallback",
                "loaded": True,
                "quality": "simulated",
            }

        # --- 电价 ---
        from core.config import build_price_series, build_period_map
        tariff_rates, tariff_source = load_tariff_prices(month)
        if tariff_rates is None:
            tariff_rates = TARIFF_PRICES
            tariff_source = {
                **tariff_source,
                "source": "2026-01_tariff_proxy_fallback",
                "loaded": True,
                "quality": "proxy",
            }
        price = build_price_series(month, tariff_rates)
        periods = build_period_map(month)

        # --- 生产排班 ---
        schedule = self._gen_schedule()

        # --- HVAC 负荷 ---
        hvac_load = self._gen_hvac_load(weather["temp_c"], load, schedule)

        # --- 空压机负荷 ---
        compressor_load = self._gen_compressor_load(load, schedule)
        from algorithms.compressor import assess_compressor_flexibility
        compressor_capability = assess_compressor_flexibility(compressor_load)

        assets, asset_source = load_asset_registry()
        if not assets:
            from algorithms.hvac import CURRENT_RUNNING_CHILLERS, TOTAL_CHILLERS, TOTAL_RATED_KW, get_chiller_topology
            assets = {
                "chillers": get_chiller_topology(),
                "compressors": [],
                "summary": {
                    "chiller_units": TOTAL_CHILLERS,
                    "running_chiller_units": CURRENT_RUNNING_CHILLERS,
                    "installed_cooling_kw": TOTAL_RATED_KW,
                    "compressor_units": COMPRESSOR_DEFAULTS["unit_count"],
                    "installed_compressor_power_kw": COMPRESSOR_DEFAULTS["total_rated_power_kw"],
                },
            }
            asset_source = {
                **asset_source,
                "source": "embedded_asset_registry_fallback",
                "loaded": True,
                "quality": "proxy",
            }

        return {
            "timestamps": timestamps,
            "load_kw": load,
            "weather": weather,
            "solar_kw": solar,
            "generation_mix": gen_mix,
            "price_cny_per_kwh": price,
            "tariff_periods": periods,
            "schedule": schedule,
            "hvac_load_kw": hvac_load,
            "compressor_load_kw": compressor_load,
            "compressor_capability": {
                **compressor_capability,
                "baseline_energy_kwh": round(sum(compressor_load) * 0.25, 1),
                "reason": "缺少压缩空气压力、流量与储气罐状态，禁止虚构移峰调度",
            },
            "asset_registry": assets,
            "data_provenance": {
                "load": load_source,
                "weather": {"source": "changsha_typical_day_model", "loaded": True, "quality": "simulated"},
                "site_solar": {
                    "source": "project_reference_docx_capacity_plus_weather_model",
                    "loaded": True,
                    "quality": "simulated",
                    "capacity_kw": SITE_SOLAR_CAPACITY_KW,
                },
                "generation_mix": generation_source,
                "tariff": tariff_source,
                "assets": asset_source,
            },
            "season": season,
            "month": month,
            "day_index": day_index,
        }

    def _gen_load(self, season: str, day_index: int) -> list[float]:
        """基于实际负荷曲线 + 随机波动。"""
        baseline = SEASONAL_LOAD_BASELINE[season]
        # 日间随机波动 ±5%，加入天气相关性
        noise_amp = 0.03 + 0.02 * self._rng.random()
        result = []
        for i, base in enumerate(baseline):
            # 低频正弦波模拟缓慢漂移
            drift = 0.02 * math.sin(2 * math.pi * i / 96 + day_index)
            # 高频随机噪声
            noise = noise_amp * (self._rng.random() - 0.5) * 2
            # 工作日/周末效应（假设 day_index 为工作日）
            result.append(base * (1 + drift + noise))
        return result

    def _gen_weather(self, month: int, day_index: int) -> dict[str, list[float]]:
        """长沙典型日气象曲线。"""
        temps = []
        humidities = []
        wind_speeds = []
        solar_irr = []
        cloud_covers = []

        # 夏季基线温度
        if month in (6, 7, 8):
            t_min, t_max = 26, 36
            h_min, h_max = 50, 85
            base_cloud = 0.3
        elif month in (12, 1, 2):
            t_min, t_max = 3, 12
            h_min, h_max = 55, 80
            base_cloud = 0.5
        else:
            t_min, t_max = 15, 28
            h_min, h_max = 50, 80
            base_cloud = 0.4

        for i in range(POINTS_PER_DAY):
            hour = i / 4.0
            # 温度：余弦曲线，最低在凌晨5-6点，最高在下午14-15点
            t_phase = (hour - 5) / 24 * 2 * math.pi
            temp = (t_min + t_max) / 2 + (t_max - t_min) / 2 * math.sin(t_phase)
            temp += self._rng.gauss(0, 0.5)
            temps.append(round(temp, 1))

            # 湿度：与温度反相
            humidity = (h_min + h_max) / 2 - (h_max - h_min) / 2 * math.sin(t_phase)
            humidity += self._rng.gauss(0, 3)
            humidities.append(max(20, min(100, round(humidity, 1))))

            # 风速
            wind = 2 + 3 * math.sin((hour - 8) / 24 * 2 * math.pi) + self._rng.gauss(0, 1)
            wind_speeds.append(max(0, round(wind, 1)))

            # 太阳辐照度
            if 6 <= hour <= 18:
                solar_angle = math.sin((hour - 6) / 12 * math.pi)
                cloud = max(0, min(1, base_cloud + self._rng.gauss(0, 0.15)))
                irradiance = 900 * solar_angle * (1 - cloud * 0.7)
                irradiance = max(0, irradiance + self._rng.gauss(0, 30))
            else:
                irradiance = 0
                cloud = base_cloud + self._rng.gauss(0, 0.1)
            solar_irr.append(round(max(0, irradiance), 1))
            cloud_covers.append(round(max(0, min(1, cloud)), 2))

        return {
            "temp_c": temps,
            "humidity_pct": humidities,
            "wind_speed_ms": wind_speeds,
            "solar_irradiance_wm2": solar_irr,
            "cloud_cover": cloud_covers,
        }

    def _gen_solar(self, irradiance: list[float], cloud: list[float]) -> list[float]:
        """厂区屋顶光伏：按参考文件确认的 19.1 MW 装机容量建模。"""
        capacity_mw = SITE_SOLAR_CAPACITY_KW / 1000.0
        efficiency = 0.18
        panel_area_m2 = capacity_mw * 1e6 / (1000 * efficiency)
        result = []
        for irr in irradiance:
            power_kw = irr * panel_area_m2 * efficiency / 1000  # kW
            result.append(round(max(0, power_kw), 1))
        return result

    def _gen_generation_mix(self, day_index: int) -> list[dict[str, float]]:
        """湖南电网发电结构（MW -> 比例化）。"""
        # 基于实际数据：火电~2160, 水电~4428, 风电~594, 光伏~150 (均值MW)
        result = []
        for i in range(POINTS_PER_DAY):
            hour = i / 4.0
            # 火电：相对稳定，傍晚高峰略增
            coal = 2150 + 300 * math.sin((hour - 6) / 24 * 2 * math.pi) + self._rng.gauss(0, 100)
            # 水电/抽蓄：早晚高峰发电，中午抽蓄
            if 8 <= hour <= 11 or 17 <= hour <= 21:
                hydro = 5000 + 2000 * math.sin((hour - 8) / 12 * math.pi) + self._rng.gauss(0, 200)
            else:
                hydro = 3500 + self._rng.gauss(0, 300)
            # 风电：夜间和凌晨较强
            wind = 400 + 400 * math.sin((hour + 6) / 24 * 2 * math.pi) + self._rng.gauss(0, 100)
            wind = max(0, wind)
            # 光伏：白天有
            if 6 <= hour <= 18:
                solar = 1000 * math.sin((hour - 6) / 12 * math.pi)
                solar += self._rng.gauss(0, 50)
            else:
                solar = 0
            solar = max(0, solar)

            # 原始数据中上述四类平均仅覆盖总出力约 37%，其余出力显式归入 other。
            other = max(0.0, 12_500 + self._rng.gauss(0, 800))
            total = coal + hydro + wind + solar + other
            if total <= 0:
                total = 1

            result.append({
                "coal": max(0, round(coal, 1)),
                "hydro": round(hydro, 1),
                "wind": max(0, round(wind, 1)),
                "solar": round(solar, 1),
                "other": round(other, 1),
                "total": round(total, 1),
            })
        return result

    def _gen_schedule(self) -> dict[str, Any]:
        """生产排班：三班制（白班/晚班/夜班）。"""
        shifts = []
        for i in range(POINTS_PER_DAY):
            hour = i / 4.0
            if 8 <= hour < 17:
                shift = "day"      # 白班
                intensity = 1.0
            elif 17 <= hour < 24:
                shift = "evening"  # 晚班
                intensity = 0.85
            else:
                shift = "night"    # 夜班
                intensity = 0.65
            shifts.append({"step": i, "shift": shift, "intensity": round(intensity, 2)})
        return {"shifts": shifts}

    def _gen_hvac_load(self, temps: list[float], load: list[float], schedule: dict) -> list[float]:
        """分解 HVAC 制冷负荷（热负荷），日均电力占比按参考文件校准。"""
        auxiliary_share = 0.73 / (1.0 + 0.73)
        hvac_electric_share = auxiliary_share * 0.44
        weights = []
        for i, temp in enumerate(temps):
            intensity = schedule["shifts"][i]["intensity"]
            weights.append(max(0.2, 0.65 + 0.05 * max(0.0, temp - 26.0) + 0.25 * intensity))
        target_electric_energy = sum(load) * hvac_electric_share
        scale = target_electric_energy / max(sum(load[i] * weights[i] for i in range(len(load))), 1.0)
        return [
            round(load[i] * weights[i] * scale * HVAC_DEFAULTS["cop_nominal"], 1)
            for i in range(len(load))
        ]

    def _gen_compressor_load(self, load: list[float], schedule: dict) -> list[float]:
        """空压机电负荷：按“辅助负荷的 40%”校准，并受台账装机上限约束。"""
        auxiliary_share = 0.73 / (1.0 + 0.73)
        compressor_share = auxiliary_share * 0.40
        weights = [0.7 + 0.3 * item["intensity"] for item in schedule["shifts"]]
        target_energy = sum(load) * compressor_share
        scale = target_energy / max(sum(load[i] * weights[i] for i in range(len(load))), 1.0)
        capacity = COMPRESSOR_DEFAULTS["total_rated_power_kw"]
        return [round(min(capacity, load[i] * weights[i] * scale), 1) for i in range(len(load))]


# 全局模拟器实例
_sim: DataSimulator | None = None


def get_simulator() -> DataSimulator:
    global _sim
    if _sim is None:
        _sim = DataSimulator(seed=42)
    return _sim


def generate_day_ahead_data(day_index: int = 0, month: int = 7) -> dict[str, Any]:
    """便捷入口：生成日前数据。"""
    return get_simulator().generate_day(day_index, month)
