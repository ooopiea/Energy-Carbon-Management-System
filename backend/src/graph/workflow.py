"""Executable LangGraph workflow plus the topology consumed by the dashboard.

图结构（对齐 guide.md 中的 mermaid 图）：
START → data_agent → prediction_agent → forecast_approval
  → [approve] storage_agent + hvac_agent
  → storage_approval + hvac_approval
  → [approve] physical_dispatch → END
  monitor_agent 监察所有节点

SimulationEngine supplies domain handlers; this module owns orchestration and compile.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from core.state import EnergySystemState


NodeHandler = Callable[[EnergySystemState], Awaitable[dict[str, Any]]]

# 节点拓扑定义（供前端渲染工作流图）
GRAPH_NODES = [
    {
        "id": "start",
        "label": "开始",
        "type": "start",
        "agent": None,
        "x": 400, "y": 40,
    },
    {
        "id": "data_agent",
        "label": "数据 Agent",
        "type": "agent",
        "agent": "data",
        "x": 400, "y": 140,
        "tools": ["数据质量检查", "日前数据汇总", "数据封存"],
        "description": "汇总厂区实时生产数据，检查并整理为标准格式进行实时封存",
    },
    {
        "id": "prediction_agent",
        "label": "预测 Agent",
        "type": "agent",
        "agent": "prediction",
        "x": 400, "y": 260,
        "tools": ["负荷预测模型"],
        "description": "结合排班、气象、近日数据进行日前负荷预测",
    },
    {
        "id": "forecast_approval",
        "label": "工程师审批",
        "type": "approval",
        "agent": None,
        "x": 400, "y": 380,
        "description": "审批负荷预测报告",
    },
    {
        "id": "storage_agent",
        "label": "储能 Agent",
        "type": "agent",
        "agent": "storage",
        "x": 200, "y": 500,
        "tools": ["MILP调度优化", "状态监测"],
        "description": "日前储能充放电优化调度 + 实时响应",
    },
    {
        "id": "hvac_agent",
        "label": "HVAC Agent",
        "type": "agent",
        "agent": "hvac",
        "x": 600, "y": 500,
        "tools": ["冷机群调度", "供回水温约束"],
        "description": "日前冷机群调度优化 + 实时响应",
    },
    {
        "id": "storage_approval",
        "label": "工程师审批",
        "type": "approval",
        "agent": None,
        "x": 200, "y": 620,
        "description": "审批储能调度策略",
    },
    {
        "id": "hvac_approval",
        "label": "工程师审批",
        "type": "approval",
        "agent": None,
        "x": 600, "y": 620,
        "description": "审批HVAC调度策略",
    },
    {
        "id": "physical_dispatch",
        "label": "物理调度",
        "type": "physical",
        "agent": None,
        "x": 400, "y": 740,
        "description": "经审批策略下发至物理调度端执行",
    },
    {
        "id": "end",
        "label": "结束",
        "type": "end",
        "agent": None,
        "x": 400, "y": 840,
    },
    {
        "id": "database",
        "label": "数据库 / 状态文件库",
        "type": "database",
        "agent": None,
        "x": 750, "y": 260,
        "description": "物理知识约束（温度/SOC范围）、历史数据、封存数据",
    },
    {
        "id": "monitor_agent",
        "label": "监察 Agent",
        "type": "agent",
        "agent": "monitor",
        "x": 50, "y": 380,
        "tools": ["心跳检测", "报告审批", "数据库监察"],
        "description": "确保Agent系统与数据封存稳步进行，保障系统运行正常",
    },
]

GRAPH_EDGES = [
    {"from": "start", "to": "data_agent", "type": "normal"},
    {"from": "data_agent", "to": "prediction_agent", "type": "normal", "label": "日前数据"},
    {"from": "prediction_agent", "to": "forecast_approval", "type": "normal", "label": "预测报告"},
    {"from": "forecast_approval", "to": "storage_agent", "type": "approve", "label": "批准"},
    {"from": "forecast_approval", "to": "hvac_agent", "type": "approve", "label": "批准"},
    {"from": "storage_agent", "to": "storage_approval", "type": "normal", "label": "调度策略"},
    {"from": "hvac_agent", "to": "hvac_approval", "type": "normal", "label": "调度策略"},
    {"from": "storage_approval", "to": "physical_dispatch", "type": "approve", "label": "批准"},
    {"from": "hvac_approval", "to": "physical_dispatch", "type": "approve", "label": "批准"},
    {"from": "physical_dispatch", "to": "end", "type": "normal"},
    # 数据库连接
    {"from": "data_agent", "to": "database", "type": "bidirectional", "style": "dashed"},
    {"from": "prediction_agent", "to": "database", "type": "bidirectional", "style": "dashed"},
    {"from": "storage_agent", "to": "database", "type": "bidirectional", "style": "dashed"},
    {"from": "hvac_agent", "to": "database", "type": "bidirectional", "style": "dashed"},
    # 监察连接
    {"from": "monitor_agent", "to": "data_agent", "type": "monitor", "style": "dotted"},
    {"from": "monitor_agent", "to": "prediction_agent", "type": "monitor", "style": "dotted"},
    {"from": "monitor_agent", "to": "storage_agent", "type": "monitor", "style": "dotted"},
    {"from": "monitor_agent", "to": "hvac_agent", "type": "monitor", "style": "dotted"},
    {"from": "monitor_agent", "to": "database", "type": "monitor", "style": "dotted"},
]


def get_graph_topology() -> dict[str, Any]:
    """返回图拓扑结构（供前端渲染）。"""
    return {"nodes": GRAPH_NODES, "edges": GRAPH_EDGES}


def build_energy_workflow(handlers: dict[str, NodeHandler]):
    """Build and compile the workflow actually used by SimulationEngine.

    Each invocation is a durable phase transition: a fresh day stops at forecast
    approval, forecast approval produces both plans and stops at their gates, and
    only two approved dispatch reports can reach physical activation.
    """

    required = {
        "data_agent",
        "prediction_agent",
        "forecast_approval",
        "storage_agent",
        "hvac_agent",
        "dispatch_approvals",
        "physical_dispatch",
        "monitor_agent",
    }
    missing = required.difference(handlers)
    if missing:
        raise ValueError(f"missing workflow handlers: {sorted(missing)}")

    graph = StateGraph(EnergySystemState)

    async def route_node(_: EnergySystemState) -> dict[str, Any]:
        return {}

    def route(state: EnergySystemState) -> str:
        status = state.get("workflow_status", "new")
        if status == "new":
            return "new"
        if status == "forecast_approved":
            return "forecast_approved"
        if status == "dispatch_approved":
            return "dispatch_approved"
        return "wait"

    graph_names = {
        "data_agent": "run_data_agent",
        "prediction_agent": "run_prediction_agent",
        "forecast_approval": "open_forecast_gate",
        "storage_agent": "run_storage_agent",
        "hvac_agent": "run_hvac_agent",
        "dispatch_approvals": "open_dispatch_gates",
        "physical_dispatch": "activate_physical_dispatch",
        "monitor_agent": "run_monitor_agent",
    }
    graph.add_node("route", route_node)
    for logical_name, handler in handlers.items():
        graph.add_node(graph_names[logical_name], handler)

    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route",
        route,
        {
            "new": "run_data_agent",
            "forecast_approved": "run_storage_agent",
            "dispatch_approved": "activate_physical_dispatch",
            "wait": END,
        },
    )

    graph.add_edge("run_data_agent", "run_prediction_agent")
    graph.add_edge("run_prediction_agent", "open_forecast_gate")
    graph.add_edge("open_forecast_gate", END)

    graph.add_edge("run_storage_agent", "run_hvac_agent")
    graph.add_edge("run_hvac_agent", "open_dispatch_gates")
    graph.add_edge("open_dispatch_gates", END)

    graph.add_edge("activate_physical_dispatch", "run_monitor_agent")
    graph.add_edge("run_monitor_agent", END)
    return graph.compile()
