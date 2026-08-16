# v3 后端忠实化改造 — 代码审查报告

- 审查范围：本次忠实化改造引入/修改的全部新代码
- 审查对象：`src/graph/spec.py`、`src/graph/compiler.py`、`src/agents/engine.py`、`src/repositories/state_repository.py`、`src/collaboration/*`
- 审查方法：逐文件人工静态审查 + 针对性运行时验证 + 全量 `pytest`
- 审查日期：2026-08-03
- Python：3.14.2 / pydantic 2.13.4 / pytest 9.1.1 / langgraph（asyncio AUTO）

## 测试结论

全量测试 **105 passed, 0 failed**（运行命令 `python -m pytest tests/ -p no:cacheprovider`，耗时约 33s）。

> 说明：若按任务给定命令带 `--basetemp="D:\...\v2_energy_agent\work\pytest_basetemp"` 运行，会出现 47 个 **ERROR（非 FAILED）**。经逐项排查，这些 ERROR 全部来自该 `--basetemp` 目录残留的 `PermissionError`（rm_rf 删除旧 basetemp 失败、`.pytest_cache` 在 D 盘项目根目录无写权限），与被测代码无关。去掉该 `--basetemp`、禁用 cacheprovider 后全部通过。建议后续将 `--basetemp` 指向本后端项目内的可写目录。

## 问题汇总

| 级别 | 数量 | 含义 |
|------|------|------|
| P0 阻断 | 0 | 无导入崩溃、无核心流程硬故障 |
| P1 高 | 1 | G4 重启恢复在引擎层未实现 + G3 跨重启失效 |
| P2 中 | 4 | fail-closed 漏洞、forecast 修订死路、reject/控制动作不落 checkpoint、安全内核不校验提案动作 |
| P3 低 | 7 | 白名单幽灵键、可达性检查非真遍历、checkpoint 视图缺字段、agent 推断脆弱等 |

---

## P1 — 高严重度

### P1-1 引擎层未实现 restart-resume，G3 幂等在跨重启时失效
- 文件：engine.py
- 关键位置：`__init__`→`_clear_all_state()`（全新状态，`workflow_status="idle"`、新 `run_id`）；`_state_repo` 仅 `save`（L297）与 `_get_checkpoint_view` 的 `load`（L1470，仅展示）；`_acked_commands` 仅内存（L162 / L228 清理、L823/L902 读写）
- 描述：`StateRepository`/`RuntimeCheckpoint` 这套 G4 恢复设施完整，但引擎从未消费 checkpoint 来恢复。构造函数总是 `_clear_all_state()` 重新开始，`_start_day_locked`→`_prepare_day` 恒置 `workflow_status="new"` 并生成新 `run_id`。没有任何 `resume()`/从 checkpoint 还原 `workflow_status`、`last_step`、`dispatch_enabled` 的路径。
- 连带影响（更严重）：G3 幂等字典 `_acked_commands` 只活在内存里，既不在 `RuntimeCheckpoint` 中、也不会在重启后重建。一旦进程崩溃重启，已 ACK 的物理命令会因内存字典丢失而被**重复执行**，违反“已确认命令不重复执行”的不变式。
- 关于测试：`tests/test_state_repository.py::test_restart_resumes_pending_approval` 的文档串声称“restart correctly resumes workflow position without repeating side effects”，但实际只断言 `repo.load(cp.run_id)` 可读（即旧 checkpoint 还在共享 repo 里），并未验证 `engine2` 真正恢复了工作流位置或跳过已 ACK 步骤。`engine2 = SimulationEngine(...)` 同样走 `_clear_all_state()`，是全新状态。**测试与名字/文档串不符，给了虚假的安全感。**
- 建议：
  1. 在引擎增加显式恢复路径：构造时按给定 `run_id` `load` checkpoint，还原 `workflow_status`/`current_node`/`last_step`/`dispatch_enabled`/`approval_bindings`，并据此重建 `_acked_commands`（或将其纳入 checkpoint 持久化）。
  2. 将 `_acked_commands`（至少 `(run_id, step)→command_id`）持久化进 `RuntimeCheckpoint`，使 G3 幂等跨重启成立。
  3. 强化 `test_restart_resumes_*`：断言恢复后 `workflow_status`、`last_completed_tick` 与断点一致，且重放已 ACK 步骤不触发 executor。

---

## P2 — 中严重度

### P2-1 FileStateRepository.load 的 fail-closed 未覆盖非 dict JSON
- 文件：state_repository.py:105
- 描述：`load` 的 `except (json.JSONDecodeError, ValueError, KeyError, TypeError)` 想做 fail-closed。但当 checkpoint 文件是合法但非对象的 JSON（如 `[1,2,3]` 或 `"hello"`）时，`json.loads` 返回 list/str，随后 `data.get("schema_version")` 抛 **`AttributeError`**，未被捕获，异常直接外泄。
- 运行时验证（已复现）：写入 `[1,2,3]` → `AttributeError: 'list' object has no attribute 'get'`；写入 `"hello"` → 同样 `AttributeError`。
- 备注：pydantic 2.13.4 的 `ValidationError` 确为 `ValueError` 子类（已验证 MRO），故字段级校验异常已被覆盖；漏洞仅限“顶层不是 dict”这一类。
- 影响：损坏/被篡改的非对象 checkpoint 会让 `_get_checkpoint_view` 抛异常而非优雅降级，违背 fail-closed 契约。
- 建议：先判 `if not isinstance(data, dict): return None`，或把 `AttributeError` 加入 except 元组。

### P2-2 forecast_approval 的 revise 是空操作，工作流落入不可路由状态
- 文件：engine.py:683-685
- 描述：`_submit_approval_locked` 的 revise 分支对 `storage_approval`/`hvac_approval` 会置状态并重跑对应分支（G2 局部修订，正确）；但 `forecast_approval` revise 落到末尾兜底：仅 `workflow_status="revision_requested"`、`current_node=gate_id` 后直接 `return gate`，**既不重跑 prediction、也不调用 `_invoke_graph_locked`、也不 `_save_checkpoint`**。
- 影响：`"revision_requested"` 不在 `route_map` 中（route_map 仅 new/forecast_approved/storage_revision/hvac_revision/dispatch_approved/wait），任何后续图调用都会走默认 `"wait"`→END。工程师对预测报告点“修订”后无法触发重新计算，工作流停在一个不可路由的中间态，且该转换未落 checkpoint。
- 建议：要么为 forecast 修订实现真实重算（如增加 `forecast_revision`→`prediction_agent` 路由并重绑 gate），要么显式拒绝 forecast 的 revise 决策并返回明确错误；当前两种语义都未实现。

### P2-3 reject 与控制动作等转换未保存 checkpoint
- 文件：engine.py:687-694（reject）、`submit_control_action` L728-783、forecast revise L683-685
- 描述：`_save_checkpoint` 仅在 `_invoke_graph_locked`（L265）与 `_update_realtime_locked`（L989）被调用。reject 路径改了 `workflow_status="rejected"` 与 `_dispatch_enabled=False` 却不落 checkpoint；`submit_control_action` 改 `_demand_cap_kw`/入队 override 同样不落。
- 影响：持久化 checkpoint 与内存态不一致——展示给前端的 `_get_checkpoint_view` 以及（未来）恢复点会停留在旧状态。在 P1-1 的恢复能力补齐前，这种不一致会被放大。
- 建议：在所有改变 `workflow_status`/`_dispatch_enabled`/`_demand_cap_kw` 的分支末尾统一调用 `_save_checkpoint()`（或抽一个“状态变更后必落盘”的收口）。

### P2-4 安全内核只校验快照态、不校验提案动作的取值
- 文件：safety_kernel.py:43-66
- 描述：`SafetyKernel.validate` 对 SOC/温度/供水温度的检查全部读自 `current_snapshot.facts`（当前态），而**完全不检查 `proposal.actions` 会把这些量推向何方**。一个“把 SOC 设到 0.95 / 供水温度设到 4℃”的提案，只要当前态合规就能 PASS。
- 影响：与“不可由 LLM 覆盖的硬约束（MA-4）”定位不符——内核挡不住“会把系统推越限”的动作本身。
- 备注：当前 `JointProposal.actions` 是 `JointActionType` 枚举列表（动作类型），不携带数值参数，所以严格说内核“无值可校验”。但正因为如此，真正承载数值的 `propose_disturbance`/`submit_control_action` 路径仍由引擎侧的 `_validate_physical_action` 等兜底，内核这一层并未形成闭环。建议明确内核职责边界并在文档/代码中标注，或让提案携带可校验的目标值。

---

## P3 — 低严重度

### P3-1 context_broker 白名单存在幽灵键，且缺失部分关键字段
- 文件：context_broker.py:18-27
- 描述：STORAGE/HVAC 白名单里的 `day_ahead_storage_plan`、`day_ahead_hvac_plan` 在 `engine.get_state()` 中**不存在**（get_state 只有 `day_ahead` 嵌套字典与 `storage_summary`/`hvac_summary`）。这两个键实际只对应 `core/state.py` 的 `RuntimeState`（旧模型，引擎并不产出该实例）。一旦把引擎接到多智能体运行时，储/HVAC agent 经 broker 将拿不到任何日前计划数据。
- 附带：STORAGE 缺 `grid_kw`/`load_kw`/`solar_kw`，HVAC 缺 `price`，RISK 缺 `daily`/`physical_dispatch`。
- 现状缓解：目前生产 `src` 内没有把 engine 接入多智能体的快照提供者（`capture()` 实现仅在 tests 中，facts 为静态字典），故为潜在缺陷而非现行故障。
- 建议：以 `engine.get_state()` 的真实键为准重写白名单（含 `day_ahead` 嵌套对象），并补足各角色做决策所需字段。

### P3-2 validate_spec 的不可达检查不是真正的可达性遍历
- 文件：spec.py:168-189
- 描述：规则5把“所有路由目标 ∪ 所有执行边目标 ∪ 路由源”当作可达集，再做差集。这是“边端点并集”而非从 START 出发的图遍历（BFS/DFS）。对一个“自身不可达、但其出边目标却被并进去”的传递性不可达节点，会产生漏报。
- 影响：对当前静态 WORKFLOW_SPEC 无实际错误（已通过校验），但该检查无法可靠防止未来引入“级联不可达”子图。
- 建议：改为从 START/路由源出发的真遍历来计算可达集。

### P3-3 `_get_checkpoint_view` 缺字段，`pending_recovery_reason` 为死字段
- 文件：engine.py:1468-1486、`_save_checkpoint` L295
- 描述：视图未暴露 `sim_time`/`day`/`step`/`pending_recovery_reason`，而恢复 UI/逻辑会用到 day/step。同时 `_save_checkpoint` 恒置 `pending_recovery_reason=""`，该“恢复受阻原因”字段从未被真正写入，是未完成特性。
- 建议：视图补 day/step（及必要时的 pending_recovery_reason）；要么实现 pending_recovery_reason 的判定，要么移除以免误导。

### P3-4 `_infer_agent_id` 关键词推断脆弱，未命中即静默 FAILED
- 文件：mission_runtime.py:186-196
- 描述：靠 objective 文本的子串匹配（“data”/“storage”/“hvac”/“risk” 及中文词）推断 agent。子串匹配易误判（如 “metadata” 命中 data、“frisk” 命中 risk）；未命中返回 `""`，随后 `_agents.get("")` 为 None，任务被记为 FAILED 并 continue——属于静默失败。
- 建议：让 `AgentTask` 显式携带 `agent_id`/`role`，由 coordinator 直接指派，避免靠自然语言推断；或至少对未命中抛出可观测错误。

### P3-5 safety_kernel 的 COP_MIN 定义未使用，且未检查负荷区间
- 文件：safety_kernel.py:17
- 描述：`COP_MIN` 常量定义后从未在 `validate` 中使用；`PhysicsConstraints` 里的 `load_min_kw/load_max_kw`、COP、爬坡率等约束也未进入内核。运行时由引擎 `_monitor_check` 兜底，但“硬约束内核”这一层覆盖不全。
- 建议：补 COP/负荷校验或显式声明这些约束由引擎侧负责，删除死常量。

### P3-6 FileStateRepository.save 残留临时文件、无 fsync
- 文件：state_repository.py:120-127
- 描述：`write_text` 后 `os.replace`，结构正确；但写失败会残留 `*.tmp` 孤儿文件，且写后未 `fsync` 即 replace（崩溃窗口内可能 replace 未落盘的半成品）。对单实例 demo 可接受。
- 建议：写失败时清理 temp；如对持久化强度有要求，flush+fsync 后再 replace。

### P3-7 需量上限（demand_cap）实现为单旋钮，仅放电、无下界/爬坡校验
- 文件：engine.py:857-862
- 描述：`_demand_cap_kw` 命中时仅把超出量加到 `storage_setpoint`（放电），并仅以 `max_discharge_power_kw` 封顶；不校验爬坡、不校验 SOC 余量、不联动 HVAC。最终由 executor 兜底校验爬坡/SOC。
- 建议：在封顶处同步考虑爬坡与 SOC 余量，或将该策略明确标注为“仅储能削峰”的受限实现。

---

## 已验证为正确/稳健的设计点（平衡记录）

- **`_graph_dispatch_approvals` 条件重绑定（engine.py:531-559）无竞态**：所有变更均经 `_transition_lock`（asyncio.Lock）串行化，`_invoke_graph_locked` 在锁内 `await ainvoke` 完成，单实例下无交织；逻辑上 storage_revision 仅重算储能、hvac gate 凭 `report_id` 不变而保留既有批准，G2 局部修订语义正确。
- **`_fanout_day_ahead` 并行分发**：以 `__route__` 透传节点 + 两条执行边实现 fan-out，再于 `dispatch_approvals` 汇合 join，LangGraph 原生支持，结构正确。
- **conditional_routes 覆盖完整**：route_map 覆盖 new/forecast_approved/storage_revision/hvac_revision/dispatch_approved/wait（END），缺失值默认回落 `wait`→END，安全。
- **`_acked_commands` 在 reset/start_day 正确清理**：`_clear_all_state`（L162）与 `_prepare_day`（L228）均重置，新 `run_id` 配合清空，日内幂等成立（跨重启问题见 P1-1）。
- **边定义完整、不可达检查对当前 spec 通过**；编译器 terminal→END 收口与路由条件边接线正确，无悬挂节点。
- **feature_flags 环境变量读取可靠**：`strip().lower()` + 未匹配回落 DEFAULT_MODE（template），fail-safe。
- **ApprovalGate/报告哈希绑定、stale report_id/hash 拒绝、双审批汇合**等安全不变式在 `test_safety_invariants`/`test_engine_workflow` 中均通过。

## 结论

忠实化改造整体结构清晰：单一 `WorkflowSpec` 单源、fan-out/join 工作流、审批绑定与幂等执行的主路径正确，105 项测试全绿。但存在 **1 个 P1（G4/G3 跨重启恢复未落地且测试给了虚假信心）**、**4 个 P2（fail-closed 漏洞、forecast 修订死路、转换不落 checkpoint、内核不校验提案动作）** 与若干 P3 设计缺陷。建议优先处理 P1-1 与 P2-1/P2-2。
