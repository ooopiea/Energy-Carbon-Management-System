"""黄花园区原始数据读取器。

该模块只做确定性的格式转换，不对缺失数据伪装成实测值。每个读取函数均返回
``(data, provenance)``；无法读取时返回 ``None`` 和带错误原因的溯源信息，由上层
模拟器明确启用工程回退数据。
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from core.config import DATA_RAW_DIR


TIME_LABELS = [f"{minute // 60:02d}:{minute % 60:02d}" for minute in range(0, 1440, 15)]


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
    """读取目标日 96 点园区负荷；找不到精确日期时选同月最近日期。"""
    path = raw_dir / "daily_load_real" / "total_substation" / "用电负荷参考_15min.xlsx"
    if not path.exists():
        return None, _provenance("raw_15min_load", path, reason="file_missing")
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=True)
        sheet = workbook["0"]
        rows = sheet.iter_rows(values_only=True)
        headers = [str(value) if value is not None else "" for value in next(rows)]
        date_index = headers.index("日期")
        time_indexes = [headers.index(label) for label in TIME_LABELS]
        candidates: list[tuple[date, list[float]]] = []
        for row in rows:
            raw_date = row[date_index]
            if raw_date is None:
                continue
            parsed = _parse_excel_date(raw_date)
            if parsed is None or parsed.month != target_date.month:
                continue
            values = [float(row[index]) for index in time_indexes]
            if len(values) == 96 and all(value >= 0 for value in values):
                candidates.append((parsed, values))
        workbook.close()
        if not candidates:
            return None, _provenance("raw_15min_load", path, reason="month_not_found")
        selected_date, values = min(
            candidates,
            key=lambda item: (abs((item[0] - target_date).days), item[0]),
        )
        return values, _provenance(
            "raw_15min_load",
            path,
            loaded=True,
            selected_date=selected_date.isoformat(),
            exact_date=selected_date == target_date,
            points=96,
            unit="kW",
        )
    except Exception as exc:  # 数据源损坏时由上层显式降级
        return None, _provenance("raw_15min_load", path, reason=f"{type(exc).__name__}: {exc}")


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
