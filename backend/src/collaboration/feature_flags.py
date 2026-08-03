"""Feature flags for multi-agent coordination path switching (revise_guide §10.9, §12.7).

Three-state flag: template | shadow_multi_agent | multi_agent
- template: existing coordinate_agents() template path (default)
- shadow_multi_agent: new path runs but never produces physical side effects
- multi_agent: new path is active and may create controlled proposals
"""
from __future__ import annotations

import os

from enum import StrEnum


class AgentMode(StrEnum):
    TEMPLATE = "template"
    SHADOW = "shadow_multi_agent"
    MULTI_AGENT = "multi_agent"


DEFAULT_MODE = AgentMode.TEMPLATE


def get_agent_mode() -> AgentMode:
    """Read the current multi-agent feature flag from the environment."""
    raw = os.getenv("ENERGY_AGENT_MODE", DEFAULT_MODE.value).strip().lower()
    for mode in AgentMode:
        if mode.value == raw:
            return mode
    return DEFAULT_MODE


def is_shadow_mode() -> bool:
    return get_agent_mode() == AgentMode.SHADOW


def is_multi_agent_active() -> bool:
    return get_agent_mode() == AgentMode.MULTI_AGENT


def is_multi_agent_enabled() -> bool:
    """True if either shadow or full multi-agent mode is on."""
    return get_agent_mode() in (AgentMode.SHADOW, AgentMode.MULTI_AGENT)
