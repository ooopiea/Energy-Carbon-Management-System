"""Single source of truth for workflow nodes, edges, routing and display metadata.

This module replaces the former dual definition (GRAPH_NODES/GRAPH_EDGES for
display + build_energy_workflow for execution).  Both compile_workflow() and
get_topology() read the WORKFLOW_SPEC defined here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Spec primitives
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeSpec:
    """One node in the workflow, carrying both display and execution metadata."""

    node_id: str
    label: str
    node_type: str                       # start / end / agent / approval / physical / database / route
    x: int = 0
    y: int = 0
    agent: str | None = None             # agent type key, e.g. "data"
    tools: tuple[str, ...] = ()
    description: str = ""
    # Execution metadata (None for pure display nodes)
    handler_key: str | None = None       # key in the handlers dict passed to compile_workflow
    graph_name: str | None = None        # node name inside the compiled LangGraph


@dataclass(frozen=True)
class EdgeSpec:
    """One edge in the workflow."""

    from_node: str
    to_node: str
    edge_type: str = "normal"            # normal / approve / reject / revise / monitor / bidirectional
    style: str = "solid"                 # solid / dashed / dotted
    label: str = ""
    # Execution metadata
    is_execution_edge: bool = False      # True = appears in compiled LangGraph
    condition_key: str | None = None     # route key for conditional edges


@dataclass(frozen=True)
class ConditionalRouteSpec:
    """Conditional routing from a source node based on a state field."""

    source_node: str
    status_field: str                    # which state field to read
    route_map: dict[str, str]            # status value -> target node_id (or "__END__")


@dataclass(frozen=True)
class WorkflowSpec:
    """Frozen, validated single source of truth."""

    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    conditional_routes: tuple[ConditionalRouteSpec, ...] = ()
    required_handlers: frozenset[str] = field(default_factory=frozenset)

    # -- lookup helpers ---------------------------------------------------

    def node_ids(self) -> set[str]:
        return {n.node_id for n in self.nodes}

    def get_node(self, node_id: str) -> NodeSpec:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(node_id)

    def executable_nodes(self) -> list[NodeSpec]:
        """Nodes that have a handler_key (appear in the compiled graph)."""
        return [n for n in self.nodes if n.handler_key is not None]

    def edges_from(self, node_id: str) -> list[EdgeSpec]:
        return [e for e in self.edges if e.from_node == node_id]

    def to_topology(self) -> dict[str, Any]:
        """Export display-friendly topology for /api/graph."""
        return {
            "nodes": [
                {
                    "id": n.node_id,
                    "label": n.label,
                    "type": n.node_type,
                    "agent": n.agent,
                    "x": n.x,
                    "y": n.y,
                    **({"tools": list(n.tools)} if n.tools else {}),
                    **({"description": n.description} if n.description else {}),
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "from": e.from_node,
                    "to": e.to_node,
                    "type": e.edge_type,
                    "style": e.style,
                    **({"label": e.label} if e.label else {}),
                }
                for e in self.edges
                if not (
                    e.is_execution_edge
                    and any(
                        not d.is_execution_edge
                        and d.from_node == e.from_node
                        and d.to_node == e.to_node
                        for d in self.edges
                    )
                )
            ],
        }


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------

class SpecValidationError(ValueError):
    pass


def validate_spec(spec: WorkflowSpec) -> None:
    """Raise if the spec violates structural integrity rules."""
    node_ids = spec.node_ids()

    # 1. Node IDs unique
    if len(node_ids) != len(spec.nodes):
        seen: set[str] = set()
        for n in spec.nodes:
            if n.node_id in seen:
                raise SpecValidationError(f"duplicate node id: {n.node_id}")
            seen.add(n.node_id)

    # 2. All edges reference existing nodes
    for e in spec.edges:
        for endpoint, role in ((e.from_node, "from"), (e.to_node, "to")):
            if endpoint not in node_ids:
                raise SpecValidationError(
                    f"edge {e.from_node}->{e.to_node} references missing {role} node: {endpoint}"
                )

    # 3. Conditional routes are complete and reachable
    for cr in spec.conditional_routes:
        if cr.source_node not in node_ids:
            raise SpecValidationError(
                f"conditional route source {cr.source_node} is not a known node"
            )
        for status_value, target in cr.route_map.items():
            if target == "__END__":
                continue
            if target not in node_ids:
                raise SpecValidationError(
                    f"conditional route {cr.source_node}[{status_value}] targets unknown node {target}"
                )
        source_node = spec.get_node(cr.source_node)
        if source_node.handler_key is None:
            raise SpecValidationError(
                f"conditional route source {cr.source_node} has no handler and cannot execute"
            )

    # 4. Every required handler key has a node
    handler_keys = {n.handler_key for n in spec.nodes if n.handler_key}
    missing = spec.required_handlers - handler_keys
    if missing:
        raise SpecValidationError(
            f"required handler keys not found in spec nodes: {sorted(missing)}"
        )

    # 5. No unreachable execution nodes (each must be reachable via edges or routes)
    reachable: set[str] = set()
    for cr in spec.conditional_routes:
        for target in cr.route_map.values():
            if target != "__END__":
                reachable.add(target)
    for e in spec.edges:
        if e.is_execution_edge:
            reachable.add(e.to_node)
    # route source itself is reachable from START
    for cr in spec.conditional_routes:
        reachable.add(cr.source_node)

    unreachable_exec = {n.node_id for n in spec.executable_nodes()} - reachable
    # The route node is reachable by convention (START -> route)
    unreachable_exec.discard(
        spec.conditional_routes[0].source_node if spec.conditional_routes else ""
    )
    if unreachable_exec:
        raise SpecValidationError(
            f"execution nodes unreachable via edges or routes: {sorted(unreachable_exec)}"
        )

    # 6. Approval nodes must have approve edges leading toward physical dispatch
    approval_nodes = {n.node_id for n in spec.nodes if n.node_type == "approval"}
    physical_nodes = {n.node_id for n in spec.nodes if n.node_type == "physical"}
    if approval_nodes and physical_nodes:
        # Every approval node must have at least one outgoing "approve" edge
        for ap in approval_nodes:
            outgoing = spec.edges_from(ap)
            if not any(e.edge_type == "approve" for e in outgoing):
                raise SpecValidationError(
                    f"approval node {ap} has no 'approve' edge to the next stage"
                )


# ---------------------------------------------------------------------------
# The actual v3 workflow specification
# ---------------------------------------------------------------------------

WORKFLOW_SPEC = WorkflowSpec(
    nodes=(
        NodeSpec(
            node_id="start", label="开始", node_type="start",
            x=400, y=40,
        ),
        NodeSpec(
            node_id="data_agent", label="数据 Agent", node_type="agent", agent="data",
            x=400, y=140,
            tools=("数据质量检查", "日前数据汇总", "数据封存"),
            description="汇总厂区实时生产数据，检查并整理为标准格式进行实时封存",
            handler_key="data_agent", graph_name="run_data_agent",
        ),
        NodeSpec(
            node_id="prediction_agent", label="负荷处理 Agent", node_type="agent", agent="prediction",
            x=400, y=260,
            tools=("质量校验", "时间粒度转换"),
            description="校验实测负荷，并在输入较粗时转换为15分钟粒度；暂不外推未来负荷",
            handler_key="prediction_agent", graph_name="run_prediction_agent",
        ),
        NodeSpec(
            node_id="forecast_approval", label="工程师审批", node_type="approval",
            x=400, y=380,
            description="审批负荷处理与粒度转换报告",
            handler_key="forecast_approval", graph_name="open_forecast_gate",
        ),
        NodeSpec(
            node_id="storage_agent", label="储能 Agent", node_type="agent", agent="storage",
            x=200, y=500,
            tools=("MILP调度优化", "状态监测"),
            description="日前储能充放电优化调度 + 实时响应",
            handler_key="storage_agent", graph_name="run_storage_agent",
        ),
        NodeSpec(
            node_id="hvac_agent", label="HVAC Agent", node_type="agent", agent="hvac",
            x=600, y=500,
            tools=("冷机群调度", "供回水温约束"),
            description="日前冷机群调度优化 + 实时响应",
            handler_key="hvac_agent", graph_name="run_hvac_agent",
        ),
        NodeSpec(
            node_id="dispatch_approvals", label="双审批汇合", node_type="approval",
            x=400, y=620,
            description="储能和HVAC调度策略双审批汇合",
            handler_key="dispatch_approvals", graph_name="open_dispatch_gates",
        ),
        NodeSpec(
            node_id="physical_dispatch", label="物理调度", node_type="physical",
            x=400, y=740,
            description="经审批策略下发至物理调度端执行",
            handler_key="physical_dispatch", graph_name="activate_physical_dispatch",
        ),
        NodeSpec(
            node_id="end", label="结束", node_type="end",
            x=400, y=840,
        ),
        NodeSpec(
            node_id="monitor_agent", label="监察 Agent", node_type="agent", agent="monitor",
            x=50, y=380,
            tools=("心跳检测", "报告审批", "数据库监察"),
            description="确保Agent系统与数据封存稳步进行，保障系统运行正常",
            handler_key="monitor_agent", graph_name="run_monitor_agent",
        ),
        NodeSpec(
            node_id="database", label="数据库 / 状态文件库", node_type="database",
            x=750, y=260,
            description="物理知识约束（温度/SOC范围）、历史数据、封存数据",
        ),
        # Internal route node (not displayed in the frontend SVG but part of the executable graph)
        NodeSpec(
           node_id="route", label="路由", node_type="route",
           x=400, y=20,
           handler_key="__route__", graph_name="route",
        ),
        # Fan-out node: after forecast approval, dispatches storage + HVAC in parallel
        NodeSpec(
            node_id="_fanout_day_ahead", label="日前并行分发", node_type="route",
            x=400, y=440,
            handler_key="__route__", graph_name="_fanout_day_ahead",
        ),
    ),
    edges=(
        # --- display edges (for frontend rendering) ---
        EdgeSpec("start", "data_agent", "normal"),
        EdgeSpec("data_agent", "prediction_agent", "normal", label="日前数据"),
        EdgeSpec("prediction_agent", "forecast_approval", "normal", label="负荷处理报告"),
        EdgeSpec("forecast_approval", "storage_agent", "approve", label="批准"),
        EdgeSpec("forecast_approval", "hvac_agent", "approve", label="批准"),
        EdgeSpec("storage_agent", "dispatch_approvals", "normal", label="调度策略"),
        EdgeSpec("hvac_agent", "dispatch_approvals", "normal", label="调度策略"),
        EdgeSpec("dispatch_approvals", "physical_dispatch", "approve", label="批准"),
        EdgeSpec("physical_dispatch", "end", "normal"),
        # database connections
        EdgeSpec("data_agent", "database", "bidirectional", style="dashed"),
        EdgeSpec("prediction_agent", "database", "bidirectional", style="dashed"),
        EdgeSpec("storage_agent", "database", "bidirectional", style="dashed"),
        EdgeSpec("hvac_agent", "database", "bidirectional", style="dashed"),
        # monitor connections
        EdgeSpec("monitor_agent", "data_agent", "monitor", style="dotted"),
        EdgeSpec("monitor_agent", "prediction_agent", "monitor", style="dotted"),
        EdgeSpec("monitor_agent", "storage_agent", "monitor", style="dotted"),
        EdgeSpec("monitor_agent", "hvac_agent", "monitor", style="dotted"),
        EdgeSpec("monitor_agent", "database", "monitor", style="dotted"),
        # --- execution edges (inside compiled LangGraph, not displayed) ---
        EdgeSpec("data_agent", "prediction_agent", "normal", is_execution_edge=True),
        EdgeSpec("prediction_agent", "forecast_approval", "normal", is_execution_edge=True),
        # Parallel: fan-out dispatches both agents simultaneously
        EdgeSpec("_fanout_day_ahead", "storage_agent", "normal", is_execution_edge=True),
        EdgeSpec("_fanout_day_ahead", "hvac_agent", "normal", is_execution_edge=True),
        # Both branches converge to dispatch_approvals (join)
        EdgeSpec("storage_agent", "dispatch_approvals", "normal", is_execution_edge=True),
        EdgeSpec("hvac_agent", "dispatch_approvals", "normal", is_execution_edge=True),
        EdgeSpec("physical_dispatch", "monitor_agent", "normal", is_execution_edge=True),
    ),
    conditional_routes=(
        ConditionalRouteSpec(
            source_node="route",
            status_field="workflow_status",
            route_map={
                "new": "data_agent",
                "forecast_approved": "_fanout_day_ahead",
                "forecast_revision": "prediction_agent",
                "storage_revision": "storage_agent",
                "hvac_revision": "hvac_agent",
                "dispatch_approved": "physical_dispatch",
                "wait": "__END__",
            },
        ),
    ),
    required_handlers=frozenset({
        "data_agent", "prediction_agent", "forecast_approval",
        "storage_agent", "hvac_agent", "dispatch_approvals",
        "physical_dispatch", "monitor_agent",
    }),
)

# Validate at import time so spec errors surface immediately.
validate_spec(WORKFLOW_SPEC)
