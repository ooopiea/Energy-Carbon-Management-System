# 场景对比功能 Spec（A/B/C 三场景调度）

> 本文档固化 2026-07-27 grill 会话的全部决策，作为后续代码修改的依据。
> 任何偏离需重新确认，不得擅自修改。

## 1. 背景与目标

在现有 V2 能源调度系统（LangGraph 编排 + MILP 储能优化 + 电碳双因子）基础上，
新增三个对比场景，用于研究"不同电价机制 × 不同储能规模"对园区经济性与碳排的影响：

| 场景 | 电价机制 | 储能规模 | 定位 |
|------|----------|----------|------|
| A | 分时电价 TOU（黄花目录价） | 10 MWh / 5 MW（现状） | 现状基线 |
| B | 分时电价 TOU | 40 MWh / 20 MW（扩大） | 扩容后 TOU 收益 |
| C | 实时电价 RTP（湖北出清价） | 40 MWh / 20 MW（扩大） | 实时电价下储能表现 |

用户在前端切换场景查看各自结果，不并排对比。

## 2. 决策记录

### D1 实时电价口径（方案①）
- 场景 C 的电量电价 = 湖北出清价（批发侧，作为分时信号）
- 三场景共享统一的附加费口径：政府基金（0.04625 元/kWh）、力调（-0.75%）、
  绿电环境价值、需量电费（30.6 元/kW/月）
- 三场景唯一的差异变量：电量电价形成机制（TOU 目录价 vs RTP 市场价）+ 储能规模

**已知 gap（诚实记录）**：湖北出清价为批发侧价格，不含输配电价；A/B 的 TOU 目录价
为到户含税价（含输配电价）。严格"统一到户口径"需在 RTP 电度价上叠加输配电价，
但项目目前无黄花输配电价数值依据。本期实施暂用现状口径（出清价直接作电度价），
附加费统一。若后续取得输配电价数值，需在 RTP 模式电度价中补加。

### D2 储能扩容（默认 40 MWh / 20 MW）
- 默认容量 40 MWh，功率 20 MW（0.5C 倍率，2 小时满充满放）
- 与现状 10 MWh / 5 MW 保持同倍率，锁定单一变量为"储能规模"
- 前端可自定义储能容量，改容量时功率按 0.5C 自动联动
- 投资成本不纳入本期范围（只比运行日电费），留作 future work

### D3/D4 实现路径（方案②：走完整 LangGraph 审批流）
- 三场景都复用现有 /api/runs 完整"优化→审批→冻结"流程
- 不新建独立对比 runner，结果按场景单独展示
- 必须修复的 bug：provider.load_dispatch_inputs() 在挂了 CSV fetcher 后
  会强制用 RTP 覆盖 TOU，导致场景 A/B 无法触发。新增 price_mode 参数解决。

### D5 对比产出（可切换，不并排）
- 前端场景选择器，选中后展示该场景完整结果（电费/储能/碳排/审批事件链）
- 不做三场景并排对比图

### D6 数据源正当性（方案②：代码注释补充）
- 在 csv_fetcher.py 补充湖北电价替代理由的指向性说明
- 不单独建数据来源文档

### D7 推送范围（只推 v2_energy_agent/）
- 仓库只包含 v2_energy_agent/（代码 + 文档 + 配置）
- 电网数据 CSV（hunan_core/、hubei_rt_clearing/）不进仓库，本地保留
- 运行产物（plan_archive/、distillation/、cases/*_result.json、outputs/）不进仓库
- 根目录建 .gitignore 双保险

### D8 测试（保持现状）
- 已知 1 failed（test_account_dispatch 版本标签）+ 1 error（沙箱权限）
- 本次不修复，保持现状推送

## 3. 场景映射模型

两个独立维度：price_mode（tou/rtp）× battery（容量+功率）。
A/B/C 是二维空间的三个预设点：

- A: price_mode=tou, capacity=10, power=5   (现状)
- B: price_mode=tou, capacity=40, power=20  (扩大)
- C: price_mode=rtp, capacity=40, power=20  (实时+扩大)

- 储能字段选中预设后保持可编辑
- A 不锁死（用户可改），但默认值是现状 10 MWh
- 容量改动时功率按 0.5C 自动联动
- RTP + 小储能（非预设组合）不禁止也不设预设

## 4. 技术实现方案

### 4.1 provider 改造（核心）

SeedDataProvider.load_dispatch_inputs() 新增参数：

- price_mode="tou"：强制保留分时电价，即使 CSV 有出清价也不覆盖
- price_mode="rtp"：强制用 CSV 出清价（无数据时报错而非静默回退）
- price_mode="auto"：维持现状（有 CSV 就 RTP，无则 TOU）
- battery_override：覆盖默认 10 MWh 电池参数

### 4.2 state + API 透传链路

数据流：CreateRunRequest -> make_initial_state -> EnergyDispatchStateV2
-> load_inputs_node -> provider.load_dispatch_inputs

需要改动的文件：
- orchestration.py：EnergyDispatchStateV2 加 price_mode、battery_override 字段
- runner.py：make_initial_state 接收并写入 state
- orchestration.py：load_inputs_node 透传给 provider
- server.py：CreateRunRequest 加字段

### 4.3 前端（dashboard.html）

- 场景选择器（A/B/C 三个预设按钮）
- 储能容量输入框（默认按预设，可编辑）
- 功率自动联动（0.5C）
- 提交时把 price_mode + battery_override 一并发给 /api/runs

## 5. 验收标准

1. 选场景 A：结果中电价为分时 4 档阶梯（非 RTP 连续波动），储能 10 MWh
2. 选场景 B：电价同 A，储能 40 MWh，日电费节省应高于 A
3. 选场景 C：电价为湖北出清价（连续波动），储能 40 MWh
4. 改储能容量后功率自动按 0.5C 联动
5. 三场景都走完整审批流程，结果含完整 events 事件链
