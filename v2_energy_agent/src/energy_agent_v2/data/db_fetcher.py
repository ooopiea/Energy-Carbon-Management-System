from __future__ import annotations

import os
import urllib.parse
from datetime import datetime
from pathlib import Path

from energy_agent_v2.contracts import GenerationMixPoint
from energy_agent_v2.errors import AppError

DEFAULT_ENV_FILE = (
    r"D:\ZBY_synchronization\【博士】其他项目\清鹏智能\zhangbeiyuan\tianyan-datahub\.env"
)


class HunanDataUnavailable(AppError):
    """湖南数据不可用（数据库驱动缺失或不可连）。"""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(code="hunan_data_unavailable", message=message, details=details)


# 发电结构能源类型标签 -> GenerationSource 规范值
_SOURCE_ALIASES = {
    "火电": "coal", "煤": "coal", "coal": "coal", "thermal": "coal",
    "水电": "hydro", "水": "hydro", "hydro": "hydro",
    "风电": "wind", "wind": "wind",
    "光伏": "solar", "太阳能": "solar", "solar": "solar", "pv": "solar",
    "核电": "nuclear", "nuclear": "nuclear",
    "气电": "gas", "gas": "gas",
    "生物质": "biomass", "biomass": "biomass",
    "外购": "purchase", "purchase": "purchase", "区外": "purchase",
    "其他": "other_renewable", "other": "other_renewable",
}


class HunanDataFetcher:
    """湖南 EData PostgreSQL 数据拉取器。

    连接串依次取自：构造参数 ``db_uri`` -> 环境变量 ``EDATA_DB_URI`` ->
    tianyan-datahub 的 ``.env`` 文件。查询 ``market_data`` 表的湖南实时
    发电结构（``real_time_generation_type_energy_by_period``）与实时出清价
    （``real_time_avg_clearing_price``）。

    当 psycopg2 / SQLAlchemy 均缺失、或数据库不可连时，所有 fetch 方法
    捕获异常并返回空集合，绝不向上抛出；由调用方（如 SeedDataProvider）
    决定是否回退合成数据。
    """

    KEY_GENERATION_MIX = "real_time_generation_type_energy_by_period"
    KEY_REALTIME_PRICE = "real_time_avg_clearing_price"
    TABLE = "market_data"

    def __init__(
        self,
        db_uri: str | None = None,
        env_file: str | Path | None = None,
        region: str = "cn-hunan",
    ) -> None:
        self.region = region
        self.env_file = Path(env_file) if env_file else Path(DEFAULT_ENV_FILE)
        self.db_uri = db_uri or self._resolve_db_uri()

    # ------------------------------------------------------------- 连接串解析
    def _resolve_db_uri(self) -> str | None:
        uri = os.environ.get("EDATA_DB_URI")
        if uri:
            return uri.strip()
        return self._read_env_file("EDATA_DB_URI")

    def _read_env_file(self, key: str) -> str | None:
        # 优先使用 python-dotenv（若已安装）
        try:
            from dotenv import dotenv_values

            values = dotenv_values(str(self.env_file))
            if values.get(key):
                return str(values[key]).strip()
        except Exception:
            pass
        # 降级：自行解析 KEY=VALUE（兼容行内注释与引号）
        if not self.env_file.exists():
            return None
        for raw in self.env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                v = v.split("#", 1)[0].strip()  # 去行内注释
                if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                    v = v[1:-1]
                return v or None
        return None

    # ------------------------------------------------------------- 驱动探测
    def _open_handle(self):
        """打开数据库句柄，返回 (kind, handle)。

        SQLAlchemy 优先（pandas 直读更便利），其次 psycopg2；两者均不可用
        或不可连时抛 HunanDataUnavailable。
        """
        if not self.db_uri:
            raise HunanDataUnavailable("未配置 EDATA_DB_URI（环境变量或 .env 均缺失）")
        uri = self._normalize_uri(self.db_uri)
        # 1) SQLAlchemy
        try:
            import sqlalchemy as sa

            engine = sa.create_engine(
                uri,
                pool_pre_ping=True,
                connect_args={"connect_timeout": 5},
            )
            return "sqlalchemy", engine
        except Exception:
            pass
        # 2) psycopg2
        try:
            import psycopg2  # type: ignore

            params = self._uri_to_psycopg2(uri)
            conn = psycopg2.connect(connect_timeout=5, **params)
            return "psycopg2", conn
        except Exception as exc:  # noqa: BLE001
            raise HunanDataUnavailable(
                "psycopg2 与 SQLAlchemy 均不可用或数据库不可连",
                details={"uri_host": self._safe_host(uri), "reason": str(exc)},
            )

    @staticmethod
    def _normalize_uri(uri: str) -> str:
        # SQLAlchemy 2.x 推荐 postgresql://（postgres:// 仍可用但会弃用警告）
        if uri.startswith("postgres://"):
            return "postgresql://" + uri[len("postgres://") :]
        return uri

    @staticmethod
    def _uri_to_psycopg2(uri: str) -> dict:
        parsed = urllib.parse.urlparse(uri)
        return {
            "host": parsed.hostname,
            "port": parsed.port or 5432,
            "user": parsed.username,
            "password": urllib.parse.unquote(parsed.password) if parsed.password else None,
            "dbname": (parsed.path or "/").lstrip("/") or "tianyan",
        }

    @staticmethod
    def _safe_host(uri: str) -> str | None:
        try:
            return urllib.parse.urlparse(uri).hostname
        except Exception:
            return None

    # ------------------------------------------------------------- 基础查询
    def _query_rows(self, key: str, start: datetime, end: datetime) -> list[tuple]:
        kind, handle = self._open_handle()
        try:
            if kind == "sqlalchemy":
                import pandas as pd
                import sqlalchemy as sa

                sql = sa.text(
                    f"SELECT time, value, tags FROM {self.TABLE} "
                    "WHERE key = :key AND time BETWEEN :start AND :end ORDER BY time"
                )
                df = pd.read_sql(
                    sql, handle, params={"key": key, "start": start, "end": end}
                )
                return list(df.itertuples(index=False, name=None))
            # psycopg2
            sql = (
                f"SELECT time, value, tags FROM {self.TABLE} "
                "WHERE key = %s AND time BETWEEN %s AND %s ORDER BY time"
            )
            cur = handle.cursor()
            try:
                cur.execute(sql, (key, start, end))
                return list(cur.fetchall())
            finally:
                cur.close()
        finally:
            if kind == "psycopg2":
                handle.close()
            # sqlalchemy engine 由对象持有，不在单次查询中 dispose

    # ------------------------------------------------------------- 对外方法
    def fetch_generation_mix(
        self, start: datetime, end: datetime
    ) -> list[GenerationMixPoint]:
        """下载湖南实时发电结构，按时间聚合各能源类型；任何异常返回空列表。"""
        try:
            rows = self._query_rows(self.KEY_GENERATION_MIX, start, end)
        except Exception:
            return []
        by_time: dict[datetime, dict[str, float]] = {}
        for ts, value, tags in rows:
            source = self._parse_source(tags)
            moment = self._as_datetime(ts)
            bucket = by_time.setdefault(moment, {})
            bucket[source] = bucket.get(source, 0.0) + float(value)
        return [
            GenerationMixPoint(timestamp=ts, generation_by_source_kw=src)
            for ts, src in sorted(by_time.items())
        ]

    def fetch_realtime_price(self, start: datetime, end: datetime) -> list[float]:
        """下载湖南实时出清价（元/MWh 原始值）；任何异常返回空列表。"""
        try:
            rows = self._query_rows(self.KEY_REALTIME_PRICE, start, end)
        except Exception:
            return []
        return [float(r[1]) for r in rows]

    # ------------------------------------------------------------- 辅助
    @staticmethod
    def _parse_source(tags) -> str:
        """从 tags 列解析能源类型，兼容字符串 / JSON / dict。"""
        if tags is None:
            return "other_renewable"
        if isinstance(tags, dict):
            for v in tags.values():
                key = str(v).strip().lower()
                if key in _SOURCE_ALIASES:
                    return _SOURCE_ALIASES[key]
            return "other_renewable"
        s = str(tags).strip().lower()
        for alias, canonical in _SOURCE_ALIASES.items():
            if alias.lower() in s:
                return canonical
        return "other_renewable"

    @staticmethod
    def _as_datetime(ts) -> datetime:
        if isinstance(ts, datetime):
            return ts
        try:
            return ts.to_pydatetime()  # type: ignore[union-attr]
        except AttributeError:
            return datetime.fromisoformat(str(ts))