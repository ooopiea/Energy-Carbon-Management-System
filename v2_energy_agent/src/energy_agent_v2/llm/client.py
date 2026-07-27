"""OpenAI-compatible LLM client for GLM-5 (ZhipuAI).

Provides a Protocol-based interface so tests can inject MockLLMClient
without depending on the real API.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Minimal interface every LLM client must satisfy."""

    def chat_json(
        self,
        system_prompt: str,
        user_message: str,
        *,
        few_shot: str = "",
        temperature: float = 0.1,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


class OpenAICompatibleClient:
    """Calls GLM-5 via OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "glm-5",
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.base_url = base_url or os.environ.get("LLM_BASE_URL", "")
        self.model = model
        self.timeout = timeout

    def chat_json(
        self,
        system_prompt: str,
        user_message: str,
        *,
        few_shot: str = "",
        temperature: float = 0.1,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
        sys_msg = system_prompt
        kw: dict[str, Any] = {}
        if response_schema is not None:
            sys_msg += "\n\n## 强制输出约束\n你必须且只能输出一个合法 JSON 对象，禁止输出 markdown。\n输出必须严格符合以下 JSON Schema：\n" + json.dumps(response_schema, ensure_ascii=False, indent=2)
            kw["response_format"] = {"type": "json_object"}
        messages: list[dict[str, str]] = [{"role": "system", "content": sys_msg}]
        if few_shot:
            messages.append({"role": "system", "content": few_shot})
        messages.append({"role": "user", "content": user_message})

        resp = client.chat.completions.create(
            model=self.model, messages=messages, temperature=temperature, **kw,
        )
        content = resp.choices[0].message.content or ""
        return self._parse_json(content)

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        """Robustly parse LLM JSON output: strip markdown fences,
        extract the outermost JSON object if extra text is present."""
        import re

        content = content.strip()
        # Strip markdown code fences
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content.rsplit("```", 1)[0]
            content = content.strip()
        # Try direct parse first
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # Extract outermost { ... } block (GLM sometimes wraps JSON with explanation text)
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        # Last resort: fix common issues (trailing commas, smart quotes)
        cleaned = content.replace("\u201c", '"').replace("\u201d", '"')
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        return json.loads(cleaned)


class MockLLMClient:
    """Deterministic mock for unit tests."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self._responses = list(responses) if responses else []
        self._idx = 0
        self.call_log: list[dict[str, str]] = []

    def chat_json(
        self, system_prompt: str, user_message: str, *, few_shot: str = "", temperature: float = 0.1,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.call_log.append({"system": system_prompt[:80], "user": user_message[:120]})
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return {"slots": {}}

    def reset(self) -> None:
        self._idx = 0
        self.call_log.clear()


def create_llm_client() -> LLMClientProtocol | None:
    """工厂函数：先加载 .env 文件，再检测环境变量。

    环境变量：
        LLM_API_KEY  — 智谱 API key（必填，缺失则回退 mock）
        LLM_BASE_URL — OpenAI 兼容端点（必填，缺失则回退 mock）
        LLM_MODEL    — 模型名（默认 glm-5）
        LLM_TIMEOUT  — 超时秒数（默认 30）
    """
    # 从 .env 文件加载（自动查找项目根目录的 .env）
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv 未安装则跳过，仅依赖系统环境变量
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    if not api_key or not base_url:
        logger.warning(
            "LLM_API_KEY 或 LLM_BASE_URL 未设置，parse_revision 回退到 mock 模式。"
            "设置环境变量后可接入真实 GLM-5。"
        )
        return None
    model = os.environ.get("LLM_MODEL", "glm-5")
    timeout = int(os.environ.get("LLM_TIMEOUT", "30"))
    logger.info("LLM 接入: model=%s base_url=%s", model, base_url)
    return OpenAICompatibleClient(
        api_key=api_key, base_url=base_url, model=model, timeout=timeout,
    )
