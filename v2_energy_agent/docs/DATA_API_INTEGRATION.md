# 电碳 / 电价 API 接入说明

> 本文档供实施 agent 阅读。目标：将外部电碳（发电结构→碳排放因子）和电价 API
> 接入 v2_energy_agent 的 LangGraph 调度引擎，使 MILP 优化使用真实市场数据
> 而非合成种子数据。

---

## 1. 架构总览

系统有两层，数据通过 **fetcher 鸭子类型协议** 注入：

```
前端 dashboard.html
    │ HTTP
    ▼
server.py  (LangGraph 调度引擎, 端口 8000)
    │  create_run → load_inputs → compute_tariff → optimize_storage → compute_carbon → human_approval
    │                         ▲
    │            SeedDataProvider.load_dispatch_inputs(site_id, date)
    │                         │
    │           ┌─────────────┴──────────────┐
    │           │  fetcher (鸭子类型协议)      │
    │           │  fetch_generation_mix()     │ → generation_mix → CarbonAccountant → C(τ)/Cr(τ)
    │           │  fetch_realtime_price()     │ → electricity_price → TariffSchedule → 元/kWh
    │           └─────────────────────────────┘
    │
    │  fetcher 为空或返回空 → 回退合成种子数据 (SeedDataProvider 内部逻辑)
```

**电碳数据流：** `fetcher.fetch_generation_mix()` → `GenerationMixPoint[]` → `CarbonAccountant.compute_factors()` → C(τ) 直接碳因子 / Cr(τ) 江亿动态责任因子 → 进入 MILP 目标函数（MIN_CARBON 或 WEIGHTED 模式）

**电价数据流：** `fetcher.fetch_realtime_price()` → `list[float]`（元/MWh）→ provider 除以 1000 转元/kWh → 写入 `TariffSchedule.price_cny_per_kwh_by_step` → `resolve_price_series()` → 进入 MILP 目标函数（MIN_COST 模式）

---

## 2. Fetch 协议规范（鸭子类型）

不需要继承任何基类。只要实现以下两个方法，异常时返回空集合即可：

```python
from datetime import datetime
from energy_agent_v2.contracts import GenerationMixPoint

class YourApiDataFetcher:
    def fetch_generation_mix(
        self, start: datetime, end: datetime
    ) -> list[GenerationMixPoint]:
        """
        拉取 [start, end] 时间段的发电结构。
        必须返回 96 个点（15 分钟粒度），时间戳从 start 当天 00:00 开始。
        任何异常返回空列表 []，不要抛异常。
        """
        ...

    def fetch_realtime_price(
        self, start: datetime, end: datetime
    ) -> list[float]:
        """
        拉取 [start, end] 时间段的实时出清价格。
        返回 96 个 float，单位 元/MWh（provider 内部会除以 1000 转元/kWh）。
        任何异常返回空列表 []，不要抛异常。
        """
        ...
```

**现有参考实现：**
| 实现类 | 文件 | 数据源 |
|--------|------|--------|
| `CSVDataFetcher` | `src/energy_agent_v2/data/csv_fetcher.py` | 本地 CSV |
| `HunanDataFetcher` | `src/energy_agent_v2/data/db_fetcher.py` | PostgreSQL DB |

新建 `src/energy_agent_v2/data/api_fetcher.py` 作为第三个实现。

---

## 3. 接入点（改哪里）

**唯一需要改动的地方：** `src/energy_agent_v2/runner.py` 的 `create_app_context()`，约 L44：

```python
# 当前代码（合成数据，无 fetcher）：
data_provider=SeedDataProvider(),

# 改为（注入你的 API fetcher）：
from energy_agent_v2.data.api_fetcher import YourApiDataFetcher
data_provider=SeedDataProvider(fetcher=YourApiDataFetcher(...)),
```

**不需要改动的地方：**
- `provider.py` 的 `load_dispatch_inputs()` 已经内置 fetcher 调用 + 回退逻辑
- `orchestration.py` 的 `load_inputs_node` 不变
- `server.py` 不变
- 所有算法层（MILP / carbon / tariff）不变

---

## 4. 电碳数据格式

### 4.1 GenerationMixPoint

```python
GenerationMixPoint(
    timestamp=datetime(2026, 7, 27, 0, 0, 0),  # 必填，UTC 或本地时间均可
    generation_by_source_kw={
        "coal":   35000.0,   # 火电功率 kW
        "solar":   2000.0,   # 光伏 kW
        "wind":    3000.0,   # 风电 kW
        "hydro":  12000.0,   # 水电 kW
        # 以下可选，没有就不填：
        # "gas": 0.0, "oil": 0.0, "nuclear": 0.0,
        # "biomass": 0.0, "purchase": 0.0, "other_renewable": 0.0,
    },
)
```

### 4.2 能源类型标准标签

必须使用以下字符串值（`GenerationSource` 枚举）：

| 标签 | 含义 | 默认排放因子 (kgCO2/kWh) |
|------|------|--------------------------|
| `coal` | 火电/煤电 | 0.85 |
| `gas` | 天然气 | 0.40 |
| `oil` | 燃油 | 0.75 |
| `hydro` | 水电（含抽蓄发电态，抽水态取 max(0,val)） | 0.0 |
| `wind` | 风电 | 0.0 |
| `solar` | 光伏 | 0.0 |
| `nuclear` | 核电 | 0.0 |
| `biomass` | 生物质 | 0.0 |
| `purchase` | 外购电 | 0.5366 |
| `other_renewable` | 其他可再生能源 | 0.0 |

如果 API 返回的能源类型名称不同（如中文"火电""水电"），参考 `db_fetcher.py` 的 `_SOURCE_ALIASES` 做映射。

### 4.3 点数和粒度

- 固定 **96 个点**（一天，15 分钟粒度）
- 时间戳从 `start` 当天 `00:00` 开始，到 `23:45`
- 各能源功率单位为 **kW**（不是 MW、不是 kWh）
- `total_generation_kw` 由 `GenerationMixPoint` 自动计算（`sum(generation_by_source_kw.values())`），不需要手动填

### 4.4 碳因子计算逻辑

`CarbonAccountant.compute_factors()` 按 `generation_by_source_kw` 加权平均各能源的排放因子，
得到该时步的直接碳因子 C(τ)：

```
C(τ) = Σ(source_power[τ] × EF[source]) / total_generation[τ]
```

所以只要发电结构正确，碳因子自动正确，不需要单独传碳因子 API。

---

## 5. 电价数据格式

```python
fetch_realtime_price(start, end) -> [450.0, 430.0, 410.0, ..., 380.0]
#                               96 个 float，单位 元/MWh
```

**单位非常重要：** 返回值是 **元/MWh**（电力市场出清价格的标准单位）。
`SeedDataProvider` 内部会执行 `price / 1000.0` 转成 `元/kWh`。

如果 API 直接返回元/kWh，需要在你的 fetcher 里先 `× 1000` 再返回。

---

## 6. SeedDataProvider 的 fetcher 调用逻辑（provider.py L228-252）

这是你不用改但要理解的回退逻辑：

```python
effective_fetcher = fetcher or self._fetcher   # 构造时传入的 fetcher
data_source = "seed"                            # 默认标记

if effective_fetcher is not None:
    # 1. 试拉发电结构
    remote_mix = effective_fetcher.fetch_generation_mix(start, end)  # 异常被 catch，返回 []
    if remote_mix:
        generation_mix = remote_mix              # 用真实数据

    # 2. 试拉电价（必须正好 96 点才会覆盖合成分时电价）
    remote_prices = effective_fetcher.fetch_realtime_price(start, end)
    if remote_prices and len(remote_prices) == POINTS_PER_DAY:
        clearing_prices = [p / 1000.0 for p in remote_prices]   # 元/MWh → 元/kWh
        tariff = tariff.model_copy(update={"price_cny_per_kwh_by_step": clearing_prices})
        data_source = "csv-realtime"             # 标记为真实数据
```

**关键行为：**
- 发电结构和电价**独立回退**：API 拉到电价但拉不到发电结构时，电价用真实、发电结构用合成
- 电价必须**正好 96 个点**才会覆盖，否则保留合成分时电价
- fetcher 抛异常不影响主流程，provider 会 catch 后回退

---

## 7. 实现步骤

### 步骤 1：创建 fetcher

```python
# src/energy_agent_v2/data/api_fetcher.py
from __future__ import annotations
from datetime import datetime
import requests  # 或 httpx / aiohttp
from energy_agent_v2.contracts import GenerationMixPoint

class ApiDataFetcher:
    """从外部 API 拉取发电结构与实时电价。"""

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def fetch_generation_mix(self, start: datetime, end: datetime) -> list[GenerationMixPoint]:
        try:
            # TODO: 调用你的 API，把返回数据映射成 GenerationMixPoint
            # 关键：能源类型 key 必须是 coal/solar/wind/hydro 等标准标签
            # 关键：功率单位必须是 kW
            ...
        except Exception:
            return []  # 异常时返回空，让 provider 回退合成数据

    def fetch_realtime_price(self, start: datetime, end: datetime) -> list[float]:
        try:
            # TODO: 调用你的 API，返回 96 个 float，单位 元/MWh
            ...
        except Exception:
            return []
```

### 步骤 2：注入到 runner

```python
# src/energy_agent_v2/runner.py create_app_context() 约 L44
from energy_agent_v2.data.api_fetcher import ApiDataFetcher
# ...
data_provider=SeedDataProvider(fetcher=ApiDataFetcher(
    base_url="https://your-api.example.com",
    api_key=os.environ.get("MARKET_API_KEY", ""),
)),
```

### 步骤 3：配置环境变量

在 `.env` 中添加 API 地址和密钥：

```
MARKET_API_BASE_URL=https://your-api.example.com
MARKET_API_KEY=your_key_here
```

### 步骤 4：验证

```powershell
cd D:\ZBY_synchronization\【博士】其他项目\碳中和实践\v2_energy_agent

# 直接测试 fetcher（不走 LangGraph）
python -c @"
import sys; sys.path.insert(0, 'src')
from datetime import datetime
from energy_agent_v2.data.api_fetcher import ApiDataFetcher
f = ApiDataFetcher(base_url='...')
mix = f.fetch_generation_mix(datetime(2026,7,27), datetime(2026,7,27,23,45))
prices = f.fetch_realtime_price(datetime(2026,7,27), datetime(2026,7,27,23,45))
print(f'mix points: {len(mix)}, price points: {len(prices)}')
"@

# 通过 LangGraph 端到端测试（需要 escalated 权限跑 server）
python work/e2e_revise.py
```

验证成功的标志：`create_run` 返回的 `input_bundle.data_version` 不是 `"seed-20260726"`，
而是 `"csv-realtime"` 或你在 fetcher 里自定义的标记。

---

## 8. 注意事项

1. **能源类型映射**：API 返回的字段名几乎不可能直接匹配 coal/solar/wind/hydro。
   参考 `db_fetcher.py` 的 `_SOURCE_ALIASES`，建一个映射表。映射不上的能源可以忽略
   （只影响 `total_generation` 的精度），但不能用未知 key（会被 carbon_accounting 忽略）。

2. **功率修正系数**：如果 API 数据各能源被系统性低估（CSV 版本就需要 ×5.76 等修正系数），
   参照 `csv_fetcher.py` 的 `_GEN_CORRECTION`。真实 API 通常不需要。

3. **电价必须 96 点**：`fetch_realtime_price` 返回长度 ≠ 96 时，provider 会忽略它、
   回退合成分时电价。你的 API 如果返回 288 点（5 分钟粒度）或 24 点（1 小时粒度），
   需要在 fetcher 里重采样到 96 点。

4. **时间戳对齐**：API 数据的日期可能和请求日期不同（比如只有历史数据）。
   `CSVDataFetcher` 用 `_nearest_day()` 取最近可用日期，你可以参照实现。

5. **不要在 fetcher 里做单位转换（除了元/kWh↔元/MWh）**：
   所有 SOC、功率、能量的单位转换都在 provider/optimizer 里统一处理。

6. **异步还是同步**：当前 `load_dispatch_inputs` 是同步调用。
   如果 API 延迟高（>2 秒），考虑在 fetcher 内部加缓存或预拉取。
   graph 节点 `load_inputs` 有 `time.perf_counter()` 计时，会显示在事件链里。

---

## 9. 文件清单（完整路径）

```
src/energy_agent_v2/
├── data/
│   ├── provider.py        ← 数据组装中心（不用改，理解 L228-252）
│   ├── csv_fetcher.py     ← 参考实现 1（CSV）
│   ├── db_fetcher.py      ← 参考实现 2（PostgreSQL）
│   └── api_fetcher.py     ← 【新建】你的 API fetcher
├── contracts.py           ← GenerationMixPoint / GenerationSource / TariffSchedule 定义
├── runner.py              ← create_app_context() L44 改注入点
└── orchestration.py       ← load_inputs_node（不用改）

docs/
└── DATA_API_INTEGRATION.md  ← 本文档
```
