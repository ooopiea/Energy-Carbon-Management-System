"""Executable LangGraph workflow and topology, both derived from a single WorkflowSpec.

G1 fix: GRAPH_NODES / GRAPH_EDGES and build_energy_workflow() have been merged
into one WorkflowSpec.  This module re-exports the compiler and topology
functions for backward compatibility with engine.py and api/main.py.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from graph.compiler import NodeHandler, compile_workflow as _compile_workflow
from graph.compiler import get_topology as _get_topology
from graph.spec import WORKFLOW_SPEC


def build_energy_workflow(handlers: dict[str, NodeHandler]):
    """Build and compile the workflow from the single WORKFLOW_SPEC."""
    return _compile_workflow(WORKFLOW_SPEC, handlers)


def get_graph_topology() -> dict[str, Any]:
    """Return the display topology derived from WORKFLOW_SPEC (for /api/graph)."""
    return _get_topology(WORKFLOW_SPEC)


__all__ = [
    "WORKFLOW_SPEC",
    "build_energy_workflow",
    "get_graph_topology",
    "NodeHandler",
]
