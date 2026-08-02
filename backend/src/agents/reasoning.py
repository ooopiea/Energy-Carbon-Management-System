"""Optional GLM narrative layer for deterministic LangGraph agents."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import AGENTS_CONFIG_DIR
from llm.glm_client import GlmClient, GlmError


class AgentReasoningService:
    """Turns verified facts into operator-facing language without owning decisions."""

    def __init__(self, glm: GlmClient | None = None, config_dir: Path = AGENTS_CONFIG_DIR):
        self.glm = glm or GlmClient()
        self.config_dir = config_dir
        self.last_error: str | None = None

    async def explain(
        self,
        agent_name: str,
        fallback: str,
        facts: dict[str, Any],
        task: str,
    ) -> tuple[str, dict[str, Any]]:
        if not self.glm.configured:
            return fallback, {"mode": "deterministic_fallback", "configured": False}
        config_path = self.config_dir / f"{agent_name}_agent.md"
        role_config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        messages = [
            {
                "role": "system",
                "content": (
                    "你是工业能源管理 LangGraph 节点的解释层。算法结果和物理约束是唯一事实源；"
                    "不得修改数值、发明依据或宣称已执行。用中文输出2到4句，先结论，再风险与下一步。\n"
                    f"节点配置：\n{role_config[:5000]}"
                ),
            },
            {
                "role": "user",
                "content": f"任务：{task}\n已验证事实：{json.dumps(facts, ensure_ascii=False, default=str)}",
            },
        ]
        try:
            result = await self.glm.complete(messages, max_tokens=420, temperature=0.15)
            content = result.content.strip() or fallback
            self.last_error = None
            return content, {
                "mode": "glm",
                "configured": True,
                "model": self.glm.config.model,
            }
        except GlmError as exc:
            self.last_error = str(exc)
            return fallback, {
                "mode": "deterministic_fallback",
                "configured": True,
                "model": self.glm.config.model,
                "error": str(exc),
            }

    def status(self) -> dict[str, Any]:
        return {**self.glm.status(), "last_error": self.last_error}
