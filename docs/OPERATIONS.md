# 上线与运维

## 上线前检查

1. `pytest` 与前端生产构建全部通过。
2. `ENERGY_AUTO_APPROVE=false`。
3. `ENERGY_CORS_ORIGINS` 仅包含实际前端域名。
4. `ENERGY_ARCHIVE_DIR` 指向可持久化且可备份的卷。
5. Uvicorn 使用 `--workers 1`；不要让多个进程各自运行一套仿真时钟。
6. `/api/health` 返回 `healthy`、`ready=true`、`background_task_running=true`。

## 最小冒烟流程

1. 打开综合页面，确认 96 点时间轨推进且 WebSocket 为已连接。
2. Agent 页面批准预测报告，确认出现储能与 HVAC 两项待审批报告。
3. 仅批准一项调度，确认物理调度仍未激活。
4. 两项均批准后，确认下一步出现 command、ACK 与反馈。
5. 下发储能或 HVAC 人工设定，确认操作记录从 `accepted` 更新为 `executed`。
6. reset 后确认累计值为 0、序列为空、预测重新进入待审批。

## 运行数据

归档目录可整体备份。每次运行按模拟日期和 `run_id` 隔离，不覆盖历史证据。日志不应包含访问令牌或人员隐私。

## 当前部署边界

- 系统执行端为可信模拟器，尚未直接连接 BAS/BMS/PLC。
- 尚未实现企业身份认证与 RBAC；部署到公网前应置于组织网关或反向代理认证之后。
- Docker 配置已提供，但本项目开发机若未安装 Docker，需要在 CI 或目标服务器执行镜像构建验证。
