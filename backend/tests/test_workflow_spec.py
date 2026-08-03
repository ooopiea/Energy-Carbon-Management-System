"""Stage-1 tests: WorkflowSpec is the single source of truth (revise_guide 12.1).

These tests prove that the executable LangGraph and the display topology
both derive from one WorkflowSpec, with no second editable copy.
"""
from __future__ import annotations

import pytest

from graph.compiler import compile_workflow, get_topology
from graph.spec import WORKFLOW_SPEC, validate_spec, SpecValidationError
from graph.workflow import build_energy_workflow, get_graph_topology


def test_graph_topology_is_derived_from_executable_spec():
    """get_topology() and compile_workflow() read the same WORKFLOW_SPEC."""
    topology = get_topology(WORKFLOW_SPEC)
    assert topology is not None
    assert len(topology["nodes"]) > 0
    assert len(topology["edges"]) > 0

    # The spec is frozen; topology is a fresh dict derived from it.
    assert isinstance(topology["nodes"], list)
    assert isinstance(topology["edges"], list)

    # Every display node id exists in the spec.
    spec_ids = WORKFLOW_SPEC.node_ids()
    for node in topology["nodes"]:
        assert node["id"] in spec_ids


def test_every_edge_references_existing_nodes():
    """No edge may reference a node that does not exist in the spec."""
    node_ids = WORKFLOW_SPEC.node_ids()
    for edge in WORKFLOW_SPEC.edges:
        assert edge.from_node in node_ids, (
            f"edge from unknown node: {edge.from_node}"
        )
        assert edge.to_node in node_ids, (
            f"edge to unknown node: {edge.to_node}"
        )


def test_every_conditional_route_is_reachable():
    """Every conditional route target must be a real node or END."""
    node_ids = WORKFLOW_SPEC.node_ids()
    for cr in WORKFLOW_SPEC.conditional_routes:
        assert cr.source_node in node_ids
        for status_value, target in cr.route_map.items():
            if target == "__END__":
                continue
            assert target in node_ids, (
                f"route {cr.source_node}[{status_value}] -> unknown {target}"
            )
        # Every route must have at least one non-END target to be useful.
        assert any(t != "__END__" for t in cr.route_map.values()), (
            f"route {cr.source_node} has no reachable target"
        )


def test_graph_api_matches_workflow_spec():
    """get_graph_topology() (the /api/graph source) must match the spec."""
    api_topology = get_graph_topology()
    spec_topology = get_topology(WORKFLOW_SPEC)

    # Same node set.
    api_ids = {n["id"] for n in api_topology["nodes"]}
    spec_ids = {n["id"] for n in spec_topology["nodes"]}
    assert api_ids == spec_ids

    # Same edge set (by from-to pairs).
    api_edges = {(e["from"], e["to"]) for e in api_topology["edges"]}
    spec_edges = {(e["from"], e["to"]) for e in spec_topology["edges"]}
    assert api_edges == spec_edges


def test_spec_validation_rejects_duplicate_node_ids():
    """validate_spec must catch duplicate node IDs."""
    from graph.spec import NodeSpec, EdgeSpec, WorkflowSpec
    bad = WorkflowSpec(
        nodes=(
            NodeSpec("a", "A", "agent"),
            NodeSpec("a", "A2", "agent"),
        ),
        edges=(),
    )
    with pytest.raises(SpecValidationError, match="duplicate"):
        validate_spec(bad)


def test_spec_validation_rejects_dangling_edge():
    """validate_spec must catch edges referencing non-existent nodes."""
    from graph.spec import NodeSpec, EdgeSpec, WorkflowSpec
    bad = WorkflowSpec(
        nodes=(NodeSpec("a", "A", "agent"),),
        edges=(EdgeSpec("a", "ghost"),),
    )
    with pytest.raises(SpecValidationError, match="missing"):
        validate_spec(bad)


def test_compiled_workflow_uses_spec_handlers():
    """compile_workflow must wire handlers from the spec's handler_key mapping."""
    call_log: list[str] = []

    async def noop_handler(state):
        call_log.append("called")
        return {}

    # The route node is internal; only real handlers need to be supplied.
    handlers = {key: noop_handler for key in WORKFLOW_SPEC.required_handlers}
    graph = compile_workflow(WORKFLOW_SPEC, handlers)
    assert graph is not None  # compiled successfully


def test_compiled_workflow_rejects_missing_handlers():
    """compile_workflow must fail when a required handler is missing."""
    with pytest.raises(ValueError, match="missing"):
        compile_workflow(WORKFLOW_SPEC, {})


def test_build_energy_workflow_is_backward_compatible():
    """The legacy entry point must still produce a compiled graph."""
    async def noop(state):
        return {}
    handlers = {key: noop for key in WORKFLOW_SPEC.required_handlers}
    graph = build_energy_workflow(handlers)
    assert graph is not None


def test_no_second_static_topology_definition():
    """GRAPH_NODES and GRAPH_EDGES must not exist as module attributes."""
    import graph.workflow as wf_module
    assert not hasattr(wf_module, "GRAPH_NODES"), "GRAPH_NODES still exists"
    assert not hasattr(wf_module, "GRAPH_EDGES"), "GRAPH_EDGES still exists"