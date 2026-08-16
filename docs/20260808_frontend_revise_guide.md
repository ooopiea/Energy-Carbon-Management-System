# 黄花园区能源管理系统 — 前端界面与需量控制改造指南

版本日期：2026-08-08
适用项目：`energy_management_v3/v3_energy_management`
文档状态：**改造执行依据（并行 worktree 分支 `codex/parallel-work`）**
文档用途：本次改造的唯一工程规范，覆盖左侧导航重构、综合可视化调整、需量限制智能削峰、负荷报告可视化、储能SOC日界约束五大目标。

本文档与 `20260803revise_guide.md` 互补：前者聚焦 LangGraph 忠实化重构，本文档聚焦用户界面与调度策略层的增强。

## 1. 改造总览

### 1.1 需求清单

| 编号 | 需求 | 涉及层 | 优先级 |
| --- | --- | --- | --- |
| R1 | 左侧导航栏重新设计（分组 + 下拉子项 + 待接入占位） | 前端 | MUST |
| R2 | 综合可视化删除"自然语言工程师"模块 | 前端 | MUST |
| R3 | 园区控制策略新增"需量限制"参数，实现当月最大需量约束 | 前端 + 后端 | MUST |
| R4 | 负荷处理报告加入可视化图表 | 前端 | MUST |
| R5 | 储能每日起始/结束 SOC 默认 10%，自然语言可覆盖当日 SOC | 后端 | MUST |

### 1.2 并行开发约定

本次改造在独立 worktree 中进行：

```
主 worktree:    v3_energy_management       分支 Reorganize        （另一个 AI 在此工作）
并行 worktree:  v3_em_worktree              分支 codex/parallel-work（本文档所在）
```

两个 worktree 从同一 commit `4d494bf` 拉出。开发阶段各自独立，合并时统一处理冲突。

**已知冲突文件预测：**

| 文件 | 冲突风险 | 缓解策略 |
| --- | --- | --- |
| `frontend/src/components/Layout.tsx` | 高 | 本次导航重构改动集中且独立 |
| `frontend/src/panels/OverviewPanel.tsx` | 高 | 删除 NaturalLanguagePrompt 是减法操作，冲突面积小 |
| `backend/src/agents/engine.py` | 高 | 需量逻辑改动集中在 `_resolve_setpoints` 区域 |
| `backend/src/workflows/realtime.py` | 高 | 同步需量逻辑 |
| `frontend/src/types.ts` | 中 | 仅新增字段，不修改已有字段 |
| `frontend/src/pages/ReportPage.tsx` | 低 | 对方未修改此文件 |

### 1.3 执行级别

沿用 `20260803revise_guide.md` 的 MUST / SHOULD / MAY 约定。本文档中标注 **必须(MUST)** 的条目不满足即不得合并。

---

## 2. 左侧导航重构（R1）

### 2.1 目标导航结构

```
综合可视化
AI厂务协同
AI协作流程
不可调负荷              -> "系统待接入"
可调负荷
  |- 暖通空调           -> 现有 HVAC 页面
  |- 空压机             -> "系统待接入"
储能设备
  |- 电池储能           -> 现有储能页面
  |- 蓄冷               -> "系统待接入"
```

"系统待接入"页面仅显示居中小字注释，不预留卡片框架或灰色占位区域。

### 2.2 数据结构设计

当前导航是扁平数组：

```typescript
// Layout.tsx 现状
const NAV_ITEMS = [
  { id: 'overview', label: '综合可视化', icon: LayoutDashboard },
  { id: 'facility_chat', label: '厂务协同', icon: MessageSquareText },
  { id: 'agent_flow', label: 'Agent流程', icon: Workflow },
  { id: 'storage', label: '储能系统', icon: BatteryCharging },
  { id: 'hvac', label: 'HVAC系统', icon: Wind },
]
```

改造为支持分组的联合类型：

```typescript
type NavLeaf = {
  kind: 'leaf'
  id: string
  label: string
  icon: LucideIcon
}

type NavGroup = {
  kind: 'group'
  id: string
  label: string
  icon: LucideIcon
  children: NavLeaf[]
}

type NavItem = NavLeaf | NavGroup
```

新 `NAV_ITEMS` 定义：

```typescript
const NAV_ITEMS: NavItem[] = [
  { kind: 'leaf', id: 'overview',       label: '综合可视化', icon: LayoutDashboard },
  { kind: 'leaf', id: 'facility_chat',  label: 'AI厂务协同', icon: MessageSquareText },
  { kind: 'leaf', id: 'agent_flow',     label: 'AI协作流程', icon: Workflow },
  { kind: 'leaf', id: 'fixed_load',     label: '不可调负荷', icon: Lock },
  { kind: 'group', id: 'adjustable',    label: '可调负荷',   icon: SlidersHorizontal, children: [
    { kind: 'leaf', id: 'hvac',         label: '暖通空调',   icon: Wind },
    { kind: 'leaf', id: 'compressor',   label: '空压机',     icon: Fan },
  ]},
  { kind: 'group', id: 'storage_group', label: '储能设备',   icon: BatteryCharging, children: [
    { kind: 'leaf', id: 'storage',      label: '电池储能',   icon: Battery },
    { kind: 'leaf', id: 'ice_storage',  label: '蓄冷',       icon: Snowflake },
  ]},
]
```

分组展开/收起状态通过组件内 `useState` 管理，默认可调负荷和储能设备都展开。

### 2.3 "系统待接入"占位页面

新建 `frontend/src/pages/SystemPendingPage.tsx`：

```typescript
interface Props {
  title: string
  description?: string
}

export function SystemPendingPage({ title, description = '系统待接入' }: Props) {
  return (
    <div className="page-state" role="status">
      <h1>{title}</h1>
      <p className="empty-copy">{description}</p>
    </div>
  )
}
```

**必须(MUST)**：占位页面只包含标题和"系统待接入"小字，不渲染任何卡片、图表、KPI 或灰色框架。

### 2.4 路由注册

`App.tsx` 的 `renderPage` switch 新增三个 case：

```typescript
case 'fixed_load':   return <SystemPendingPage title="不可调负荷" />
case 'compressor':   return <SystemPendingPage title="空压机" />
case 'ice_storage':  return <SystemPendingPage title="蓄冷" />
```

右侧详情面板的 `renderRightPanel` switch 中，这三个页面走 `default` 分支返回 `<OverviewPanel />`。由于占位页面无交互内容，右侧面板默认收起即可。

### 2.5 涉及文件清单

| 文件 | 改动类型 |
| --- | --- |
| `frontend/src/components/Layout.tsx` | 重写 `NAV_ITEMS` + 导航渲染逻辑 |
| `frontend/src/pages/SystemPendingPage.tsx` | 新建 |
| `frontend/src/App.tsx` | 新增 import + switch case |

---

## 3. 综合可视化调整（R2 + R3 前端）

### 3.1 删除"自然语言工程师"模块（R2）

在 `frontend/src/panels/OverviewPanel.tsx` 中：

1. 移除 `import { NaturalLanguagePrompt } from '../components/NaturalLanguagePrompt'`
2. 移除 JSX 中的 `<NaturalLanguagePrompt placeholder="..." />` 行
3. 清理因此变为未使用的 lucide icon import

**必须(MUST)**：移除后 `OverviewPanel` 顶部第一个卡片变为"日前负荷审批报告"。NaturalLanguagePrompt 组件文件本身不删除（FacilityChat 页面可能仍在使用），仅从 OverviewPanel 中摘除引用。

### 3.2 需量限制参数化（R3 前端）

当前"园区控制策略"卡片位于 `OverviewPanel.tsx`，已有 `cap` 输入和策略选择下拉。改造点：

**改动 1：展示当月已发生最大需量**

在需量上限输入框下方新增一行只读展示：

```
当月最大需量：XX,XXX kW  |  剩余余量：XX,XXX kW
```

数据来源：`state.active_strategy.monthly_peak_kw`（后端新增字段，见第 4 节）。

```typescript
const monthlyPeak = state.active_strategy?.monthly_peak_kw ?? 0
const capValue = Number(cap)
const remaining = capValue > 0 ? Math.max(0, capValue - monthlyPeak) : null
```

**改动 2：标签调整**

- "需量上限（kW）" 更名为"需量限制（kW）"
- 确认按钮文案保持"确认应用"
- 策略下拉中的"需量抑制"选项保留

**改动 3：控制策略卡片整体布局**

```
+-- 园区控制策略 -----------------------+
| 优化目标    [电费最优]                |
|                                      |
| 需量限制    [ 90000 ] kW             |
| 当月峰值    78,234 kW | 余量 11,766  |
|                                      |
| [确认应用]  [撤销]                   |
+--------------------------------------+
```

### 3.3 涉及文件清单

| 文件 | 改动类型 |
| --- | --- |
| `frontend/src/panels/OverviewPanel.tsx` | 删除 NaturalLanguagePrompt + 增强需量限制展示 |
| `frontend/src/types.ts` | `active_strategy` 接口新增 `monthly_peak_kw` 字段 |

---

## 4. 需量限制智能削峰（R3 后端）

### 4.1 问题定义

当前需量限制逻辑是**逐步硬截断**：每个 tick 只要电网功率超过 `_demand_cap_kw`，就立即增加储能放电。

```python
# engine.py 现状 (约 line 910)
if self._demand_cap_kw is not None:
    planned_grid = load - solar - storage_setpoint - (baseline_hvac - hvac_setpoint)
    storage_setpoint += max(0.0, planned_grid - self._demand_cap_kw)
    storage_setpoint = min(float(STORAGE_DEFAULTS["max_discharge_power_kw"]), storage_setpoint)
```

实际电力计费中的"基本电费"按**自然月内最大需量**（15 分钟平均功率最大值）计收。如果当月已经出现过 80 MW 的峰值，后续即使跑到 85 MW 也只会把计费基数抬高到 85 MW；但如果当月还没超过 80 MW，当前步到了 85 MW 才需要干预。

### 4.2 目标逻辑：智能削峰

用户确认采用智能削峰模式。核心规则：

> **仅当当前步的预测电网功率既超过需量限制设定值，又会推高当月峰值时，才启动储能放电进行削峰。**

数学表达：

```
设 demand_cap_kw 为用户设定的当月需量限制（kW）
设 monthly_peak_kw 为当月已记录的最大需量（kW）
设 predicted_grid_kw 为当前步预测电网功率（kW）

if predicted_grid_kw > demand_cap_kw AND predicted_grid_kw > monthly_peak_kw:
    discharge_extra = predicted_grid_kw - demand_cap_kw
    storage_setpoint += discharge_extra
    storage_setpoint = min(storage_setpoint, max_discharge_power_kw)
```

两个条件缺一不可：

- `predicted_grid_kw > demand_cap_kw`：当前步确实超限
- `predicted_grid_kw > monthly_peak_kw`：当前步会刷新当月峰值。如果不会刷新，说明当月已经出现过更高的峰值，当前步不影响计费基数，无需浪费储能循环

**完整验收用例：**

| demand_cap | monthly_peak | planned_grid | 是否干预 | 原因 |
| --- | --- | --- | --- | --- |
| 50000 | 30000 | 45000 | 否 | 未超限（45000 < 50000） |
| 50000 | 30000 | 55000 | 是 | 超限且刷新峰值 |
| 50000 | 60000 | 55000 | 否 | 55000 < 60000，不刷新峰值，浪费储能无意义 |
| 50000 | 60000 | 65000 | 是 | 超限且刷新峰值（65000 > 60000） |
| null | - | - | 否 | 未设定需量限制 |

### 4.3 engine.py 改动

**改动 1：新增月度峰值跟踪变量**

在 `__init__` 中（约 line 168 附近）：

```python
self._demand_cap_kw: float | None = None
self._monthly_peak_kw: float = 0.0           # 新增：当月已记录的最大需量
self._monthly_peak_month: int | None = None  # 新增：跟踪当前月份，跨月时重置
```

**改动 2：每 tick 更新月度峰值**

在 `_update_realtime_locked` 中计算完 `grid` 之后：

```python
# 更新当月最大需量
current_month = self._time.sim_time.month
if self._monthly_peak_month != current_month:
    self._monthly_peak_kw = 0.0
    self._monthly_peak_month = current_month
if grid > self._monthly_peak_kw:
    self._monthly_peak_kw = grid
```

**改动 3：改写需量限制逻辑**

替换现有 `_resolve_setpoints` 中的需量段（约 line 910）：

```python
if self._demand_cap_kw is not None:
    planned_grid = load - solar - storage_setpoint - (baseline_hvac - hvac_setpoint)
    # 智能削峰：仅在当前步会推高当月峰值且超出设定值时干预
    if planned_grid > self._demand_cap_kw and planned_grid > self._monthly_peak_kw:
        storage_setpoint += planned_grid - self._demand_cap_kw
        storage_setpoint = min(
            float(STORAGE_DEFAULTS["max_discharge_power_kw"]),
            storage_setpoint,
        )
```

**改动 4：状态序列化**

在 `get_state()` 的 `active_strategy` 字段中新增：

```python
"active_strategy": {
    "demand_cap_kw": self._demand_cap_kw,
    "enabled": self._demand_cap_kw is not None,
    "monthly_peak_kw": self._monthly_peak_kw,   # 新增
},
```

### 4.4 realtime.py 同步改动

`RealtimeTickEngine._resolve_setpoints` 中的需量逻辑需要同步修改。由于 `RealtimeTickEngine` 是无状态的，月度峰值需要通过 `TickInput` 传入。

**改动 1：`TickInput` 新增字段**

在 `workflows/phase.py` 的 `TickInput` 中：

```python
@dataclass
class TickInput:
    # ... 现有字段 ...
    monthly_peak_kw: float = 0.0  # 新增
```

**改动 2：`_resolve_setpoints` 改写**

```python
if inp.demand_cap_kw is not None:
    planned_grid = load - solar - storage_sp - (baseline_hvac - hvac_sp)
    if planned_grid > inp.demand_cap_kw and planned_grid > inp.monthly_peak_kw:
        storage_sp += planned_grid - inp.demand_cap_kw
        storage_sp = min(float(STORAGE_DEFAULTS["max_discharge_power_kw"]), storage_sp)
```

**改动 3：engine.py 调用处传参**

在 `engine.py` 构建 `TickInput` 的位置传入 `monthly_peak_kw=self._monthly_peak_kw`。

### 4.5 涉及文件清单

| 文件 | 改动类型 |
| --- | --- |
| `backend/src/agents/engine.py` | 新增月度峰值跟踪 + 改写需量逻辑 + 状态序列化 |
| `backend/src/workflows/phase.py` | `TickInput` 新增 `monthly_peak_kw` 字段 |
| `backend/src/workflows/realtime.py` | `_resolve_setpoints` 同步改写 |
| `frontend/src/types.ts` | `active_strategy` 接口新增 `monthly_peak_kw` |

---

## 5. 负荷报告可视化（R4）

### 5.1 现状

`ReportPage.tsx` 当前渲染三种报告（forecast / storage / hvac），但只有纯表格（`ReportTable` 组件）。需要为每种报告的 `plan_table` 数据增加 ECharts 图表。

### 5.2 forecast 报告可视化

plan_table 包含字段：`time`, `shift`, `production_intensity`, `forecast_kw`

新增折线图：X 轴为时间，Y 轴为负荷预测值（kW）。

```typescript
function ForecastChart({ rows }: { rows: Array<Record<string, unknown>> }) {
  const times = rows.map(r => String(r.time))
  const values = rows.map(r => Number(r.forecast_kw))
  return <ReactECharts option={{
    tooltip: { trigger: 'axis' },
    grid: { left: 58, right: 18, top: 30, bottom: 30 },
    xAxis: { type: 'category', data: times, axisLabel: { fontSize: 9, interval: 11 } },
    yAxis: { type: 'value', name: 'kW', axisLabel: { fontSize: 9 } },
    series: [{
      name: '负荷预测', type: 'line', smooth: true, symbol: 'none',
      data: values,
      lineStyle: { width: 2, color: '#176b87' },
      areaStyle: { color: 'rgba(23,107,135,.08)' },
    }],
  }} style={{ height: 240 }} />
}
```

### 5.3 storage 报告可视化

plan_table 包含字段：`time`, `power_kw`, `soc_ratio`, `temperature_c`, `grid_kw`

新增双 Y 轴折线图：左轴为功率（kW），右轴为 SOC（百分比）。

```typescript
// 储能功率（折线）+ SOC（折线，右轴）
series: [
  { name: '储能功率', type: 'line', smooth: true, data: powerValues,
    yAxisIndex: 0, lineStyle: { width: 2, color: '#d97706' } },
  { name: 'SOC', type: 'line', smooth: true, data: socValues,
    yAxisIndex: 1, lineStyle: { width: 2, color: '#15803d' },
    areaStyle: { color: 'rgba(21,128,61,.06)' } },
]
```

### 5.4 hvac 报告可视化

plan_table 包含字段：`time`, `power_kw`, `active_chillers`, `cop`, `supply_temp_c`, `return_temp_c`

新增组合图表：

- HVAC 功率：折线图（左轴，kW）
- COP：折线图（右轴）
- 冷机台数：柱状图（左轴，台数）

```typescript
series: [
  { name: 'HVAC功率', type: 'line', smooth: true, yAxisIndex: 0,
    data: powerValues, lineStyle: { width: 2, color: '#1688a7' } },
  { name: 'COP', type: 'line', smooth: true, yAxisIndex: 1,
    data: copValues, lineStyle: { width: 2, color: '#6d5b8c' } },
  { name: '冷机台数', type: 'bar', yAxisIndex: 0,
    data: chillerValues, itemStyle: { color: 'rgba(22,136,167,.2)' } },
]
```

**必须(MUST)**：在 hvac 报告图表区域底部添加一行注释说明：

> 该图表基于日前调度计划生成，未来将接入现有调度系统以实现实时更新与闭环反馈。

### 5.5 图表插入位置

在 `ReportPage.tsx` 中，当前 `detail` 块的结构为：

```
key_metrics（指标卡片）
-> schedule_table（排班表格）
-> plan_table（调度方案明细表格）
-> risks（风险）
```

图表插入在 `plan_table` 表格**之前**（先看图再看表）：

```
key_metrics
-> schedule_table
-> [新增] plan_table 对应的可视化图表
-> plan_table（调度方案明细表格）
-> risks
```

通过 `detail.kind` 判断渲染哪种图表组件：

```typescript
{detail.kind === 'forecast' && <ForecastChart rows={detail.plan_table} />}
{detail.kind === 'storage'  && <StorageChart rows={detail.plan_table} />}
{detail.kind === 'hvac'     && <HvacChart rows={detail.plan_table} />}
<ReportTable title="调度方案明细" ... />
```

### 5.6 涉及文件清单

| 文件 | 改动类型 |
| --- | --- |
| `frontend/src/pages/ReportPage.tsx` | 新增三个图表子组件 + 插入到报告渲染流程 |

---


---

## 5.5 储能SOC日界约束（R5）

### 5.5.1 需求定义

储能系统每日调度计划的起始 SOC 和结束 SOC 均默认保持在 **10%**（即 `min_soc_ratio`）。工程师可通过自然语言交互覆盖当日的 SOC 目标值。

设计意图：确保储能每日从最低安全水位出发、回到最低安全水位，避免长期满充导致的电池衰减；同时保留灵活性，让工程师在特殊运行日（如检修计划、需量响应事件）临时调整 SOC 目标。

### 5.5.2 现状分析

**SOC 参数当前流转路径：**

```
engine.py::_graph_storage_agent (line 509)
  -> initial_soc = self._current_values.get("storage_soc", 0.5)  // 取实时SOC，默认0.5
  -> terminal_soc 未显式传入，optimize_storage_dispatch 内部默认 terminal_soc = initial_soc

engine.py::_preview_storage_optimization (line 1473)
  -> initial_soc = self._current_values.get("storage_soc", 0.5)
  -> terminal_soc 仅当 params 中存在时才传入

engine.py::_apply_day_ahead_revision (line 1580)
  -> 与 _preview 相同逻辑
```

**自然语言修改路径已部分存在：**

`chat_service.py` 的 `propose_day_ahead_params` 工具已支持 `terminal_soc` 参数（schema 范围 0.1-0.9），但**不支持 `initial_soc`**。engine.py 收到 `terminal_soc` 后通过 `_apply_day_ahead_revision` 路径重跑优化器。

**需要补充的能力：**

1. 默认 initial_soc 和 terminal_soc 均为 0.10
2. 自然语言可同时覆盖 initial_soc 和 terminal_soc
3. 每日起新一天时自动重置为默认值

### 5.5.3 engine.py 改动

**改动 1：新增 SOC 日界约束常量**

在 `engine.py` 顶部或 `__init__` 中：

```python
# 储能每日默认SOC约束
STORAGE_DEFAULT_INITIAL_SOC = 0.10
STORAGE_DEFAULT_TERMINAL_SOC = 0.10
```

**改动 2：新增当日SOC覆盖变量**

在 `__init__` 中（约 line 168 附近，与其他状态变量并列）：

```python
self._daily_soc_override: dict[str, float | None] | None = None
# 格式: {"initial_soc": 0.3, "terminal_soc": 0.2} 或 None（表示用默认值）
# 每日开始时重置为 None
```

**改动 3：每日重置 SOC 覆盖**

在 `start_day()` 方法中（或每日切换的入口处）加入：

```python
self._daily_soc_override = None  # 新的一天清除昨日的SOC覆盖
```

**改动 4：改写 `_graph_storage_agent` 中的 SOC 传参**

当前代码（line 509-515）：

```python
self._storage_plan = optimize_storage_dispatch(
    ...
    initial_soc=float(self._current_values.get("storage_soc", 0.5)),
    ...
)
```

改为：

```python
# 优先使用自然语言覆盖值，否则使用默认日界约束
soc_override = self._daily_soc_override or {}
initial_soc = float(soc_override.get("initial_soc", STORAGE_DEFAULT_INITIAL_SOC))
terminal_soc = float(soc_override.get("terminal_soc", STORAGE_DEFAULT_TERMINAL_SOC))

self._storage_plan = optimize_storage_dispatch(
    ...
    initial_soc=initial_soc,
    terminal_soc=terminal_soc,
    ...
)
```

**改动 5：改写 `_preview_storage_optimization` 和 `_apply_day_ahead_revision`**

同样的模式：优先读取 `self._daily_soc_override`，回退到默认常量。

```python
soc_override = self._daily_soc_override or {}
# params 中的 terminal_soc 优先级最高（来自自然语言实时修改）
initial_soc = float(params.get("initial_soc", soc_override.get("initial_soc", STORAGE_DEFAULT_INITIAL_SOC)))
terminal_soc = float(params.get("terminal_soc", soc_override.get("terminal_soc", STORAGE_DEFAULT_TERMINAL_SOC)))
```

**改动 6：状态序列化**

在 `get_state()` 中暴露当前 SOC 约束信息（供前端展示和调试）：

```python
"storage_soc_config": {
    "default_initial_soc": STORAGE_DEFAULT_INITIAL_SOC,
    "default_terminal_soc": STORAGE_DEFAULT_TERMINAL_SOC,
    "daily_override": self._daily_soc_override,
},
```

### 5.5.4 自然语言修改 SOC 路径

现有的 `propose_day_ahead_params` 工具只支持 `terminal_soc`。需要扩展为同时支持 `initial_soc`：

**chat_service.py schema 扩展：**

```python
# 在 propose_day_ahead_params 的 parameters schema 中
"initial_soc": {
    "type": "number",
    "minimum": 0.1,
    "maximum": 0.9,
    "description": "当日储能起始SOC比例，默认0.10"
},
"terminal_soc": {
    "type": "number",
    "minimum": 0.1,
    "maximum": 0.9,
    "description": "当日储能结束SOC比例，默认0.10"
},
```

**engine.py 接收覆盖：**

当 `propose_day_ahead_params` 携带 `initial_soc` 或 `terminal_soc` 时，engine 更新 `self._daily_soc_override`：

```python
if "initial_soc" in params or "terminal_soc" in params:
    override = dict(self._daily_soc_override or {})
    if "initial_soc" in params:
        override["initial_soc"] = float(params["initial_soc"])
    if "terminal_soc" in params:
        override["terminal_soc"] = float(params["terminal_soc"])
    self._daily_soc_override = override
```

**自然语言交互示例：**

```
工程师: "今天储能起始SOC改为30%，结束SOC保持20%"
GLM 解析 -> propose_day_ahead_params(target_system="storage",
           initial_soc=0.30, terminal_soc=0.20)
Engine 更新 _daily_soc_override = {"initial_soc": 0.30, "terminal_soc": 0.20}
重新跑日前优化 -> 生成新的 storage_plan
```

### 5.5.5 config.py 约束检查

当前 `STORAGE_DEFAULTS` 中 `min_soc_ratio = 0.10`。SOC 默认值恰好等于下限，这是合理的——意味着储能每天从最低安全水位开始调度。但 MILP 求解器需要 SOC 变量范围 `[min_soc, max_soc]`，当 `initial_soc == min_soc` 时第一个步的约束变为 `soc[0] = min_soc`，这不会导致不可行（因为只能充不能放），但需确认 CBC solver 能正常求解。

**必须(MUST)**：验证 `initial_soc = terminal_soc = 0.10` 时 MILP 求解状态为 `Optimal`，`violations` 列表为空。

### 5.5.6 涉及文件清单

| 文件 | 改动类型 |
| --- | --- |
| `backend/src/agents/engine.py` | 新增 SOC 日界常量 + 覆盖变量 + 改写传参 + 状态序列化 |
| `backend/src/agents/chat_service.py` | `propose_day_ahead_params` schema 扩展 `initial_soc` |

---

## 6. types.ts 接口变更汇总

```typescript
// active_strategy 接口变更
export interface RuntimeState {
  // ... 现有字段不变 ...

  active_strategy: {
    demand_cap_kw: number | null
    enabled: boolean
    monthly_peak_kw: number  // 新增：当月已记录的最大需量（kW）
  }
}
```

**必须(MUST)**：前端读取时应做 fallback，保证后端未更新时前端不崩溃：

```typescript
const monthlyPeak = state.active_strategy?.monthly_peak_kw ?? 0
```

---

## 7. 完整改动文件清单

| # | 文件 | 模块 | 改动类型 | 冲突风险 |
| --- | --- | --- | --- | --- |
| 1 | `frontend/src/components/Layout.tsx` | R1 | 重写导航 | 高 |
| 2 | `frontend/src/pages/SystemPendingPage.tsx` | R1 | 新建 | 无 |
| 3 | `frontend/src/App.tsx` | R1 | 新增路由 | 低 |
| 4 | `frontend/src/panels/OverviewPanel.tsx` | R2+R3 | 删除模块+增强展示 | 高 |
| 5 | `backend/src/agents/engine.py` | R3 | 需量逻辑改写 | 高 |
| 6 | `backend/src/workflows/phase.py` | R3 | TickInput 新增字段 | 低 |
| 7 | `backend/src/workflows/realtime.py` | R3 | 同步需量逻辑 | 高 |
| 8 | `frontend/src/types.ts` | R3 | 新增字段 | 中 |
| 9 | `frontend/src/pages/ReportPage.tsx` | R4 | 新增图表组件 | 低 |

共 10 个文件，其中新建 1 个、修改 9 个。engine.py 同时被 R3 和 R5 修改，实际只动一次。

---

## 8. 验证计划

### 8.1 前端验证

1. 启动 dev server，检查左侧导航结构与目标一致
2. 点击"不可调负荷""空压机""蓄冷"，确认显示"系统待接入"
3. 点击"暖通空调""电池储能"，确认跳转到现有页面
4. 综合可视化页面确认无"自然语言工程师"输入框
5. 园区控制策略确认显示"当月最大需量"和"剩余余量"
6. 打开任一报告详情页，确认图表正确渲染

### 8.2 后端验证

1. 设置 demand_cap_kw = 50000，观察 monthly_peak_kw 随 tick 增长
2. 当 monthly_peak_kw 达到 45000 时，设置 demand_cap = 40000
3. 观察后续 tick 中 planned_grid < monthly_peak_kw 时不触发储能干预
4. 观察跨月时 monthly_peak_kw 正确重置为 0
5. 确认每日 storage_plan 的 initial_soc 和 terminal_soc 均为 0.10
6. 通过自然语言修改当日 SOC（如 initial=0.3, terminal=0.2），确认重跑优化生效
7. 确认跨日时 _daily_soc_override 自动清除，回到默认 10%

### 8.3 合并验证

1. 将 `codex/parallel-work` 分支 merge 回 `Reorganize`
2. 手动解决 `Layout.tsx`、`OverviewPanel.tsx`、`engine.py`、`realtime.py` 的冲突
3. 冲突解决原则：保留双方功能性改动，导航结构以本文档为准，需量逻辑以本文档为准
4. 全量回归测试：前后端 dev server 联合运行

---

## 9. 实施顺序

```
Phase 1 - 前端导航（R1）
  |- SystemPendingPage.tsx 新建
  |- Layout.tsx 导航重写
  +- App.tsx 路由注册
  -> 验证：导航结构正确，页面切换正常

Phase 2 - 综合可视化（R2）
  |- OverviewPanel 删除 NaturalLanguagePrompt
  +- 清理 import
  -> 验证：自然语言模块消失

Phase 3 - 需量限制后端（R3 后端）
  |- phase.py TickInput 新增字段
  |- engine.py 月度峰值跟踪 + 智能削峰逻辑 + 状态序列化
  |- realtime.py 同步逻辑
  +- types.ts 接口更新
  -> 验证：后端逻辑正确，状态可序列化

Phase 4 - 需量限制前端（R3 前端）
  +- OverviewPanel 需量展示增强
  -> 验证：前端正确显示当月峰值和余量

Phase 5 - 报告可视化（R4）
  |- ForecastChart 组件
  |- StorageChart 组件
  |- HvacChart 组件
  +- ReportPage 集成
  -> 验证：三种报告图表正确渲染

Phase 6 - 联合验证
  +- 前后端联合 dev server 测试
```

每个 Phase 完成后进行局部验证，确保增量可观测。
