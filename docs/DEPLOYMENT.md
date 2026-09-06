# 独立域名上线与扫码访问指南

本文描述如何把当前 FastAPI + React 仿真平台部署到公网 Linux 服务器，通过独立域名提供 HTTPS 访问，并让老师扫码查看实时运行页面。

## 1. 部署形态

```text
老师手机扫码
    ↓
https://你的域名
    ↓
Caddy / Nginx 反向代理 + HTTPS
    ↓
Docker Compose 单容器
    ↓
FastAPI + 前端静态文件 + WebSocket + 仿真循环
```

不要用 GitHub Pages 承载实时平台。`github.io` 只能托管静态文件，不能运行 FastAPI、WebSocket 和常驻仿真进程。GitHub 适合托管代码和静态说明页；实时页面需要云服务器、校园/企业服务器或内网穿透。

推荐服务器配置：

- 2 核 CPU、4 GB 内存、40 GB 系统盘起步。
- Ubuntu 22.04 或 24.04。
- 服务器在中国大陆时，域名必须完成 ICP 备案；活动临近可改用香港/海外服务器，但必须提前在现场网络测速。
- 安全组只开放 `22`、`80`、`443`；`8000` 不直接暴露。

## 2. 域名和 DNS

1. 购买一个尽量短的域名，例如 `energysync.cn`。
2. 在域名控制台添加记录：

```text
A     @       服务器公网IP
CNAME www     你的域名
```

3. 等 DNS 生效后，在本地或服务器检查：

```bash
ping energysync.example
```

返回的 IP 应该是服务器公网 IP。

## 3. 安装基础软件

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

重新登录一次，让 docker 组生效：

```bash
docker --version
docker compose version
```

## 4. 获取代码

```bash
git clone https://github.com/ooopiea/Energy-Carbon-Management-System.git
cd Energy-Carbon-Management-System
```

私有仓库建议使用只读 deploy key，不要把个人 GitHub 密码放在服务器上。

## 5. 配置环境变量

```bash
cp .env.example .env
nano .env
```

最小生产配置：

```env
ZAI_API_KEY=你的智谱APIKey
ENERGY_AUTO_APPROVE=false
ENERGY_SIMULATION_LOOP=true
ENERGY_CORS_ORIGINS=https://你的域名
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
GLM_MODEL=glm-5.2
GLM_THINKING=enabled
```

两种运行策略：

- 安全默认：`ENERGY_AUTO_APPROVE=false`。每次循环回到第一天后，系统重新进入人工审批链，工程师批准后物理调度才继续执行。
- 无人值守演示：`ENERGY_AUTO_APPROVE=true`。系统自动批准三道审批，适合展厅连续播放；不要把这个模式暴露给不可信用户。

循环行为：

- `ENERGY_SIMULATION_LOOP=true` 时，仿真时钟从负载数据首日运行到末日 23:45。
- 末日 24:00 是下一轮的 00:00，系统自动回到数据首日。
- 每轮生成新的 `run_id`，旧报告、审批、命令和 ACK 继续留在归档卷中。
- 200 倍速下一个模拟日为 7.2 分钟；完整循环耗时为 `days_per_cycle * 7.2` 分钟。

## 6. 只让容器监听本机

编辑 `docker-compose.yml`，把端口设置为：

```yaml
ports:
  - "127.0.0.1:8000:8000"
```

这样公网无法直接访问 `8000`，只能通过反向代理进入。仓库的 Compose 配置已按这个安全形态发布。

## 7. 构建并启动

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100 energy-management
```

本机验收：

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/llm/status
```

检查点：

- `status=healthy`
- `ready=true`
- `background_task_running=true`
- `simulation_loop.enabled=true`
- `simulation_loop.days_per_cycle` 大于 0
- `llm.configured=true`

## 8. 配置 Caddy 和 HTTPS

```bash
sudo apt install -y caddy
sudo nano /etc/caddy/Caddyfile
```

基础配置：

```text
energysync.example {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
```

启用 HTTPS 证书：

```bash
sudo systemctl reload caddy
curl -I https://energysync.example
```

Caddy 自动处理证书申请、续期和 WebSocket 反代。

## 9. 扫码访问保护

当前系统没有企业身份认证和 RBAC，不建议裸露在公网。最低成本是给整个站点加 Caddy Basic Auth：

```bash
caddy hash-password --plaintext '现场演示口令'
```

把生成的哈希加入 Caddyfile：

```text
energysync.example {
    encode zstd gzip

    basic_auth {
        teacher 生成的密码哈希
    }

    reverse_proxy 127.0.0.1:8000
}
```

重载：

```bash
sudo systemctl reload caddy
```

老师扫码后会输入一次共享口令。活动结束后立即更换口令。更正式的方案是校园 VPN、Cloudflare Access、Authelia/Authentik，或后续在系统内实现只读访客角色。

## 10. 生成二维码

HTTPS 验收通过后再生成二维码。二维码内容只放：

```text
https://你的域名
```

要求：

- 不放 IP、端口、API Key 或带密码的 URL。
- 打印尺寸不小于 3 cm。
- 二维码旁边保留人类可读域名，扫码失败时可手动输入。
- 另准备一台现场电脑直接打开同一域名，作为演示兜底。

## 11. 现场验收

活动前一天完成：

1. 手机关闭 Wi-Fi，使用 4G/5G 打开域名。
2. 登录后能看到综合页面。
3. 仿真时间持续推进，WebSocket 保持已连接。
4. 页面刷新后状态恢复。
5. `/api/health` 正常。
6. 若开启循环，确认 `time.cycle` 能在跨轮后递增。
7. 执行一次 `reset`，确认当天曲线和累计状态清空。
8. 按安全模式演示一次三道审批和物理执行。
9. 打印二维码并现场扫码测试。

活动当天启动前：

```bash
cd /path/to/Energy-Carbon-Management-System
docker compose ps
curl http://127.0.0.1:8000/api/health
```

如需干净演示状态：

```bash
curl -X POST http://127.0.0.1:8000/api/time/reset
```

## 12. 更新版本

```bash
cd /path/to/Energy-Carbon-Management-System
git pull
docker compose up -d --build
curl http://127.0.0.1:8000/api/health
```

更新前先确认：

```bash
git status
docker compose ps
```

不要在服务器上直接修改业务代码；自定义配置只放在 `.env` 和反向代理配置中。

## 13. 备份归档

备份：

```bash
docker compose exec -T energy-management \
  tar -czf - -C /app/backend/data/archive . \
  > energy-archive-$(date +%F).tar.gz
```

建议：

- 每天至少备份一次。
- 备份文件复制到对象存储或另一台机器。
- 定期抽取一个备份文件检查能正常打开。
- 关注服务器磁盘空间；归档和 Docker 日志会持续增长。

## 14. 故障处理

查看日志：

```bash
docker compose logs -f --tail=200 energy-management
```

常见问题：

- 域名打不开：检查 DNS、安全组 80/443、Caddy 状态。
- 证书失败：确认域名已解析且大陆服务器已完成备案。
- 页面打开但状态不动：查看 `/api/health.background_task_running`。
- WebSocket 断开：检查反向代理是否转发了 Upgrade 请求；Caddy 默认支持。
- GLM 失败：检查 `ZAI_API_KEY` 和服务器出网，不要把 Key 写入前端或 Git。
- 磁盘增长：备份并清理旧归档，同时为 Docker 配置日志轮转。
