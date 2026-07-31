# V2 园区能源管理 Agent 系统 —— 架构文档

> 版本：2.0.0 | 更新：2026-07-27
> 验收标准：LangGraph 节点/边编排完整 + 2 个端到端案例跑通

## 一、系统定位

V2 是在 V1（规则储能优化器 + 单一碳因子）基础上的升级，核心变化三点：

1. **储能优化器从规则法升级为 MILP**（PuLP + CBC），增加爬坡约束和电芯热安全模型；
2. **电碳计量从单一因子升级为江亿双因子**：直接排放因子 C(τ) + 动态责任因子 Cr(τ)；
3. **电费接入湖南分时电价**：峰谷价差 0.47–0.71 元/kWh，含需量电费。

设计原则不变：大模型只负责理解意图、选工具、解释结果；算数、预测、优化、硬约束全部由确定性算法执行。

## 二、目录结构

```
v2_energy_agent/
├── src/energy_agent_v2/
│   ├── contracts.py          # Pydantic 契约（所有模块的接口定义）
│   ├── orchestration.py      # LangGraph 状态图（11 节点 + 条件边 + 审批回路）
│   ├── runner.py             # 运行入口（上下文构建、interrupt/resume、结果输出）
│   ├── config.py             # 配置
│   ├── errors.py             # 业务错误
│   ├── monitoring.py         # 异常工况外挂监控图（独立于主调度图）
│   ├── algorithms/
│   │   ├── storage_optimizer.py   # MILP 储能优化器
│   │   ├── carbon_accounting.py  # 电碳双因子计量
│   │   └── tariff.py             # 分时电费计算器
│   ├── llm/                  # LLM agent 集合（client + 4 个语义理解 agent）
│   │   ├── client.py             # OpenAI 兼容 client（GLM-5）+ JSON 双层约束
│   │   ├── json_schemas.py       # 6 个输出 schema 集中定义
│   │   ├── base.py               # BaseLLMAgent 公共基类
│   │   ├── data_ingest.py        # 数据清洗 agent
│   │   ├── data_archive.py       # 数据封存 agent
│   │   ├── anomaly_monitor.py    # 异常工况 agent
│   │   ├── distillation_review.py# 复盘蒸馏 agent
│   │   └── parse_revision.py     # 审批修改意图解析 agent
│       └── storage_approval.py   # 储能审批 Agent（包装 RevisionParser + 物理含义 + 重算决策）
│   └── prompts/              # 各 agent 的 system prompt
│   └── data/
│       ├── provider.py            # 合成种子数据提供者
│       └── db_fetcher.py          # 湖南 EData 数据库下载（优雅降级）
├── tests/                    # 51 项单元测试
├── cases/
│   ├── case1_storage_dispatch/    # 案例1：储能经济调度
│   └── case2_carbon_aware/        # 案例2：电碳感知调度
├── data/                     # 数据目录
└── docs/ARCHITECTURE.md      # 本文档
```

## 三、LangGraph 编排图

### 节点（11 个）

| 节点 | 类型 | 职责 |
|---|---|---|
### 节点（14 个）

| 节点 | 类型 | 职责 |
|---|---|---|
| `ingest_raw_data` | llm | 数据清洗：原始文件 → LLM 判类型 + 字段映射 + 质量检测（无 raw_file 时跳过） |
| `archive_data` | llm | 数据封存：在 freeze_plan 之后执行，清洗结果 → 血缘记录 + 落盘（无 ingest_result 时跳过） |
| `initialize_run` | system | 初始化运行上下文和版本 |
| `load_inputs` | system | 加载日前数据（负荷/电价/发电结构/储能参数） |
| `compute_tariff` | workflow | 计算基线电费（分时电价 + 需量） |
| `optimize_storage` | workflow | MILP 求解储能充放电计划（爬坡/SOC/温度约束） |
| `compute_carbon_factors` | workflow | **优化前**计算 C(τ)/Cr(τ) 碳因子，供 MILP 优化器使用 |
| `factory_summary` | workflow | 生成审批摘要（收益/风险/检查项） |
| `compute_carbon_dispatch` | workflow | **优化后**用预计算碳因子核算方案碳排放（直接 C + 责任 Cr） |
| `storage_approval` | human | 储能审批 interrupt（approve/reject/revise），由 StorageApprovalAgent 支撑 |
| `parse_revision` | llm | LLM 解析审批修改意图（最多 2 轮澄清 + 1 次表单回退） |
| `apply_revision` | workflow | 合并审批修改（约束只能收紧） |
| `freeze_plan` | system | 批准后冻结方案 |
| `close_rejected` | system | 拒绝后关闭 |
| `fail_run` | system | 失败后关闭 |

### 边与条件分支

```
START → initialize_run → load_inputs
```
更新后：
```
START → ingest_raw_data → initialize_run → load_inputs
  load_inputs ──ok──→ compute_tariff ──ok──→ compute_carbon_factors ──ok──→ optimize_storage
             └──failed──→ fail_run → END

  optimize_storage ──ok──→ compute_carbon_dispatch ──ok──→ factory_summary ──ok──→ storage_approval
                  └──failed──→ fail_run                    └──failed──→ fail_run

  storage_approval ──approve──→ freeze_plan → archive_data → END
                ──reject───→ close_rejected → END
                 ──revise───→ parse_revision → apply_revision → optimize_storage（重新优化）
                 注：revise 回路复用已计算的 carbon_factors，不重算碳因子
```

### 审批回路

revise 分支是核心安全机制：工程师可以收紧约束（提高备用 SOC、降低放电上限、封锁时段），
但不能放松。apply_revision 合并修改后回到 optimize_storage 重新求解，生成 V(n+1) 方案。
每次方案版本递增，全部留痕。

### LLM 语义层与 JSON 输出约束

系统严格遵守设计原则：**LLM 只做语义理解，不做优化和硬约束**。
电价计算和碳因子计算是确定性算法工具，不是 LLM agent。

### LLM Agent 清单（5 个，各有独立 API 端点）

|---|---|---|
| Agent | API 端点 | 位置 | 职责 |
|---|---|---|---|
| 数据清洗 `DataIngestAgent` | `POST /api/agents/ingest` | 主图 ingest_raw_data | 原始文件 → 判类型 + 列名映射 + 质量检测 |
| 数据封存 `DataArchiveAgent` | `POST /api/agents/archive` | 主图 archive_data | 清洗结果 → 血缘记录 + 合规标签 + 落盘 |
| 储能审批 `StorageApprovalAgent` | `POST /api/agents/approval/interpret` | 主图 storage_approval | 工程师指令 → 物理含义 + 结构化约束 + 重算决策 |
| 异常工况 `AnomalyMonitorAgent` | `POST /api/agents/anomaly/analyze` | 监控图 analyze_anomaly | 实时信号 → 告警级别 + 根因 + 是否触发重算 |
| 复盘蒸馏 `DistillationReviewAgent` | `POST /api/agents/distillation/review` | 离线 | revise_pairs → 高频模式 + prompt 改进建议 |

> `RevisionParser` 和 `SlotValidator` 是 `StorageApprovalAgent` 的内部支撑类，不单独暴露 API。

### 确定性工具（非 Agent）

| 工具 | 类名 | 位置 | 职责 |
|---|---|---|---|
| 分时电费计算器 | `TariffCalculator` | 主图 compute_tariff | 湖南峰谷电价 + 需量电费（纯数学，无 LLM） |
| 电碳双因子计量 | `CarbonAccountant` | 主图 compute_carbon_factors / compute_carbon_dispatch | C(τ)/Cr(τ) 碳因子计算（纯数学，无 LLM） |
| MILP 储能优化器 | `MILPStorageOptimizer` | 主图 optimize_storage | PuLP+CBC 线性规划求解（纯数学，无 LLM） |
| 种子数据提供者 | `SeedDataProvider` | 主图 load_inputs | 合成数据 + CSV 数据加载（无 LLM） |

**JSON 双层约束**：`client.chat_json(response_schema=...)` 同时在两个层面限制 LLM 输出：
1. API 层 — `response_format={"type": "json_object"}` 强制 JSON mode；
2. Prompt 层 — schema 嵌入 system prompt，让模型知道确切的输出结构。

6 个 schema 集中定义在 `json_schemas.py`，确保所有 agent 输出可被 Pydantic 契约安全解析。

## 四、三大算法模块

### 4.1 MILP 储能优化器（`milp-storage-2.0.0`）

**求解器**：PuLP + CBC（纯 LP 松弛，充放电互斥由效率差自然保证）

**决策变量**（每步 t，96 点 × 15 分钟）：
- `p_ch[t]` 充电功率、`p_dis[t]` 放电功率
- `soc[t]` 荷电状态、`temp[t]` 电芯温度
- `peak`（仅 limit_peak_demand 模式）

**约束**：
- SOC 范围：`min_soc ≤ soc[t] ≤ max_soc`
- 功率上限：`0 ≤ p_ch ≤ max_charge`，`0 ≤ p_dis ≤ max_discharge`
- **爬坡**：`|p[t] - p[t-1]| ≤ max_ramp_kw_per_step`
- 不允许上网：`p_dis - p_ch ≤ load[t]`
- 末端 SOC：`soc[末] ≥ terminal_target`
- **热安全**：一阶热模型 `T[t] = T_amb + (T[t-1]-T_amb)·α + (p_ch+p_dis)·R·(1-α)`，约束 `T[t] ≤ max_cell_temp`

**目标函数**：
- `min_cost`：minimize Σ grid[t]·price[t]·dt
- `limit_peak_demand`：minimize peak
- `min_carbon`：minimize Σ grid[t]·carbon_factor[t]·dt

### 4.2 电碳双因子计量

#### 直接排放因子 C(τ)

```
C(τ)[t] = Σ_s (gen[s,t] · ef[s]) / Σ_s gen[s,t]
```

基于发电结构加权平均的物理碳流强度。总发电为 0 时以区域平均因子（湖南 0.5366）兜底。

#### 江亿动态责任因子 Cr(τ)

```
C̄        = mean(C(τ))
adjust(t) = 1 + λ·(C(τ)[t] - C̄) / max(C̄, ε)     # λ 默认 0.5
Cr(τ)[t] = C(τ)[t] · adjust(t)
``+
低碳时段（风光大发）：`C < C̄ ⇒ adjust < 1 ⇒ Cr < C`，责任减免，激励用电；
高碳时段（火电高峰）：`C > C̄ ⇒ adjust > 1 ⇒ Cr > C`，责任加重，抑制用电。

守恒性：`mean(Cr) = C̄ + λ·Var(C)/C̄ ≥ C̄`，比值 `mean(Cr)/mean(C) = 1 + λ·Var(C)/C̄²`。
Cr 刻意不等于平均因子，它是激励信号而非核算因子。

### 4.3 分时电费计算器

湖南分时电价（峰 1.0 / 平 0.7 / 谷 0.35 元/kWh），峰谷价差 0.65 元/kWh。
需量电费 = `max(实际最大需量, 申报需量) × 需量单价`。
支持绿电价格和占比。

## 五、数据层

### 合成种子数据（无网络可演示）

`SeedDataProvider` 生成 96 点合成场景：
- 工业负荷曲线（白天 80–120 MW，夜间 40–60 MW）
- 湖南峰谷电价 + TariffPeriod 映射
- 发电结构（煤 62% + 水 25% + 风 6% + 购电 6%，白天叠加光伏）
- 储能 10 MWh / 5 MW，爬坡 2 MW/步，温度上限 45°C

### 湖南数据库下载（`HunanDataFetcher`）

连接 EData PostgreSQL（`pg-dev.db.tsingroc.tech`），下载湖南发电结构和实时出清价。
网络不可达时优雅降级返回空列表，provider 自动回退合成种子数据。

## 六、演示案例

### 案例 1：储能经济调度（min_cost → approve）

```
运行命令：python cases/case1_storage_dispatch/run_case1.py
```

| 指标 | 结果 |
|---|---|
| 最终状态 | approved |
| 求解器 | Optimal |
| 电费节省 | 6,333 元/日 |
| 峰值削减 | 467 kW |
| 最高电芯温度 | 33.0°C |
| 末端 SOC | 0.500 |
| 直接碳减排 | -1,021.5 kg（min_cost 模式碳排增加） |
| 责任碳减排 | -1,092.1 kg |

> 碳减排为负说明：min_cost 目标下储能谷充峰放，把负荷从低碳时段（白天光伏）
> 转移到高碳时段（夜间煤电），导致碳排增加。这正是引入 min_carbon 目标和 Cr 的动机。

### 案例 2：电碳感知调度（min_carbon → revise → re-optimize → approve）

```
运行命令：python cases/case2_carbon_aware/run_case2.py
```

| 指标 | V1 → revise | V2（最终） |
|---|---|---|
| 最终状态 | pending_approval | approved |
| 方案版本 | V1 → revise | V2 |
| 电费节省 | — | -251 元/日（为减碳多花） |
| 直接碳减排 C(τ) | — | 144.8 kg |
| 责任碳减排 Cr(τ) | — | 367.6 kg |
| 备用 SOC | 0.1 → revise 0.3 | 0.3 |

> revise 回路演示：工程师要求收紧备用 SOC 到 0.3，系统重新优化出 V2。
> Cr 减排（367.6 kg）显著大于 C 减排（144.8 kg），体现了动态责任因子的激励放大效应。

## 七、运行方式

```powershell
# 环境变量
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = "...\v2_energy_agent\src"
$python = "...\energy_agent\.venv\Scripts\python.exe"

# 运行全部测试
& $python -m pytest tests/ -v

# 运行案例 1
& $python cases/case1_storage_dispatch/run_case1.py

# 运行案例 2
& $python cases/case2_carbon_aware/run_case2.py
```

## 八、验收清单

| 验收项 | 状态 |
|---|---|
| LangGraph 节点编排（11 节点） | ✓ |
| 条件边和审批回路（approve/reject/revise） | ✓ |
| MILP 储能优化（爬坡/SOC/温度约束） | ✓ 6 测试 |
| 电碳双因子 C(τ) + Cr(τ) | ✓ 6 测试 |
| 分时电费 + 需量电费 | ✓ 20 测试 |
| 案例 1 端到端跑通 | ✓ approved |
| 案例 2 revise 回路跑通 | ✓ approved V2 |
| 单元测试总数 | ✓ 51 passed |

## 九、与 V1 的对比

| 维度 | V1 | V2 |
|---|---|---|
| 储能优化 | 规则法（分位数阈值） | MILP（PuLP+CBC） |
| 爬坡约束 | 无 | `max_ramp_kw_per_step` |
| 温度安全 | 无 | 一阶热模型 + `max_cell_temp` |
| 碳计量 | 单一 `carbon_factor_kg_per_kwh` | 双因子 C(τ) + Cr(τ) |
| 电费 | 内嵌 price 序列 | TariffSchedule（峰谷+需量+绿电） |
| 数据来源 | JSON demo | 合成种子 + EData 数据库 |
| 目标函数 | min_cost / limit_peak | + min_carbon |
