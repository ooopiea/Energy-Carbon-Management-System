"""LLM agent 公共基类：加载 prompt + 调用 LLM 并带 JSON schema 约束"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from energy_agent_v2.llm.client import LLMClientProtocol, MockLLMClient

_DEFAULT_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


class BaseLLMAgent:
    """所有 LLM agent 的公共基类：负责 prompt 加载 + JSON schema 双层约束"""

    _prompt_name: str = ""
    _schema: dict[str, Any] = {}

    def __init__(
        self,
        client: LLMClientProtocol | None = None,
        prompt_dir: Path | None = None,
    ) -> None:
        self.client = client or MockLLMClient()
        self.prompt_dir = prompt_dir or _DEFAULT_PROMPT_DIR
        self._system_prompt = self._load_prompt()

    @property
    def llm_model(self) -> str:
        return getattr(self.client, "model", "mock")

    def _load_prompt(self) -> str:
        path = self.prompt_dir / self._prompt_name
        if path.exists():
            return path.read_text(encoding="utf-8")
        return f"你是 {self.__class__.__name__}，请输出合法 JSON。"

    def _call_llm(
        self, user_message: str, *, few_shot: str = "", temperature: float = 0.1,
    ) -> dict[str, Any]:
        return self.client.chat_json(
            system_prompt=self._system_prompt,
            user_message=user_message,
            few_shot=few_shot,
            temperature=temperature,
            response_schema=self._schema,
        )
