"""Small async client for Zhipu's official OpenAI-compatible Chat API."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


class GlmError(RuntimeError):
    pass


@dataclass(frozen=True)
class GlmConfig:
    api_key: str = ""
    base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    model: str = "glm-5.2"
    timeout_seconds: float = 45.0
    max_retries: int = 2
    thinking: str = "enabled"

    @classmethod
    def from_env(cls) -> "GlmConfig":
        file_values = _read_project_env()
        def value(name: str, default: str = "") -> str:
            return os.getenv(name, file_values.get(name, default))
        return cls(
            api_key=(
                value("ZAI_API_KEY")
                or value("ZHIPU_API_KEY")
                or value("GLM_API_KEY")
                or ""
            ).strip(),
            base_url=value("GLM_BASE_URL", cls.base_url).strip(),
            model=value("GLM_MODEL", cls.model).strip(),
            timeout_seconds=float(value("GLM_TIMEOUT_SECONDS", "45")),
            max_retries=max(0, int(value("GLM_MAX_RETRIES", "2"))),
            thinking=value("GLM_THINKING", "enabled").strip().lower(),
        )


def _read_project_env() -> dict[str, str]:
    """Read the project-local .env without mutating process environment."""
    path = Path(__file__).resolve().parents[3] / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            parsed = raw_value.strip()
            if len(parsed) >= 2 and parsed[0] == parsed[-1] and parsed[0] in {'"', "'"}:
                parsed = parsed[1:-1]
            values[key] = parsed
    except OSError:
        return {}
    return values


@dataclass(frozen=True)
class GlmToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class GlmMessage:
    content: str = ""
    tool_calls: list[GlmToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def as_assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message


class GlmClient:
    """OpenAI-compatible HTTP boundary with bounded retry and safe diagnostics."""

    def __init__(
        self,
        config: GlmConfig | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.config = config or GlmConfig.from_env()
        self._client = http_client

    @property
    def configured(self) -> bool:
        return bool(self.config.api_key)

    def status(self) -> dict[str, Any]:
        return {
            "provider": "zhipu",
            "configured": self.configured,
            "model": self.config.model,
            "base_url": self.config.base_url,
            "thinking": self.config.thinking,
        }

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int = 1200,
        temperature: float = 0.2,
    ) -> GlmMessage:
        if not self.configured:
            raise GlmError("GLM API Key 未配置")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": max(0.01, min(0.99, temperature)),
            "thinking": {"type": "enabled" if self.config.thinking == "enabled" else "disabled"},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        try:
            for attempt in range(self.config.max_retries + 1):
                try:
                    response = await client.post(endpoint, headers=headers, json=payload)
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < self.config.max_retries:
                        await asyncio.sleep(0.4 * (2 ** attempt))
                        continue
                    response.raise_for_status()
                    return self._parse_response(response.json())
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt >= self.config.max_retries:
                        raise GlmError(f"GLM 网络请求失败: {type(exc).__name__}") from exc
                    await asyncio.sleep(0.4 * (2 ** attempt))
                except httpx.HTTPStatusError as exc:
                    request_id = exc.response.headers.get("x-request-id", "unknown")
                    raise GlmError(
                        f"GLM 接口返回 HTTP {exc.response.status_code} (request_id={request_id})"
                    ) from exc
        finally:
            if owns_client:
                await client.aclose()
        raise GlmError("GLM 请求未产生响应")

    @staticmethod
    def _parse_response(payload: dict[str, Any]) -> GlmMessage:
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GlmError("GLM 响应缺少 choices[0].message") from exc
        calls: list[GlmToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            arguments = function.get("arguments") or "{}"
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise GlmError(f"GLM 工具参数不是有效 JSON: {function.get('name', 'unknown')}") from exc
            calls.append(
                GlmToolCall(
                    id=str(raw_call.get("id") or "tool-call"),
                    name=str(function.get("name") or ""),
                    arguments=parsed,
                )
            )
        return GlmMessage(content=str(message.get("content") or ""), tool_calls=calls, raw=message)
