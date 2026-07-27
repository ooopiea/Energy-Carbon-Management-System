# V2.1 重构版对抗性评审报告

评审对象: V2.1 重构后代码（电价4档 + 碳守恒 + GLM-5 LLM接入 + 每日试运行 + 回放导出）
评审日期: 2026-07-27
评审视角: 能源工程师 / 运维可靠性 / 架构质量
交叉核对: tariff.py ↔ provider.py ↔ carbon_accounting.py ↔ orchestration.py ↔ llm/ ↔ contracts.py

## 总体判定: 可用于试运行

代码底座扎实。电价模块从3档合成升级为真实月度4档（含季节性尖峰），碳模块修复了守恒性，
LLM接入层（parse_revision + 三层校验 + 澄清回路 + 蒸馏采集）设计合理且经 Mock 端到端验证。
本轮修复了上一轮 S1 的展示层残留问题，补建了每日入口和回放导出两个接口。

---

## 本轮修复（2026-07-27 第二轮）

### F1. S1 残留修复: 电费展示口径分离 [已修复]
- 问题: `total_cost_cny` 把月度需量电费(3,680,773元/月)混入日电度电费(1,311,052元/日)，
  导致"基线电费504万"被误读为日费用，节省率从真实的0.39%被压缩到0.10%。
- 修复: `compute_tariff_node` 事件展示改为"日电度电费 X 元 | 需量电费(月) Y 元"；
  `print_result` 拆分日度/月度4行展示；benefit标注改为"日电度电费节省"。
- 数据模型本身已正确分离(energy_cost/demand_cost/gov_fund/reactive独立字段)。

### F2. 蒸馏路径修复: 相对路径→绝对路径 [已修复]
- 问题: `parse_revision_node` 硬编码 `Path("data/distillation")`，依赖 cwd，换目录写入位置不可控。
- 修复: 新增 `_DISTILLATION_DIR` 模块级常量，基于 `__file__` 解析到 `v2_energy_agent/data/distillation/`。

### F3. 补建 D2: 每日试运行入口 [已完成]
- `cases/daily_trial/run_daily.py`: 支持 --date/--objective/--mock 参数，CLI 交互式审批，
  revise→LLM解析→澄清→fallback 全链路。结果保存到 `results/run_{date}.json`。

### F4. 补建 D3: 回放导出工具 [已完成]
- `tools/export_trial_week.py`: 扫描 results + distillation，输出 summary/pairs/events 三件套。

---

## 已知限制（文档标注，试运行可接受）

### L1. min_carbon 用平均碳因子 C(τ) 而非边际 MEF [文档标注]
- 储能转移的是边际电量，用系统平均因子会高估 min_carbon 的真实减碳量。
- `carbon_accounting.py` notes 已明确标注，spec D11 记录为已知限制。
- 试运行可接受: 采集数据反哺 V3 后可引入 MEF 近似。

### L2. Cr(τ) 是激励信号非物理减排 [文档标注]
- 经 V2.1 乘法归一化后 mean(Cr)=mean(C)，守恒性已修复。
- notes 标注"用于优化方向引导而非碳交易/碳报告"。

### L3. 5MW 储能对 110MW 负荷杯水车薪 [物理事实]
- 需量管理(limit_peak_demand)对当前储能规模几乎无效，peak_reduction 显示 0kW 属实。
- 非 bug，是物理约束。dashboard 不应将其做成显眼 KPI。

### L4. 发电结构为合成数据 [数据局限]
- 煤电占比 0.62 对湖南（水电大省）偏高，需接真实数据校准。
- `db_fetcher.py` 已预留接口，试运行期间可接入。

---

## 架构质量评估

### A1. parse_revision inline interrupt 设计 [正确]
- 节点内部处理全部多轮逻辑(2轮澄清+1次fallback)，exit 时 revision_resolved 必为 True。
- 图边简化为 `parse_revision → apply_revision`，无需 conditional routing。
- 注意: resume 时节点从头重执行(LangGraph机制)，LLM会被重复调用(round1调2次)。
  功能正确但浪费 API 配额。试运行期间可接受，V3 可加状态缓存优化。

### A2. 三层校验链 [设计合理]
- Pydantic(范围) → 语义收紧(SOC只能升/功率只能降) → 置信度(<0.7标记澄清)。
- 低置信度不触发失败，只触发澄清。避免了"LLM不确定就拒绝"的僵化行为。

### A3. config.py 未接入 RevisionParser [低优先]
- config.py 有 llm_parse_confidence_threshold/max_rounds 等配置，
  但 RevisionParser 构造时用的是硬编码默认值。
- 影响: 不影响功能，试运行脚本直接传参即可。

---

## 验证状态

- 36 单元测试全绿
- Case1 (min_cost→approve): 8事件，省5150元，通过
- Case2 (min_carbon→revise→approve): 14事件，Cr减排367kg，通过
- Case3 (LLM revise→clarify→approve): 14事件，蒸馏已写入，通过
- D2 run_daily.py: mock模式 approve 流程通过
- D3 export_trial_week.py: 1运行+2配对导出通过

---

*评审结论: V2.1 重构版可进入工程师试运行阶段。*
