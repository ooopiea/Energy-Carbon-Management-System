"""从本地 CSV 读取湖南发电结构与湖北出清价格的数据获取器。

学术正当性（SPEC_SCENARIOS D1/D6）：湖南本省实时/日前出清均价全天平价（如
2025-06-11 全天 453.73 元/MWh 无分时波动），无法作为 RTP 套利信号；湖北属华中
电网与湖南耦合度高，其省级实时出清价格有真实日内波动，作为湖南园区 RTP 场景
替代信号。湖北出清价为批发侧价格，不含输配电价；场景 C 将其作电量电价分时信号，
叠加黄花固定附加费（政府基金/力调/需量），实现三场景可比口径。

数据来源（v2_energy_agent/data/）：
- hunan_core/: 湖南实时发电结构（火电/光伏/风电/水电及抽蓄/总出力）
- hubei_rt_clearing/: 湖北实时出清价格（替代湖南日内均价，因为湖南出清均价无分时波动）

数据修正：
- 原始 CSV 各能源出力被低估（火电/风电/光伏仅真实值的 1/5~1/6，导致差额虚高为外购电）
- 按真实年发电量比例修正：保持总出力形状不变，各分量缩放使其和等于总出力
- 修正后四项能源占总出力 100%，消除虚假的外购电分量

发电结构处理：
- 火电->coal, 光伏->solar, 风电->wind, 水电及抽蓄->hydro
- 抽蓄抽水时水电及抽蓄值为负，取 max(0, val) 不计为发电
- 总出力直接作为各点 total_generation_kw，不归入 purchase（修正后分量和≈总出力）

电价处理：
- 湖北实时出清价格原始单位为元/MWh，fetch_realtime_price 返回原始值
- 调用方（SeedDataProvider）负责元/MWh -> 元/kWh 的换算（除以 1000）
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from energy_agent_v2.contracts import GenerationMixPoint

_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data"

# CSV 文件名 -> 标准能源类型
_GEN_FILES: dict[str, str] = {
    "coal": "实时发电_火电.csv",
    "solar": "实时发电_光伏.csv",
    "wind": "实时发电_风电.csv",
    "hydro": "实时水电及抽蓄.csv",
}
_TOTAL_FILE = "实时总发电出力.csv"
_PRICE_DIR = "hubei_rt_clearing"
_PRICE_FILE = "实时出清价格.csv"

# 每日真实负荷目录（变电站按日 CSV，每天一个文件）
# 文件名格式: 变电站实时负荷-{月}月_{月.日}.csv（例: 变电站实时负荷-6月_6.15.csv）
_LOAD_DIR = "daily_load_real/different_substation"

# 各能源出力修正系数（真实年发电量比例 / 原始数据比例）
# 真实比例: 火60.6% 水21.2% 风13.3% 光4.8%（来源：湖南2025实际发电量）
# 原始数据比例: 火10.5% 水21.8% 风2.8% 光0.9% → 差额虚高为外购电64%
# 修正后各分量缩放，使其和等于总出力，消除虚假外购电
_GEN_CORRECTION: dict[str, float] = {
    "coal": 5.7607,
    "solar": 5.6432,
    "wind": 4.7955,
    "hydro": 0.9715,
}


_POINTS_PER_DAY = 96
_STEP_MINUTES = 15


class CSVDataFetcher:
    """从本地 CSV 文件读取湖南发电结构与湖北出清价格。

    实现与 HunanDataFetcher 相同的接口（fetch_generation_mix /
    fetch_realtime_price），数据来源为本地 CSV 而非数据库。
    当请求的日期不在数据范围内时，取最近可用日期的数据。
    任何文件缺失或解析异常时返回空集合，绝不向上抛出。
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self.hunan_dir = self.data_dir / "hunan_core"
        self.price_path = self.data_dir / _PRICE_DIR / _PRICE_FILE
        self._cache: dict[str, dict[datetime, float]] = {}
        self.last_generation_date: datetime | None = None
        self.last_price_date: datetime | None = None
        self.last_load_date: datetime | None = None
        self.data_coverage: dict[str, str] = {}
        self._load_index: dict[date, Path] | None = None

    # ------------------------------------------------------------- CSV 读取
    def _load_csv(self, path: Path) -> dict[datetime, float]:
        """读取两列 CSV (time, value)，逐行容错，返回 {datetime: float}。"""
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        result: dict[datetime, float] = {}
        try:
            with path.open(encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header
                for row in reader:
                    try:
                        if len(row) < 2 or not row[0].strip():
                            continue
                        ts = datetime.strptime(row[0].strip(), "%Y-%m-%d %H:%M:%S")
                        result[ts] = float(row[1])
                    except (ValueError, IndexError):
                        continue  # 跳过格式异常的行，不整批失败
        except (FileNotFoundError, OSError):
            return {}
        self._cache[key] = result
        return result

    # ------------------------------------------------------------- 日期匹配
    @staticmethod
    def _nearest_day(target: datetime, available: set[datetime]) -> datetime | None:
        """在可用时间戳中找 target 所在日期；不存在则取最近日期的 00:00。"""
        target_date = target.date()
        dates = sorted({ts.date() for ts in available})
        if not dates:
            return None
        if target_date in dates:
            return datetime.combine(target_date, datetime.min.time())
        nearest = min(dates, key=lambda d: abs((d - target_date).days))
        return datetime.combine(nearest, datetime.min.time())

    @staticmethod
    def _day_grid(
        data: dict[datetime, float], day: datetime
    ) -> list[tuple[datetime, float]]:
        """对齐到 00:00~23:45 的 96 点标准网格；缺失点用前值填充。"""
        day_date = day.date()
        by_ts = {ts: val for ts, val in data.items() if ts.date() == day_date}
        base = datetime.combine(day_date, datetime.min.time())
        grid: list[tuple[datetime, float]] = []
        last_val = 0.0
        for i in range(_POINTS_PER_DAY):
            ts = base + timedelta(minutes=_STEP_MINUTES * i)
            val = by_ts.get(ts)
            if val is not None:
                last_val = val
            grid.append((ts, last_val))
        return grid

    # ------------------------------------------------------------- 对外方法
    def fetch_generation_mix(
       self, start: datetime, end: datetime
    ) -> list[GenerationMixPoint]:
        """读取湖南发电结构，返回固定 96 个 GenerationMixPoint；异常时返回空。"""
        try:
            sources = {
                name: self._load_csv(self.hunan_dir / fname)
                for name, fname in _GEN_FILES.items()
            }
            total_data = self._load_csv(self.hunan_dir / _TOTAL_FILE)
        except (FileNotFoundError, OSError, ValueError):
            return []

        ref_data = total_data if total_data else next(iter(sources.values()), {})
        day_start = self._nearest_day(start, set(ref_data.keys()))
        if day_start is None:
            return []

        self.last_generation_date = day_start
        total_grid = self._day_grid(total_data, day_start)
        source_grids = {
            name: {ts: val for ts, val in self._day_grid(data, day_start)}
            for name, data in sources.items()
        }

        points: list[GenerationMixPoint] = []
        for ts, _total_gen in total_grid:
            by_source: dict[str, float] = {}
            for name, grid in source_grids.items():
                val = grid.get(ts, 0.0)
                if name == "hydro":
                    val = max(val, 0.0)  # 抽蓄抽水不计发电
                # 应用修正系数：原始数据各能源被低估，按真实比例缩放
                val *= _GEN_CORRECTION.get(name, 1.0)
                by_source[name] = val
            points.append(
                GenerationMixPoint(timestamp=ts, generation_by_source_kw=by_source)
            )
        return points

    def fetch_realtime_price(self, start: datetime, end: datetime) -> list[float]:
        """读取湖北实时出清价格（元/MWh），固定 96 点；异常时返回空。"""
        try:
            price_data = self._load_csv(self.price_path)
        except (FileNotFoundError, OSError, ValueError):
            return []
        day_start = self._nearest_day(start, set(price_data.keys()))
        if day_start is None:
            return []
        self.last_price_date = day_start
        return [val for _, val in self._day_grid(price_data, day_start)]

    def get_data_provenance(self, target_date) -> dict:
        """Return actual data dates used vs requested, plus coverage range."""
        target_str = target_date.isoformat() if hasattr(target_date, 'isoformat') else str(target_date)
        gen_str = self.last_generation_date.date().isoformat() if self.last_generation_date else None
        price_str = self.last_price_date.date().isoformat() if self.last_price_date else None
        load_str = self.last_load_date.date().isoformat() if self.last_load_date else None
        result = {
            "target_date": target_str,
            "generation_data_date": gen_str,
            "generation_data_exact_match": gen_str == target_str if gen_str else False,
            "price_data_date": price_str,
            "price_data_exact_match": price_str == target_str if price_str else False,
            "load_data_date": load_str,
            "load_data_exact_match": load_str == target_str if load_str else False,
        }
        # Coverage range
        try:
            total = self._load_csv(self.hunan_dir / _TOTAL_FILE)
            if total:
                gen_dates = sorted({ts.date().isoformat() for ts in total})
                result["generation_coverage"] = {"start": gen_dates[0], "end": gen_dates[-1], "days": len(gen_dates)}
        except Exception:
            pass
        try:
            price_data = self._load_csv(self.price_path)
            if price_data:
                price_dates = sorted({ts.date().isoformat() for ts in price_data})
                result["price_coverage"] = {"start": price_dates[0], "end": price_dates[-1], "days": len(price_dates)}
        except Exception:
            pass
        try:
            load_idx = self._build_load_index()
            if load_idx:
                load_dates = sorted(d.isoformat() for d in load_idx)
                result["load_coverage"] = {"start": load_dates[0], "end": load_dates[-1], "days": len(load_dates)}
        except Exception:
            pass
        return result

    # ------------------------------------------------------------- 真实负荷
    def _build_load_index(self) -> dict[date, Path]:
        """扫描日负荷目录，从 CSV header 日期构建索引（惰性缓存）。"""
        if self._load_index is not None:
            return self._load_index
        load_dir = self.data_dir / _LOAD_DIR
        index: dict[date, Path] = {}
        if not load_dir.exists():
            self._load_index = index
            return index
        for f in load_dir.glob("*.csv"):
            try:
                with f.open(encoding="utf-8-sig") as fh:
                    fh.readline()  # 跳过标题行
                    header_line = fh.readline()  # 含日期的表头行
                    fields = header_line.split(",")
                    if len(fields) > 2:
                        date_str = fields[2].strip()  # 如 "2026.4.12"
                        parts = date_str.replace("/", ".").split(".")
                        if len(parts) >= 3:
                            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                            index[date(y, m, d)] = f
            except (ValueError, OSError, IndexError, StopIteration):
                continue
        self._load_index = index
        return index

    def fetch_real_load(self, target: datetime) -> list[float] | None:
        """读取真实日负荷 CSV，返回插值后的 96 点负荷（kW）；无数据时返回 None。"""
        index = self._build_load_index()
        if not index:
            return None
        target_d = target.date()
        if target_d in index:
            file_path = index[target_d]
            self.last_load_date = datetime.combine(target_d, datetime.min.time())
        else:
            nearest = min(index.keys(), key=lambda d: abs((d - target_d).days))
            file_path = index[nearest]
            self.last_load_date = datetime.combine(nearest, datetime.min.time())
        return self._parse_load_file(file_path)

    @staticmethod
    def _parse_load_file(path: Path) -> list[float] | None:
        """解析日负荷 CSV（进线求和 + 线性插值到 96 点），返回 kW 列表。"""
        try:
            with path.open(encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
        except (FileNotFoundError, OSError):
            return None
        if len(rows) < 3:
            return None
        header = rows[1]
        time_cols: list[tuple[int, float]] = []
        for j, v in enumerate(header):
            v = v.strip()
            if ":" not in v or j < 3:
                continue
            t = v.split(" ")[-1]
            try:
                parts = t.split(":")
                hour = int(parts[0]) + int(parts[1]) / 60.0 + int(parts[2]) / 3600.0
                time_cols.append((j, hour))
            except (ValueError, IndexError):
                continue
        if not time_cols:
            return None
        # 对每个时间点，求进线（开群线）负荷总和（万KW -> kW）
        raw: list[tuple[float, float]] = []
        for j, hour in time_cols:
            total_kw = 0.0
            for row in rows[2:]:
                if len(row) <= j or len(row) < 3:
                    continue
                name = row[1].strip()
                unit = row[2].strip()
                if "万" in unit and ("线" in name or "群" in name):
                    try:
                        total_kw += float(row[j]) * 10000.0
                    except (ValueError, IndexError):
                        pass
            raw.append((hour, total_kw))
        if not raw or all(kw <= 0 for _, kw in raw):
            return None
        raw.sort(key=lambda p: p[0])
        raw_hours = [p[0] for p in raw]
        raw_kw = [p[1] for p in raw]
        # 线性插值到 96 点（15 分钟粒度）
        result: list[float] = []
        for i in range(_POINTS_PER_DAY):
            t = i * _STEP_MINUTES / 60.0
            if t <= raw_hours[0]:
                result.append(raw_kw[0])
            elif t >= raw_hours[-1]:
                result.append(raw_kw[-1])
            else:
                for k in range(len(raw_hours) - 1):
                    if raw_hours[k] <= t <= raw_hours[k + 1]:
                        frac = (t - raw_hours[k]) / (raw_hours[k + 1] - raw_hours[k])
                        result.append(raw_kw[k] * (1 - frac) + raw_kw[k + 1] * frac)
                        break
                else:
                    result.append(raw_kw[-1])
        return result
