# 系统架构与安全状态机

## 五层架构

1. 数据接入层：读取园区负荷、电价、湖南发电结构和设备台账；缺口以带 provenance 的工程模拟补全。
2. 确定性工具层：电价与需量核算、电碳因子、储能 MILP、HVAC 调度、空压机能力评估。
3. LangGraph 协同层：Data、负荷处理（Prediction 兼容节点）、Storage、HVAC、Monitor 五类 Agent 通过结构化状态传递结果。
4. GLM 语言层：读取各 Agent 配置，把已验证事实解释为自然语言，并通过 Function Calling 读取状态或提出扰动草案；不拥有算法数值和执行权限。
5. 人机执行层：工程师/厂务自然语言协同、事件确认、三道审批、模拟执行器、ACK、测量反馈、偏差与物理边界监察。

## 工作流状态

```mermaid
stateDiagram-v2
    [*] --> awaiting_forecast_approval: 数据封存与负荷处理完成
    awaiting_forecast_approval --> rejected: 拒绝负荷处理结果
    awaiting_forecast_approval --> awaiting_dispatch_approvals: 批准处理结果并生成两类调度
    awaiting_dispatch_approvals --> rejected: 拒绝任一调度
    awaiting_dispatch_approvals --> awaiting_dispatch_approvals: 仅批准一项
    awaiting_dispatch_approvals --> active: 储能与 HVAC 均批准
    active --> active: 15 分钟指令、ACK 与反馈
    active --> awaiting_forecast_approval: 确认扰动并重算
```

安全不变量：

- `pending_approval` 和 `rejected` 不得产生物理执行命令。
- 调度命令绑定储能/HVAC 报告的 `report_id` 与 SHA-256 哈希。
- 人工覆盖只能在对应系统调度已批准后受理，并在下一仿真步执行。
- SOC、温度、供水温度、COP、负荷边界和执行偏差由 Monitor Agent 检查。
- reset 在同一状态锁内重置时间、累计值、序列、计划、命令和审批状态。
- GLM 只能提出事件；`proposed` 事件不会改变日数据，只有人工 `apply` 后才重算。
- GLM 失败、超时或未配置 Key 时，确定性算法与规则解析继续可用，并在界面标明降级。

## 持久化

`backend/data/archive/<sim_date>/<run_id>/` 按运行归档：

- `reports/`、`approvals/`、`workflow/`
- `commands/`、`acks/`、`feedback/`
- `control_actions/`
- `disturbances/`
- `realtime.csv`

归档采用追加式事件文件和原子替换，适合当前小数据量演示。接入真实 BAS/BMS/PLC 前，应将模拟执行器替换为设备适配器，并保留相同的命令/ACK 接口。
