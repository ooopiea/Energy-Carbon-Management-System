# V3 工业能源管理系统

基于 LangGraph 的工业能源智能体监控与调度平台。

## 架构

- **5 个 Agent**：数据采集 / 负荷预测 / 储能调度 / HVAC 调度 / 系统监察
- **Human-in-the-Loop**：预测→调度、调度→物理执行之间设有工程师审批
- **200x 时间引擎**：系统自主计时，模拟一天内厂区发生的所有事情
- **复用 V2 算法**：江亿动态碳排放责任因子 Cr(τ)、分时电价、MILP 储能优化

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11+ / FastAPI / LangGraph / PuLP |
| 前端 | React 18 / TypeScript / Vite / ECharts / Tailwind CSS |
| 通信 | REST + WebSocket (实时推送) |

## 目录结构

```
v3_energy_management/
├── backend/
│   ├── src/
│   │   ├── core/        # 配置、时间引擎、状态
│   │   ├── agents/      # 5个Agent实现
│   │   ├── algorithms/  # 电碳、电价、储能优化
│   │   ├── data/        # 数据加载、模拟生成
│   │   ├── graph/       # LangGraph 编排
│   │   └── api/         # FastAPI 路由
│   ├── agents_config/   # 每个Agent的专属配置
│   └── data/            # 处理后和模拟数据
├── frontend/
│   └── src/
│       ├── pages/       # 4个主页面
│       ├── components/  # 共享组件
│       └── stores/      # Zustand状态
└── docs/
```

## 快速启动

```bash
# 后端
cd backend
pip install -e .
uvicorn src.api.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev
```
