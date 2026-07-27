# 大模型接入配置

> 代码入口：`src/energy_agent_v2/llm/client.py` -> `create_llm_client()`
> 文档日期：2026-07-27

## 接入方式

系统通过环境变量自动检测是否接入真实大模型。设置了以下两个变量后，parse_revision 节点会调用真实 GLM-5 解析工程师的自然语言修改意见。

## 环境变量

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `LLM_API_KEY` | 智谱 API key（必填） | `xxxxx.xxxxx` |
| `LLM_BASE_URL` | OpenAI 兼容端点（必填） | `https://open.bigmodel.cn/api/paas/v4` |
| `LLM_MODEL` | 模型名（默认 glm-5） | `glm-5` |
| `LLM_TIMEOUT` | 超时秒数（默认 30） | `60` |

## 设置方法

### PowerShell（临时）
```powershell
$env:LLM_API_KEY = "你的key"
$env:LLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
python cases/case3_llm_revise/run_case3.py
```

### PowerShell（永久，写入用户环境变量）
```powershell
[Environment]::SetEnvironmentVariable("LLM_API_KEY", "你的key", "User")
[Environment]::SetEnvironmentVariable("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4", "User")
```
设置后需重启终端生效。

## 未配置时的行为

不设 `LLM_API_KEY` 时，系统自动回退到 MockLLMClient：
- case3 跑预设响应（"SOC=0.3 + 夜间禁放"）
- orchestration 的 parse_revision 节点跳过 LLM 调用，直接走结构化 revision
- 控制台打印 `[case3] LLM_API_KEY 未配置，回退 MockLLMClient（预设响应）`

## 三个接入点

| 入口 | 文件 | 说明 |
|------|------|------|
| runner.create_app_context() | `src/energy_agent_v2/runner.py` | 默认调用 create_llm_client()，有 key 就接真实大模型 |
| case3/run_case3.py | `cases/case3_llm_revise/run_case3.py` | 优先真实大模型，回退 mock |
| daily_trial/run_daily.py | `cases/daily_trial/run_daily.py` | --mock 强制 mock，否则同上 |

## create_llm_client() 工厂逻辑
```
1. 读 LLM_API_KEY 和 LLM_BASE_URL
2. 两者都有值 -> 返回 OpenAICompatibleClient（真实大模型）
3. 任一为空 -> 返回 None（runner 回退 mock）
```

## 接口协议

所有 LLM client 实现 `LLMClientProtocol`：
- `chat_json(system_prompt, user_message, few_shot, temperature) -> dict`
- OpenAI 兼容格式，messages = [system, few_shot, user]
- 响应解析：去掉 markdown 代码块标记后 json.loads

## 蒸馏数据

真实大模型解析成功后，自动写入 `data/distillation/revise_pairs_YYYY-MM-DD.jsonl`，记录工程师原文、LLM 解析过程、最终 revision，供 V3 few-shot 微调使用。
