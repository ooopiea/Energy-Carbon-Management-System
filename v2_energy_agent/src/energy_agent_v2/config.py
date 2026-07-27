from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class AppConfig(BaseModel):
    site_id: str = "huanghua"
    region: str = "cn-hunan"
    data_dir: Path = Path("data")
    db_uri: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str = "glm-5"
    llm_mode: str = "fallback"
    llm_parse_confidence_threshold: float = 0.7
    llm_parse_max_clarify_rounds: int = 2
    llm_parse_timeout_seconds: int = 30
    prompt_dir: Path = Path("src/energy_agent_v2/prompts")
    distillation_dir: Path = Path("data/distillation")


def default_config() -> AppConfig:
    return AppConfig()
