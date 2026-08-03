"""Compile a WorkflowSpec into a LangGraph executable and export display topology.

Both compile_workflow() and get_topology() read the same WorkflowSpec instance,
eliminating the dual-definition gap (G1).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from core.state import EnergySystemState
from graph.spec import EdgeSpec, WorkflowSpec, validate_spec


NodeHandler = Callable[[EnergySystemState], Awaitable[dict[str, Any]]]


def get_topology(spec: WorkflowSpec) -> dict[str, Any]:
    """Export the display topology derived from the spec."""
    return spec.to_topology()


def compile_workflow(
    spec: WorkflowSpec,
    handlers: dict[str, NodeHandler],
) -> Any:
    """Build and compile a LangGraph StateGraph from the spec.

    The compiled graph uses the spec's handler_key to look up the actual
    coroutine in *handlers*, and the spec's graph_name as the LangGraph node
    identifier.  Conditional routes are wired via add_conditional_edges.
    """
    validate_spec(spec)

    # Ensure every required handler is present.
    missing = spec.required_handlers.difference(handlers)
    if missing:
        raise ValueError(f"missing workflow handlers: {sorted(missing)}")

    graph = StateGraph(EnergySystemState)

    # ---- register nodes -------------------------------------------------
    async def _route_passthrough(_: EnergySystemState) -> dict[str, Any]:
        return {}

    for node in spec.nodes:
        if node.handler_key is None:
            continue
        if node.handler_key == "__route__":
            graph.add_node(node.graph_name or node.node_id, _route_passthrough)
        else:
            handler = handlers[node.handler_key]
            graph.add_node(node.graph_name or node.node_id, handler)

    # ---- START -> route node -------------------------------------------
    route_specs = spec.conditional_routes
    if route_specs:
        route_node = route_specs[0].source_node
        route_spec = spec.get_node(route_node)
        route_name = route_spec.graph_name or route_node
        graph.add_edge(START, route_name)

        # ---- conditional edges from route -------------------------------
        def _make_router(cr):
            def router(state: EnergySystemState) -> str:
                status = state.get(cr.status_field, "new")
                return status if status in cr.route_map else "wait"
            return router

        for cr in route_specs:
            route_target_map: dict[str, str] = {}
            for status_value, target in cr.route_map.items():
                if target == "__END__":
                    route_target_map[status_value] = END
                else:
                    target_spec = spec.get_node(target)
                    route_target_map[status_value] = target_spec.graph_name or target
            graph.add_conditional_edges(
                route_name,
                _make_router(cr),
                route_target_map,
            )
    else:
        # If no conditional route, connect START to the first executable node.
        exec_nodes = spec.executable_nodes()
        if exec_nodes:
            first = exec_nodes[0]
            graph.add_edge(START, first.graph_name or first.node_id)

    # ---- sequential execution edges ------------------------------------
    for edge in spec.edges:
        if not edge.is_execution_edge:
            continue
        from_spec = spec.get_node(edge.from_node)
        to_spec = spec.get_node(edge.to_node)
        from_name = from_spec.graph_name or from_spec.node_id
        to_name = to_spec.graph_name or to_spec.node_id
        graph.add_edge(from_name, to_name)

    # ---- terminal nodes -> END -----------------------------------------
    # Any executable node that has no outgoing execution edge or conditional
    # route must connect to END to keep the graph well-formed.
    has_outgoing: set[str] = set()
    for cr in spec.conditional_routes:
        has_outgoing.add(cr.source_node)
    for edge in spec.edges:
        if edge.is_execution_edge:
            has_outgoing.add(edge.from_node)

    for node in spec.executable_nodes():
        node_name = node.graph_name or node.node_id
        if node_name not in has_outgoing and node.handler_key != "__route__":
            graph.add_edge(node_name, END)

    return graph.compile()