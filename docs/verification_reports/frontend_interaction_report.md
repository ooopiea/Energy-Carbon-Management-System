# 前端交互验证报告：Agent 工作流页面

> 验证时间：2026-08-03
> 验证环境：后端 http://localhost:8000 / 前端 http://localhost:5173
> 验证页面：Agent 工作流（Agent 流程）
> 验证人：前端交互验证工程师（小O）

---

## 一、总体结论

Agent 工作流页面**整体渲染正常**，拓扑图、检查点面板、Agent 节点卡片三大核心区域均可正确显示。`/api/graph` 返回的拓扑结构在页面上得到完整映射。发现 1 个 P2 级状态映射缺陷和 2 个 P3 级数据/布局问题。

| 验证项 | 结果 |
|--------|------|
| 工作流拓扑图渲染 | ✅ 通过 |
| 检查点/恢复状态面板 | ✅ 通过 |
| dispatch_approvals 节点映射 | ⚠️ 有缺陷（P2） |
| Agent 节点卡片 | ✅ 通过 |
| /api/graph 与页面一致性 | ✅ 通过 |
| /api/state checkpoint 字段 | ✅ 通过 |

---

## 二、详细验证结果

### 2.1 工作流拓扑图渲染（✅ 通过）

通过 DOM 检查确认 SVG 拓扑图完整渲染：

- **SVG 元素**：找到 viewBox 为 `-30 0 860 900` 的拓扑 SVG，尺寸 811×720px
- **节点**：13 个 `<g>` 节点组，13 个 `<rect>` 矩形，13 个 `<text>` 标签，7 个 `<circle>` 状态指示点
- **连线**：25 条 `<line>` 连线
- **全部 13 个节点标签正确显示**：

| 节点 ID | 标签 | 类型 | 页面显示状态 | 期望状态 | 颜色 |
|---------|------|------|-------------|---------|------|
| start | 开始 | start | 已完成 | completed | 绿色边框 |
| data_agent | 数据 Agent | agent | 已完成 | completed | 绿色填充 |
| prediction_agent | 负荷处理 Agent | agent | 已完成 | completed | 绿色填充 |
| forecast_approval | 工程师审批 | approval | 等待审批 | pending_approval | 琥珀色边框 |
| storage_agent | 储能 Agent | agent | 未开始 | idle | 灰色填充 |
| hvac_agent | HVAC Agent | agent | 未开始 | idle | 灰色填充 |
| **dispatch_approvals** | **双审批汇合** | **approval** | **等待审批** | **idle（应为未开始）** | **琥珀色边框（应为灰色）** |
| physical_dispatch | 物理调度 | physical | 未开始 | idle | 灰色边框 |
| end | 结束 | end | 未开始 | idle | 灰色边框 |
| monitor_agent | 监察 Agent | agent | 已完成 | completed | 绿色填充 |
| database | 数据库 / 状态文件库 | database | 未开始 | idle | 灰色边框 |
| route | 路由 | route | 未开始 | idle | 灰色边框 |
| _fanout_day_ahead | 日前并行分发 | route | 未开始 | idle | 灰色边框 |

状态颜色映射：`#15803d`（绿色=已完成）、`#d97706`（琥珀=等待审批）、`#94a3b8`（灰色=未开始）。

### 2.2 检查点/恢复状态面板（✅ 通过）

"运行检查点与恢复状态"卡片正确显示，包含以下字段：

| 字段 | 显示值 |
|------|--------|
| 当前子图 | 日前规划 |
| 工作流状态 | awaiting_forecast_approval |
| 最后完成 Tick | - |
| 调度已激活 | 否 |
| 最后命令 | - |
| ACK 状态 | - |
| 检查点更新 | 17:32:25 |
| 可恢复 | ✅ 显示 |

面板仅在 `state.checkpoint.available === true` 时渲染，当前状态满足条件。

### 2.3 dispatch_approvals 节点状态映射（⚠️ P2 缺陷）

**问题描述**：`dispatch_approvals`（双审批汇合）节点在工作流尚未推进到该节点时（上游 `storage_approval` 和 `hvac_approval` 审批门均为 `idle` 状态），已经显示为"等待审批"（琥珀色），而非正确的"未开始"（灰色）。

**根因分析**（`AgentFlow.tsx` `getNodeStatus` 函数）：

```typescript
if (nodeId === 'dispatch_approvals') {
    const s = gates['storage_approval']?.status
    const h = gates['hvac_approval']?.status
    if (s === 'approved' && h === 'approved') return 'completed'
    if (s === 'rejected' || h === 'rejected') return 'failed'
    return 'pending_approval'  // ← 缺陷：未区分 idle 与 pending_approval
}
```

当两个上游审批门均为 `idle` 时，代码直接返回 `pending_approval`，未增加 `idle` 判断分支。

**建议修复**：

```typescript
if (nodeId === 'dispatch_approvals') {
    const s = gates['storage_approval']?.status
    const h = gates['hvac_approval']?.status
    if (s === 'approved' && h === 'approved') return 'completed'
    if (s === 'rejected' || h === 'rejected') return 'failed'
    // 上游尚未产出时为 idle；至少一个已产出待审批时才为 pending_approval
    if (s === 'idle' && h === 'idle') return 'idle'
    return 'pending_approval'
}
```

### 2.4 Agent 节点卡片（✅ 通过）

页面下方以网格布局渲染 6 个 Agent 节点卡片，数据来源为 `state.agent_nodes`：

| 节点 | 状态 | 耗时 | 摘要 |
|------|------|------|------|
| 数据采集 | 已完成 | 39913ms | 已采集 96 点数据 (autumn) |
| 数据封存 | 已完成 | 1462260ms | 数据报告 rpt-41a556d8fe 已封存 |
| 负荷处理 | 已完成 | 1438631ms | 负荷处理报告 rpt-5df462fa4c 待审批 |
| 储能调度 | 未开始 | - | 等待运行 |
| HVAC调度 | 未开始 | - | 等待运行 |
| 系统监察 | 已完成 | 22ms | 第70步监察正常 |

### 2.5 /api/graph 接口与页面一致性（✅ 通过）

| 对比项 | API 返回 | 页面渲染 | 一致性 |
|--------|---------|---------|--------|
| 节点数 | 13 | 13 | ✅ |
| 连线数 | 25 | 25 | ✅ |
| 节点标签 | 13 个 | 13 个 | ✅ 全部匹配 |
| dispatch_approvals 节点 | 存在（type: approval） | 存在 | ✅ |
| 节点坐标 | x/y 值 | SVG 定位 | ✅ |

### 2.6 /api/state checkpoint 字段（✅ 通过）

`/api/state` 返回的 `checkpoint` 对象结构完整：

```json
{
  "available": true,
  "run_id": "run-2331f2093b2f",
  "schema_version": 1,
  "subgraph": "day_ahead",
  "workflow_status": "awaiting_forecast_approval",
  "current_node": "forecast_approval",
  "last_completed_tick": null,
  "dispatch_enabled": false,
  "last_command_id": null,
  "last_ack_accepted": null,
  "updated_at": "2026-08-03T17:28:22.703259",
  "approval_bindings": {
    "forecast_approval": { "status": "pending_approval" },
    "storage_approval": { "status": "idle" },
    "hvac_approval": { "status": "idle" }
  }
}
```

前端 `CheckpointView` 类型定义（`types.ts`）与后端返回结构对齐，页面渲染字段完整。

---

## 三、次要问题

### 3.1 [P3] /api/graph 存在重复边

`/api/graph` 返回的 25 条边中有 4 组重复，导致 SVG 中对应连线叠加渲染（视觉上线条略粗/颜色略深，不影响功能）：

| 重复边 | 出现次数 |
|--------|---------|
| data_agent → prediction_agent | 2 |
| prediction_agent → forecast_approval | 2 |
| storage_agent → dispatch_approvals | 2 |
| hvac_agent → dispatch_approvals | 2 |

**建议**：后端图定义中去重，或前端渲染时按 `from+to` 去重。

### 3.2 [P3] 虚拟节点坐标位于画布边缘

`route`（路由）和 `_fanout_day_ahead`（日前并行分发）两个虚拟路由节点的坐标为 `(0, 0)`，位于 viewBox（`-30 0 860 900`）的左上角边缘。节点矩形（宽 120px）左半部分超出可视区域被裁切。

**建议**：为虚拟路由节点分配合理的画布坐标，或在前端渲染时跳过 `type === 'route'` 且坐标为原点的节点。

---

## 四、验证截图

以下截图已保存至 `docs/verification_reports/`：

1. `agent_flow_fullpage.png`：Agent 工作流页面完整视口截图（1440×900）
2. `topology_graph.png`：拓扑图区域截图（页面顶部）
3. `checkpoint_panel.png`：检查点与恢复状态面板截图

---

## 五、验证结论

Agent 工作流页面的核心功能**验证通过**：拓扑图完整渲染、检查点面板字段齐全、Agent 卡片正常显示、API 数据与页面一致。唯一需要修复的功能性缺陷是 `dispatch_approvals` 节点在 idle 阶段错误显示为 pending_approval（P2），建议在下次迭代中修复。两个 P3 级问题（重复边、虚拟节点坐标）属于数据质量层面，不影响当前功能可用性。
