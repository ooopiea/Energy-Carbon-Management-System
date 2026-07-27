"""异常工况外挂监控图（独立于主调度图）。

设计意图：本图在主调度图之外独立运行，作为实时监控回路：
  - collect_signals 节点收集来自 SCADA/天气/电网/BMS 的异常信号；
  - analyze_anomaly 节点调用 LLM 研判，should_reoptimize=True 时触发重算；
  - 若无需重算则直接结束。
"""
from __future__ import annotations

from datetime import UTC, datetime
from operator import add
from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from energy_agent_v2.contracts import AnomalySignal


class MonitoringState(TypedDict, total=False):
    signals: list[dict[str, Any]]
    alert: dict[str, Any] | None
    should_reoptimize: bool
    events: Annotated[list[dict[str, Any]], add]


class MonitorContext:
    def __init__(self, monitor_agent: Any) -> None:
        self.monitor_agent = monitor_agent


async def collect_signals_node(state: MonitoringState, runtime: Any) -> dict[str, Any]:
    count = len(state.get("signals", []))
    return {"events": [{"event_type": "monitor.collect", "signal_count": count,
                         "occurred_at": datetime.now(UTC).isoformat()}]}


async def analyze_anomaly_node(state: MonitoringState, runtime: Any) -> dict[str, Any]:
    raw_signals = state.get("signals", [])
    if not raw_signals:
        return {"alert": None, "should_reoptimize": False,
                "events": [{"event_type": "monitor.analyze", "result": "no_signals"}]}
    signals = [AnomalySignal.model_validate(s) for s in raw_signals]
    alert = runtime.context.monitor_agent.analyze(signals)
    return {
        "alert": alert.model_dump(mode="json"),
        "should_reoptimize": alert.should_reoptimize,
        "events": [{"event_type": "monitor.analyze", "alert_level": alert.alert_level,
                     "summary": alert.alert_summary, "reoptimize": alert.should_reoptimize}],
    }


def route_after_analysis(state: MonitoringState) -> str:
    return "trigger_reoptimize" if state.get("should_reoptimize") else "done"


async def trigger_reoptimize_node(state: MonitoringState, runtime: Any) -> dict[str, Any]:
    alert = state.get("alert", {})
    return {"events": [{"event_type": "monitor.reoptimize_trigger",
                         "alert_id": alert.get("alert_id"),
                         "action": "已触发主图重新优化"}]}


async def done_node(state: MonitoringState, runtime: Any) -> dict[str, Any]:
    return {"events": [{"event_type": "monitor.done", "result": "no_action_needed"}]}


def build_monitoring_graph(checkpointer: Any = None) -> Any:
    builder = StateGraph(MonitoringState, context_schema=MonitorContext)
    builder.add_node("collect_signals", collect_signals_node)
    builder.add_node("analyze_anomaly", analyze_anomaly_node)
    builder.add_node("trigger_reoptimize", trigger_reoptimize_node)
    builder.add_node("done", done_node)
    builder.add_edge(START, "collect_signals")
    builder.add_edge("collect_signals", "analyze_anomaly")
    builder.add_conditional_edges(
        "analyze_anomaly", route_after_analysis,
        {"trigger_reoptimize": "trigger_reoptimize", "done": "done"},
    )
    builder.add_edge("trigger_reoptimize", END)
    builder.add_edge("done", END)
    return builder.compile(checkpointer=checkpointer) if checkpointer else builder.compile()
