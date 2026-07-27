# V2 能源调度控制台 · 三轮对抗评审闭环

评审对象：index.html（V2.2.0，2026-07-27）
评审方式：3 个对抗性工程师 agent + Playwright 像素级验证，三轮迭代
交叉基准：outputs/电价组成分析与优化目标厘清_2026-07-27.md、outputs/v3_revise_llm_spec_2026-07-27.md

## 最终判定：三位工程师全部 APPROVE

| 工程师 | 角色 | 最终轮次 | 判定 |
|--------|------|----------|------|
| Hypatia | 能源工程师 | Round 2 | APPROVE |
| Mendel | 可视化工程师 | Round 2 | APPROVE |
| Raman | 运维工程师 | Round 3 | APPROVE |

## 三轮修复轨迹

### Round 0 -> Round 1
- S1 电费日月口径混淆 -> 修复（日电度/月需量分离）
- S2 缺尖峰电价 -> 完全修复（4档：谷0.2758/平0.5867/峰0.8975/尖1.1102）
- S5 温度阈值线不可见 -> 修复（45C红色虚线）
- 前端 renderInsight 运行时崩溃（isC3未声明）-> 修复
- Cr(tau) 标注"物理减排" -> 改为"激励信号(非物理减排)"
- case2 insight "多花钱" -> 改为"既减碳又省钱"
- canvas 无 tooltip / 缺轴标签 / 高度不足 -> 全部修复

### Round 1 -> Round 2
- 编排图 active 边用 forceZero:false / 阈值纳入 range 计算
- 碳图双线间距从 3px 拉开到 20px
- 峰值削减恒为0 -> 不标绿，标注"5MW储能对120MW负荷无削峰效果"
- 所有 4 个 canvas 加 mousemove tooltip + yLabel/yLabelRight

### Round 2 -> Round 3（最终修复）
- S3-a case2 编排图 apply_revision 断裂 -> 修复
  - 补回 human_approval->apply_revision 边（label='revise'，case2 直连）
  - active 边判定从"两端点都在路径"改为"连续相邻"
  - case2/case3 两条 revise 边正确分流不串扰
- tooltip 单位混淆（Mendel S3 建议）-> 修复
  - 每个 series 加独立 name + unit 字段
  - 储能图 tooltip：功率 MW / 温度 C / SOC %（不再混用 MW/C）
- Google Fonts 外链（Mendel/Raman 提醒）-> 移除
  - 改用系统字体栈，离线零错误

## 最终验证
- 36 个单元测试全部通过
- Playwright 验证：3 case active 边互不串扰、4 canvas tooltip 单位正确、零 console error
- Dashboard 90KB 自包含单文件，无外部依赖
- 四个接口保留：数据采集 + 可插拔 prompt + 每日入口 + 蒸馏导出

## 挂账项（不阻断交付）
- G2 衰减成本：优化器输出尚缺 cycles/throughput/degradation 字段
- S2-a LLM 解析详情面板：parse_revision 的槽位/置信度/澄清过程尚无详情视图
- C(tau) 用平均因子而非边际因子：对外披露减碳数字时建议补边际因子对照
