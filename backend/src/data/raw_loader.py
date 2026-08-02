"""黄花园区原始数据读取器。

该模块只做确定性的格式转换，不对缺失数据伪装成实测值。每个读取函数均返回
``(data, provenance)``；无法读取时返回 ``None`` 和带错误原因的溯源信息，由上层
模拟器明确启用工程回退数据。
"""
from __future__ import annotations

import csv
import random
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.config import DATA_RAW_DIR


TIME_LABELS = [f"{minute // 60:02d}:{minute % 60:02d}" for minute in range(0, 1440, 15)]


@lru_cache(maxsize=4)
def get_load_data_range(raw_dir: Path = DATA_RAW_DIR) -> tuple[date | None, date | None, dict[str, Any]]:
    """Return coverage of the hourly actual-load rows used by the runtime."""
    path = raw_dir / "daily_load_real" / "total_substation" / "用电负荷_1h.xlsx"
    if not path.exists():
        return None, None, _provenance("raw_hourly_load", path, reason="file_missing")
    try:
        records = _load_hourly_records(path)
        if not records:
            return None, None, _provenance("raw_hourly_load", path, reason="no_valid_hourly_actual_rows")
        dates = sorted(records)
        first, last = dates[0], dates[-1]
        return first, last, _provenance(
            "raw_hourly_load",
            path,
            loaded=True,
            first_date=first.isoformat(),
            last_date=last.isoformat(),
            available_days=len(dates),
            source_resolution_minutes=60,
            target_resolution_minutes=15,
        )
    except Exception as exc:
        return None, None, _provenance("raw_hourly_load", path, reason=f"{type(exc).__name__}: {exc}")


@lru_cache(maxsize=4)
def _load_hourly_records(path: Path) -> dict[date, list[float]]:
    """Read 24-point rows marked ``实际`` from the 1h workbook.

    The first six legacy worksheets contain two-hour samples and are deliberately
    ignored: using them would silently violate the requested one-hour contract.
    Hourly values in the ledger are MW averages (the workbook labels them MWh for
    each one-hour interval), so the runtime converts them to kW.
    """
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    records: dict[date, list[float]] = {}
    try:
        for sheet in workbook.worksheets:
            header_row: int | None = None
            for row_index, row in enumerate(
                sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 12), max_col=28, values_only=True),
                start=1,
            ):
                labels = [str(value).strip() if value is not None else "" for value in row]
                if "标的日期" in labels and "用户名称" in labels and "1:00" in labels and "24:00" in labels:
                    header_row = row_index
                    break
            if header_row is None:
                continue
            current_date: date | None = None
            for row in sheet.iter_rows(min_row=header_row + 1, max_col=28, values_only=True):
                parsed = _parse_excel_date(row[0])
                if parsed is not None:
                    current_date = parsed
                label = str(row[2]).strip() if row[2] is not None else ""
                if label != "实际" or current_date is None:
                    continue
                raw_values = row[3:27]
                if len(raw_values) != 24 or any(value is None for value in raw_values):
                    continue
                values_kw = [max(0.0, float(value) * 1000.0) for value in raw_values]
                if all(value > 0 for value in values_kw):
                    records[current_date] = values_kw
    finally:
        workbook.close()
    return records


def process_load_to_15min(
    values: list[float],
    source_resolution_minutes: int,
) -> tuple[list[float], dict[str, Any]]:
    """Validate or energy-preservingly disaggregate interval-average load to 15 minutes.

    This is deliberately a load-processing operation, not a forecast.  For coarse
    inputs the average of every source interval is preserved after disaggregation.
    """
    if source_resolution_minutes < 15 or source_resolution_minutes % 15:
        raise ValueError("source resolution must be a positive multiple of 15 minutes")
    expected = 1440 // source_resolution_minutes
    if len(values) != expected:
        raise ValueError(f"expected {expected} points for {source_resolution_minutes}-minute data")
    cleaned = [max(0.0, float(value)) for value in values]
    if source_resolution_minutes == 15:
        return cleaned, {
            "method": "validated_15min_passthrough",
            "input_points": 96,
            "output_points": 96,
            "source_resolution_minutes": 15,
            "target_resolution_minutes": 15,
            "energy_preserved": True,
        }

    quarters_per_source = source_resolution_minutes // 15
    offsets = [((index + 0.5) / quarters_per_source) - 0.5 for index in range(quarters_per_source)]
    output: list[float] = []
    for index, mean in enumerate(cleaned):
        previous = cleaned[index - 1] if index else mean
        following = cleaned[index + 1] if index + 1 < len(cleaned) else mean
        slope = (following - previous) / 2.0
        block = [max(0.0, mean + slope * offset) for offset in offsets]
        block_mean = sum(block) / quarters_per_source
        if block_mean > 0:
            block = [value * mean / block_mean for value in block]
        output.extend(block)
    return output, {
        "method": "shape_preserving_interval_disaggregation",
        "input_points": len(cleaned),
        "output_points": len(output),
        "source_resolution_minutes": source_resolution_minutes,
        "target_resolution_minutes": 15,
        "energy_preserved": True,
    }


def _provenance(source: str, path: Path, **extra: Any) -> dict[str, Any]:
    return {
        "source": source,
        "path": str(path),
        "loaded": bool(extra.pop("loaded", False)),
        **extra,
    }


def load_load_profile(
    target_date: date,
    raw_dir: Path = DATA_RAW_DIR,
) -> tuple[list[float] | None, dict[str, Any]]:
    """Read an hourly actual profile and deterministically disaggregate to 15 min."""
    path = raw_dir / "daily_load_real" / "total_substation" / "用电负荷_1h.xlsx"
    if not path.exists():
        return None, _provenance("raw_hourly_load", path, reason="file_missing")
    try:
        records = _load_hourly_records(path)
        if not records:
            return None, _provenance("raw_hourly_load", path, reason="no_valid_hourly_actual_rows")
        same_month = [(item_date, values) for item_date, values in records.items()
                      if item_date.month == target_date.month]
        candidates = same_month or list(records.items())
        selected_date, hourly_kw = min(candidates, key=lambda item: (
            abs((item[0] - target_date).days), item[0]
        ))
        processed, processing = process_load_to_15min(hourly_kw, 60)
        disturbed, disturbance = _add_deterministic_quarter_hour_disturbance(
            processed, hourly_kw, target_date
        )
        return disturbed, _provenance(
            "raw_hourly_load",
            path,
            loaded=True,
            selected_date=selected_date.isoformat(),
            exact_date=selected_date == target_date,
            source_points=24,
            points=96,
            unit="kW",
            source_unit="MW average (equivalent to MWh per one-hour interval)",
            source_resolution_minutes=60,
            resolution_minutes=15,
            hourly_source_kw=[round(value, 6) for value in hourly_kw],
            processing=processing,
            perturbation=disturbance,
        )
    except Exception as exc:  # 数据源损坏时由上层显式降级
        return None, _provenance("raw_hourly_load", path, reason=f"{type(exc).__name__}: {exc}")


def _add_deterministic_quarter_hour_disturbance(
    values_15min: list[float],
    hourly_kw: list[float],
    target_date: date,
    max_ratio: float = 0.015,
) -> tuple[list[float], dict[str, Any]]:
    """Add a reproducible intra-hour disturbance while preserving hourly energy."""
    rng = random.Random(int(target_date.strftime("%Y%m%d")) + 415)
    output: list[float] = []
    observed_max = 0.0
    for hour, mean_kw in enumerate(hourly_kw):
        block = list(values_15min[hour * 4:hour * 4 + 4])
        raw = [rng.uniform(-max_ratio, max_ratio) for _ in range(4)]
        centered = [value - sum(raw) / 4 for value in raw]
        disturbed = [max(0.0, value + mean_kw * ratio) for value, ratio in zip(block, centered)]
        block_mean = sum(disturbed) / 4
        if block_mean > 0:
            disturbed = [value * mean_kw / block_mean for value in disturbed]
        observed_max = max(observed_max, *(abs(value / mean_kw - 1) for value in disturbed if mean_kw))
        output.extend(disturbed)
    return output, {
        "enabled": True,
        "kind": "deterministic_zero_mean_intra_hour",
        "seed_basis": target_date.isoformat(),
        "configured_max_ratio": max_ratio,
        "max_ratio": round(observed_max, 6),
        "hourly_energy_preserved": True,
    }


def _parse_excel_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(int(value)) if isinstance(value, (int, float)) else str(value).strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


def load_tariff_prices(
    month: int,
    raw_dir: Path = DATA_RAW_DIR,
) -> tuple[dict[str, float] | None, dict[str, Any]]:
    """读取黄花电费底稿的时段电费合计单价，而非零售交易小计。"""
    path = raw_dir / "厂务底稿实际电价组成格式（黄花）2026.7.6.csv"
    if not path.exists():
        return None, _provenance("raw_tariff_ledger", path, reason="file_missing")
    label_map = {"尖": "sharp", "峰": "peak", "平": "flat", "谷": "valley"}
    try:
        with path.open("r", encoding="gb18030", newline="") as handle:
            reader = csv.DictReader(handle)
            period_column = next(name for name in reader.fieldnames or [] if name.startswith("时段\n"))
            price_column = next(name for name in reader.fieldnames or [] if name.startswith("合计单价"))
            current_month: int | None = None
            rates_by_month: dict[int, dict[str, float]] = {}
            for row in reader:
                month_text = (row.get("月份") or "").strip()
                if month_text:
                    digits = "".join(char for char in month_text if char.isdigit())
                    if digits:
                        current_month = int(digits[-2:]) if int(digits[-2:]) <= 12 else int(digits[-1])
                label = (row.get(period_column) or "").strip()
                raw_price = (row.get(price_column) or "").strip()
                if current_month and label in label_map and raw_price:
                    rates_by_month.setdefault(current_month, {})[label_map[label]] = float(raw_price)
        rates = rates_by_month.get(month)
        required = {"sharp", "peak", "flat", "valley"} if month in (1, 7, 8, 12) else {"peak", "flat", "valley"}
        if rates and required.issubset(rates):
            if "sharp" not in rates:
                rates["sharp"] = rates["peak"]
            return rates, _provenance(
                "raw_tariff_ledger",
                path,
                loaded=True,
                ledger_month=month,
                price_column="合计单价",
                unit="CNY/kWh",
            )
        return None, _provenance("raw_tariff_ledger", path, reason="month_not_found_or_incomplete")
    except Exception as exc:
        return None, _provenance("raw_tariff_ledger", path, reason=f"{type(exc).__name__}: {exc}")


def load_generation_mix(
    target_date: date,
    raw_dir: Path = DATA_RAW_DIR,
) -> tuple[list[dict[str, float]] | None, dict[str, Any]]:
    """按时间戳合并湖南电网实际发电序列，并保留未分类出力为 other。"""
    folder = raw_dir / "hunan_core"
    files = {
        "coal": ("实时发电_火电.csv", "实时发电_火电"),
        "hydro": ("实时水电及抽蓄.csv", "实时水电及抽蓄"),
        "wind": ("实时发电_风电.csv", "实时发电_风电"),
        "solar": ("实时发电_光伏.csv", "实时发电_光伏"),
        "total": ("实时总发电出力.csv", "实时总发电出力"),
    }
    if not all((folder / filename).exists() for filename, _ in files.values()):
        return None, _provenance("raw_hunan_generation", folder, reason="file_missing")
    try:
        coal_path = folder / files["coal"][0]
        available_dates: set[date] = set()
        with coal_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    available_dates.add(date.fromisoformat(row["time"][:10]))
                except (KeyError, ValueError):
                    continue
        if not available_dates:
            return None, _provenance("raw_hunan_generation", folder, reason="no_valid_dates")
        selected_date = min(available_dates, key=lambda item: (abs((item - target_date).days), item))
        series: dict[str, dict[str, float]] = {}
        source_files: list[str] = []
        for key, (filename, value_column) in files.items():
            path = folder / filename
            source_files.append(str(path))
            values: dict[str, float] = {}
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    timestamp = row["time"]
                    if not timestamp.startswith(selected_date.isoformat()):
                        continue
                    values[timestamp] = float(row[value_column])
            series[key] = values
        common = sorted(set.intersection(*(set(values) for values in series.values())))
        if len(common) != 96:
            return None, _provenance(
                "raw_hunan_generation",
                folder,
                reason=f"expected_96_common_points_got_{len(common)}",
            )
        result = []
        for timestamp in common:
            coal = max(0.0, series["coal"][timestamp])
            hydro = max(0.0, series["hydro"][timestamp])
            wind = max(0.0, series["wind"][timestamp])
            solar = max(0.0, series["solar"][timestamp])
            total = max(coal + hydro + wind + solar, series["total"][timestamp])
            other = max(0.0, total - coal - hydro - wind - solar)
            result.append({
                "coal": coal,
                "hydro": hydro,
                "wind": wind,
                "solar": solar,
                "other": other,
                "total": total,
            })
        return result, _provenance(
            "raw_hunan_generation",
            folder,
            loaded=True,
            selected_date=selected_date.isoformat(),
            exact_date=selected_date == target_date,
            points=96,
            unit="MW",
            files=source_files,
        )
    except Exception as exc:
        return None, _provenance("raw_hunan_generation", folder, reason=f"{type(exc).__name__}: {exc}")


def load_asset_registry(raw_dir: Path = DATA_RAW_DIR) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取冷机与空压机台账并计算可审计汇总。"""
    folder = raw_dir / "HVAC_AC"
    chiller_path = folder / "黄花冷机调研.csv"
    compressor_path = folder / "黄花空压机负荷情况.csv"
    if not chiller_path.exists() or not compressor_path.exists():
        return {}, _provenance("raw_asset_registry", folder, reason="file_missing")
    try:
        chillers = _read_second_header_csv(chiller_path)
        compressors = _read_second_header_csv(compressor_path)
        installed_cooling = sum(float(row["设备数量(台)"]) * float(row["额定功率（kW）"]) for row in chillers)
        running_cooling = sum(float(row["目前运行台数"]) * float(row["额定功率（kW）"]) for row in chillers)
        installed_compressor = sum(float(row["数量"]) * float(row["额定功率kW"]) for row in compressors)
        assets = {
            "chillers": chillers,
            "compressors": compressors,
            "summary": {
                "chiller_units": int(sum(float(row["设备数量(台)"]) for row in chillers)),
                "running_chiller_units": int(sum(float(row["目前运行台数"]) for row in chillers)),
                "installed_cooling_kw": round(installed_cooling, 1),
                "running_cooling_kw": round(running_cooling, 1),
                "compressor_units": int(sum(float(row["数量"]) for row in compressors)),
                "installed_compressor_power_kw": round(installed_compressor, 1),
            },
        }
        return assets, _provenance(
            "raw_asset_registry",
            folder,
            loaded=True,
            files=[str(chiller_path), str(compressor_path)],
            note="冷机额定功率字段按额定制冷量解释，待设备铭牌复核",
        )
    except Exception as exc:
        return {}, _provenance("raw_asset_registry", folder, reason=f"{type(exc).__name__}: {exc}")


def _read_second_header_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="gb18030", newline="") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 3:
        return []
    headers = rows[1]
    return [
        {headers[index]: value.strip() for index, value in enumerate(row[: len(headers)])}
        for row in rows[2:]
        if any(value.strip() for value in row)
    ]
