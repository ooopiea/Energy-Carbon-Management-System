"""Context broker: builds role-filtered agent contexts from a single snapshot (MA-1)."""
from __future__ import annotations

from collaboration.contracts import (
    AgentContext,
    AgentRole,
    AgentTask,
    OperationalSnapshot,
)


# Role-based fact whitelist: each agent only sees fields relevant to its role.
FACT_WHITELIST: dict[str, set[str]] = {
    AgentRole.DATA: {
        "data_provenance", "data_timeline", "load_kw", "solar_kw",
        "weather", "day_ahead",
    },
    AgentRole.STORAGE: {
        "storage_soc", "storage_power_kw", "storage_temp_c",
        "storage_summary", "load_kw", "solar_kw",
        "day_ahead", "carbon_dispatch", "price", "tariff_period",
    },
    AgentRole.HVAC: {
        "hvac_power_kw", "hvac_supply_temp_c", "hvac_return_temp_c",
        "hvac_summary", "weather", "day_ahead",
        "chiller_topology", "price", "tariff_period",
    },
    AgentRole.RISK: {
        "storage_soc", "storage_temp_c", "storage_power_kw",
        "hvac_supply_temp_c", "hvac_power_kw", "alerts",
        "approval_gates", "workflow", "daily",
        "physical_dispatch",
    },
    AgentRole.MONITOR: {
        "agent_nodes", "alerts", "physical_dispatch",
        "workflow", "approval_gates",
    },
}


class ContextBroker:
    """Builds agent-specific contexts by filtering a shared snapshot."""

    def build(
        self,
        agent_id: str,
        task: AgentTask,
        snapshot: OperationalSnapshot,
        peer_results: list[dict] | None = None,
    ) -> AgentContext:
        whitelist = FACT_WHITELIST.get(agent_id, set())
        visible = {
            key: value
            for key, value in snapshot.facts.items()
            if key in whitelist
        }
        return AgentContext(
            agent_id=agent_id,
            task=task,
            snapshot=snapshot,
            visible_facts=visible,
            peer_results=peer_results or [],
        )
