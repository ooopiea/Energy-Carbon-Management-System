 # V2 园区能源 Agent 数据核查报告（DATA_AUDIT）

 > 核查员：Auditor（独立核查）
 > 核查日期：2026-07-27（Asia/Hong_Kong）
 > 核查范围：电价 / 负荷 / 发电结构与碳因子 / events 事件日志残缺
 > 数据来源：`case1_result.json`(min_cost) + `case2_result.json`(min_carbon)、`src/energy_agent_v2/`、`数据/湖南数据探查清单_2026-07-26.md`、`数据/CSV格式表格/厂务底稿实际电价组成格式（黄花）2026.7.6.csv`
 > 约束：仅核查、写报告，未改动 `src/` 与 case 代码

 ---

 ## 结论速览（TL;DR）

 | # | 核查项 | 结论 | 类别 |
 |---|--------|------|------|
 | 1 | 电价画成直线 | **数据本身是正确的 3 档阶梯分时价**，"直线"是渲染问题（与负荷共用 Y 轴被压扁）。主 Agent 判断**正确**。 | 渲染问题（数据无需改） |
 | 2 | 电价合理性 | 3 档种子电价作为演示够用，但与真实黄花 4 档电价有偏差，且缺尖峰档与季节性。建议升级为真实电价。 | 数据可改进（非阻塞） |
 | 3 | 负荷曲线 | **合理的工业负荷**：双峰（11h+17h）、峰谷比 2.8、43~120 MW。无明显问题。 | 数据没问题 |
 | 4 | 发电结构 | 煤 60% / 水 24% 与湖南实际（煤为主、水电富集）**大致吻合**，作为演示合理。 | 数据没问题 |
 | 5 | 购电碳因子 0.5366 | 标注"湖南电网2022官方"**不准确**——0.5366 是全国电网平均因子，非湖南专属。量级合理但标签需修正。 | 数据可改进（影响小） |
 | 6 | events 残缺 | **确认为 LangGraph state-merge 缺陷**：`events` 字段缺 `Annotated[..., add]` reducer，`record_node` 用就地 append 但节点返回值不带 `events`。需修代码。 | 代码缺陷（需修） |

 **一句话总结**：电价"直线"是渲染 bug，不是数据 bug；负荷与发电结构 OK；唯一需要动数据的改进是"接真实电价 + 修正碳因子标签"；events 残缺是确凿的代码缺陷，与数据无关。

 ---

 ## 1. 电价核查

 ### 1.1 结论

 电价数据**不是常数直线，而是干净的 3 档阶梯分时价**。可视化里画成直线是**渲染问题**（电价 0.35~1.0 元/kWh 与负荷 43~120 MW 共用同一个 Y 轴，电价被压成贴底细线），主 Agent 的判断**正确**。排除"电价数据本身就是错的"这一可能。

 ### 1.2 数据证据（case1_result.json → `input_bundle.electricity_price_cny_per_kwh`）

 - 共 96 个点（15 分钟粒度 × 24h），**恰好 3 个唯一值**：`{0.35, 0.7, 1.0}` 元/kWh。
 - 三档**严格各 32 点**：谷 0.35 × 32 点、平 0.7 × 32 点、峰 1.0 × 32 点。
 - 时段映射（来自 `provider._build_period_map`）：谷 00:00–07:00 + 23:00–24:00；平 07–10 / 12–15 / 21–23；峰 10–12 / 15–21。这是一个标准的分时阶梯曲线，绘制在独立 Y 轴上应当是清晰的"阶梯平台"，而非直线。

 > 旁证：`SeedDataProvider.PRICE_PEAK/FLAT/VALLEY = 1.0/0.7/0.35`，经 `TariffSchedule.resolve_price_series(96)` 按 period_map 展开后即为上述序列，与 JSON 完全一致。

 ### 1.3 对照真实黄花电价（`厂务底稿实际电价组成格式（黄花）2026.7.6.csv`）

 真实黄花工业电价是**4 档（尖/峰/平/谷）**且**含季节性尖峰**，与种子 3 档有结构差异：

 | 口径 | 尖(sharp) | 峰(peak) | 平(flat) | 谷(valley) | 峰谷价差 |
 |------|----------|----------|----------|-----------|---------|
 | 真实黄花（含税综合，约 2026-01） | ≈1.11 | ≈0.93 | ≈0.59 | ≈0.26 | ≈0.85（尖−谷） |
 | 种子（演示） | —（无尖） | 1.00 | 0.70 | 0.35 | 0.65（峰−谷） |

 - 真实电价有**季节性尖峰**：夏季 7/8 月 20:00–24:00、冬季 1/12 月 18:00–22:00 加开"尖"档；种子无此机制。
 - 种子峰价 1.0 落在真实"峰 0.93 / 尖 1.11"之间，量级合理；但种子谷价 0.35 **高于**真实谷价 0.26，**低估了谷段套利空间**；种子峰谷价差 0.65 < 真实 0.85，**低估了储能套利收益**。

 ### 1.4 真实市场出清价（探查清单）情况

 探查清单确认 EData 中湖南存在 `day_ahead_avg_clearing_price` / `real_time_avg_clearing_price`：均为 **15 分钟曲线、约 44,928 点、覆盖 2025-01-01 ~ 2026-04-13**，是高质量的日前/实时出清价时序。但清单只给出时间范围与点数，**未给出具体价格数值**（需建库查询才能看量级）。

 > 说明：对**园区表后储能**（behind-the-meter）做电费优化，**正确的成本信号是用户侧分时电价（厂务底稿那种零售 TOU），而非批发侧出清价**。出清价只有在储能参与电力市场（front-of-meter / 聚合商）时才直接驱动决策。

 ### 1.5 是否要接真实市场价？——分级建议

 - **P0（必做，属于渲染层）**：给电价单独的次坐标轴或独立子图，让阶梯价可见。**与数据无关，纯渲染修复**。
 - **P1（强烈建议，数据改进）**：把种子 3 档电价替换为**真实黄花 4 档电价**（CSV 已在手），并支持季节性尖峰档。这会让储能套利收益、需量电费核算更贴近真实。
 - **P2（可选，按场景）**：接入 EData 出清价 `day_ahead_avg_clearing_price`。仅当未来要做"市场感知/聚合参与批发市场"模式时才必要；当前表后电费优化场景**非必需**。

 ---

 ## 2. 负荷核查

 ### 2.1 结论

 负荷曲线是**合理的工业负荷形态**，无明显数据问题。

 ### 2.2 数据证据（case1_result.json → `input_bundle.load_forecast_kw`）

 - 范围：**最小 ≈ 43 MW（42,991 kW），最大 ≈ 120 MW（120,287 kW），均值 ≈ 82 MW**。
 - **峰谷比 ≈ 2.8**，处于典型工业负荷区间（2~3）。
 - 形态（来自 `SeedDataProvider._synth_load` 的 `hourly_base`）：**双峰**——上午峰 ≈ 11h、下午峰 ≈ 17–18h，夜间谷段回落，符合工厂"两班/三班制 + 午间回落"的典型工业曲线。
 - 全程叠加 ±2% 随机抖动并 clip 在 [38, 125] MW。

 > 注：任务背景里可视化标注的"负荷 40~76 MW"与实际数据范围（43~120 MW）不符，疑为渲染/标注口径问题（可能只显示了一段或图例错位）；实际数据本身是健康的工业曲线，无需改。

 ---

 ## 3. 发电结构与碳因子核查

 ### 3.1 发电结构——结论：合理

 按 24h 功率加权聚合（case1 generation_mix）：

 | 能源 | 占比 | 说明 |
 |------|------|------|
 | 煤 coal | 59.9% | 与湖南"煤电为主"吻合 |
 | 水 hydro | 24.1% | 与湖南"水电富集省"特征吻合 |
 | 风 wind | 5.8% | 合理 |
 | 外购 purchase | 5.8% | 合理（省间/网购） |
 | 光 solar | 4.4% | 白昼注入，24h 均摊后合理 |

 湖南实际电网以煤电为主、水电为重要支撑（湖南是水电大省），风电/光伏逐年提升。煤 60% / 水 24% 作为**演示用的典型湖南结构**是站得住脚的，**无需改动**。（真实建模可后续用 EData 的 `real_time_generation_type_energy_by_period` / `real_time_*_power` 校准，但非阻塞。）

 ### 3.2 排放因子——结论：量级合理，但**购电因子标签不准确**

 `EmissionFactorLibrary` 默认因子（case1 实际使用值）：煤 0.85、气 0.40、油 0.75、水/风/光/核/生物质 = 0、**购电 purchase = 0.5366**（kgCO2/kWh）。

 - 煤 0.85：直接排放因子，量级合理（直接约 0.8–0.95）。
 - **购电 0.5366，代码与注释标注为"湖南电网2022官方"——这是不准确的。** 0.5366 实为生态环境部公布的 **2022 年度全国电网平均排放因子**（0.5366 tCO2/MWh），并非湖南专属值。湖南因水电占比高，其区域电网平均因子应**低于**全国均值（经验区间约 0.40–0.45）。标签应修正为"全国电网平均2022（借用）"，或基于 `real_time_generation_type_energy_by_period` + 燃料因子校准出真正的湖南因子。
 - 影响范围有限：购电仅占 5.8%，且仅在总发电为 0 的时步作为兜底因子；对整体 C(τ) 影响小，但**对外可信度/可审计性有影响，建议修标签**。

 ### 3.3 计算出的碳因子（case1 carbon_result）——内部自洽

 - 直接因子 C(τ)：min 0.486 / max 0.565 / **mean 0.545** kg/kWh。
 - 责任因子 Cr(τ)：min 0.460 / max 0.575 / **mean 0.545** kg/kWh（λ=0.5 动态调节后均值略高于 C̄，符合代码注释里的守恒近似）。
 - 验算：煤 0.599×0.85 + 购电 0.058×0.5366 ≈ 0.54，与 mean 0.545 吻合，**计算链路正确**。

 > 附带观察（非数据问题）：case1 (min_cost) 直接碳减排 = **−1021.5 kg**（负值），case2 (min_carbon) 牺牲电费 251 元换取 +144.8 kg 减排。这正好体现"低成本 vs 低碳"的目标冲突，是双目标设计的预期行为，不是 bug。

 ---

 ## 4. events 事件日志残缺——根因 + 修复方案

 ### 4.1 现象

 - case1（min_cost，单条 approve 链路）events = **仅 1 条**（`freeze_plan`）。
 - case2（min_carbon，含 revise 回路）events = **仅 5 条**，且全部来自**末段 ainvoke**：`apply_revision / optimize_storage(V2) / compute_carbon(V2) / factory_summary / freeze_plan`。
 - 完整链路应产生 ~7 条（case1）/ ~12 条（case2）事件，前段节点（initialize_run / load_inputs / compute_tariff / optimize V1 / compute_carbon V1 / factory_summary V1）**全部丢失**。

 ### 4.2 根因（确凿）

 问题出在 `orchestration.py`，是典型的 **LangGraph state-merge 反模式**：

 1. `EnergyDispatchStateV2.events` 是**裸 `list[dict]`，没有 `Annotated[list, operator.add]` reducer**：
    ```python
    events: list[dict[str, Any]]   # ← 缺 reducer，默认 LastValue（整体覆盖）
    ```
 2. `record_node` 采用**就地 append**：
    ```python
    events = state.setdefault('events', [])
    events.append(event)            # ← 只改了入参 state 的本地副本
    ```
 3. 各业务节点返回的 dict（如 `load_inputs_node` 返回 `{"input_bundle":..., "error":None}`）**都不含 `events` 键**。

 **机制**：LangGraph 用每个 channel 的 reducer 合并"节点返回值"来推进状态；默认 LastValue reducer 只认返回值里的键。`events` 既无 reducer、又不在返回值里，于是就地 append 不会被持久化——尤其在 `MemorySaver` checkpoint + interrupt/resume 跨 superstep 时，状态从 checkpoint 重水合（rehydrate），就地修改被丢弃。最终只有**最后一次 ainvoke 末段 superstep** 的事件"漏"进了返回的 state，正好解释了 case1=1、case2=5 的残缺形态。

 ### 4.3 修复方案（二选一）

 **方案 A（推荐，LangGraph 惯用法）——给 reducer + 让节点返回事件**
 ```python
 import operator
 from typing import Annotated

 class EnergyDispatchStateV2(TypedDict, total=False):
     ...
     events: Annotated[list[dict[str, Any]], operator.add]   # ← 加 reducer
 ```
 再把 `record_node` 从"就地 append"改为"返回单条事件"，各节点把它并入返回值（`operator.add` 会累加，**务必只返回新增的那 1 条**，避免与就地 append 叠加导致重复计数）：
 ```python
 async def record_node(state, node_id, summary, *, status="succeeded", ...):
     event = {...}
     return [event]                       # 不再就地改 state

 # 节点侧示例
 new_events = []
 new_events += await record_node(state, "load_inputs", ...)
 return {"input_bundle": ..., "error": None, "events": new_events}
 ```

 **方案 B（低风险，彻底解耦）——把审计日志挪出 state，放进 AppContextV2**
 - 在 `AppContextV2` 增加 `self.events: list[dict] = []`；
 - `record_node` 通过 `runtime.context.events.append(event)` 累加；
 - `runner.run_dispatch` 在运行结束后从 `ctx.events` 一次性组装结果里的 events。

 方案 B 让审计日志完全脱离 LangGraph 的 channel 合并，最不易出错；缺点是 events 不再是可 checkpoint/可恢复的状态字段（对当前演示场景可接受）。

 > 建议：若希望 events 随 checkpoint 恢复（断点续跑也能复盘），选 **A**；若只想要一份可靠的运行审计，选 **B**。

 ### 4.4 附带 smell（次要）

 `run_case2.py` 在 revise 后用 `run_dispatch(ctx, result_v1, approval_decision="approve")` 把"上一轮的 result 当作 initial_state"再次整跑——这会让整条链路从头再跑一遍（重复 load_inputs/optimize 等），既低效也会让 events 归属更混乱。建议 case2 复用同一 thread_id 的 checkpoint 继续 resume，而非另起一轮。这不影响 events 根因判断，但属于独立的工程改进点。

 ---

 ## 5. 总结：哪些只需修渲染，哪些需改进数据/代码

 **A. 数据没问题，只需修渲染（最高优先级）**
 - 电价"直线"：给电价独立次坐标轴或单独子图即可。阶梯价数据本身正确。

 **B. 数据没问题，无需改动**
 - 负荷曲线（双峰、峰谷比 2.8、43~120 MW，工业形态正常）。
 - 发电结构（煤 60% / 水 24%，典型湖南，合理）。
 - 碳因子计算链路（C(τ)/Cr(τ) 自洽，双目标冲突行为符合预期）。

 **C. 数据可改进（非阻塞，按价值排序）**
 1. 电价升级为真实黄花 4 档（尖峰平谷）+ 季节性尖峰（CSV 已在手，P1）。
 2. 购电碳因子 0.5366 标签修正为"全国电网平均2022（借用）"，或校准真正的湖南因子（影响小但关乎可信度，P1）。
 3. （可选 P2）接入 EData 出清价 `day_ahead_avg_clearing_price`，仅在做"市场感知/聚合参与批发市场"模式时必要。

 **D. 代码缺陷（需修，与数据无关）**
 - events 残缺：根因是 `events` 字段缺 `Annotated[list, operator.add]` reducer + `record_node` 就地 append 未进节点返回值。按 §4.3 方案 A 或 B 修复。

 ---

 *报告结束。所有结论均基于实际文件与 case_result.json 的量化核查，未对 `src/` 及 case 代码做任何改动。*
