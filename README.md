# 黄花园区工业能源管理系统 V3

基于 LangGraph 的工业能源智能体监控与调度平台。系统以 200× 架空时间运行 15 分钟粒度的园区日内仿真，覆盖数据采集、负荷预测、储能优化、HVAC 调度、监察、工程师审批与模拟物理执行。

## 已实现能力

- 可执行的 LangGraph `StateGraph`，不是仅供展示的静态拓扑。
- 三道工程师审批门：预测审批、储能审批、HVAC 审批。
- 未审批或已拒绝的策略不会进入物理执行；储能和 HVAC 均批准后才激活调度。
- 15 MW / 30 MWh 储能、19.1 MW 光伏、37 台冷机和 38 台空压机的黄花口径。
- 命令、报告哈希、ACK、测量反馈、告警、人工控制与实时 CSV 的可追溯归档。
- React 三栏控制台、96 点日内调度轨、四个业务页面、上下文联动、响应式抽屉与断线恢复。
- 园区策略、储能功率和 HVAC 供水温度的真实控制 API；未满足审批条件时返回 409。

## 架构

```text
原始数据/模拟补全 → Data Agent → Prediction Agent → 预测审批
                                             ↓
                              Storage Agent + HVAC Agent
                                  ↓                ↓
                              储能审批          HVAC 审批
                                  └──────┬─────────┘
                                         ↓
                              模拟执行器 → ACK/反馈
                                         ↓
                                   Monitor Agent
```

详见 [系统架构](docs/ARCHITECTURE.md) 与 [数据血缘](docs/DATA_PROVENANCE.md)。

## 本地开发

要求 Python 3.11+、Node.js 20+。

```powershell
# 项目根目录
python -m pip install -e ".\backend[dev]"
npm.cmd --prefix .\frontend ci

# 终端 1：后端
python -m uvicorn api.main:app --app-dir .\backend\src --host 127.0.0.1 --port 8000

# 终端 2：前端开发服务器
npm.cmd --prefix .\frontend run dev
```

打开 <http://127.0.0.1:5173>；API 文档位于 <http://127.0.0.1:8000/docs>。

## 本地生产模式

```powershell
npm.cmd --prefix .\frontend run build
python -m uvicorn api.main:app --app-dir .\backend\src --host 0.0.0.0 --port 8000 --workers 1
```

打开 <http://127.0.0.1:8000>。必须保持单 worker：仿真调度器是单主实例，横向扩展时需先把调度器拆成独立服务。

## Docker

```bash
docker compose up --build
```

归档数据写入具名卷 `energy-archive`。容器健康检查访问 `/api/health`。

## 验证

```powershell
$env:PYTHONPATH=".\backend\src"
python -m pytest -q .\backend\tests
npm.cmd --prefix .\frontend run build
```

当前验收基线：后端 16 项测试全部通过，前端 TypeScript 与 Vite 生产构建通过。

## 生产环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ENERGY_ARCHIVE_DIR` | `backend/data/archive` | 报告、审批、命令、ACK 与实时数据归档目录 |
| `ENERGY_DATA_RAW_DIR` | `backend/data/raw` | 内置原始数据目录，可覆盖为挂载路径 |
| `ENERGY_CORS_ORIGINS` | 本地 5173 | 逗号分隔的允许来源 |
| `ENERGY_AUTO_APPROVE` | `false` | 是否自动审批；生产必须保持 `false` |

上线与故障处理见 [上线运维](docs/OPERATIONS.md)。
