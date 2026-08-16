# 上线与运维

## 上线前检查

1. `pytest` 与前端生产构建全部通过。
2. `ENERGY_AUTO_APPROVE=false`。
3. `ENERGY_CORS_ORIGINS` 仅包含实际前端域名。
4. `ENERGY_ARCHIVE_DIR` 指向可持久化且可备份的卷。
5. Uvicorn 使用 `--workers 1`；不要让多个进程各自运行一套仿真时钟。
6. `/api/health` 返回 `healthy`、`ready=true`、`background_task_running=true`。
7. `/api/health.llm.configured=true`，且页面只展示模型名和状态，不展示 Key。

## 最小冒烟流程

1. 打开综合页面，确认 96 点时间轨推进且 WebSocket 为已连接。
2. Agent 页面批准负荷处理报告，确认出现储能与 HVAC 两项待审批报告。
3. 仅批准一项调度，确认物理调度仍未激活。
4. 两项均批准后，确认下一步出现 command、ACK 与反馈。
5. 下发储能或 HVAC 人工设定，确认操作记录从 `accepted` 更新为 `executed`。
6. 厂务协同页输入“今天 14:00 3号冷机故障，预计2小时恢复”，确认只生成草案。
7. 点击“确认并重算”，确认事件变为已应用、冷机可用数下降且负荷处理重新进入待审批。
8. reset 后确认累计值为 0、序列为空、负荷处理重新进入待审批。

## GLM 配置与密钥轮换

- 推荐环境变量为 `ZAI_API_KEY`；系统也兼容 `ZHIPU_API_KEY` 和 `GLM_API_KEY`。
- 默认端点为 `https://open.bigmodel.cn/api/paas/v4/`，默认模型为 `glm-5.2`。
- 本地可从 `.env.example` 复制配置，但启动 Uvicorn 前仍需把变量注入进程环境；Docker Compose 会读取同名宿主环境变量。
- Key 只允许存在于服务器环境或密钥管理服务，不得写入前端、归档、日志、截图或 Git。
- 轮换后重启单 worker 服务，再检查 `/api/llm/status`；不要在聊天窗口粘贴 Key。

## 运行数据

归档目录可整体备份。每次运行按模拟日期和 `run_id` 隔离，不覆盖历史证据。日志不应包含访问令牌或人员隐私。

## 当前部署边界

- 系统执行端为可信模拟器，尚未直接连接 BAS/BMS/PLC。
- 尚未实现企业身份认证与 RBAC；部署到公网前应置于组织网关或反向代理认证之后。
- Docker 配置已提供，但本项目开发机若未安装 Docker，需要在 CI 或目标服务器执行镜像构建验证。
