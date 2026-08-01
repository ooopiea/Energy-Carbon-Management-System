"""模拟引擎：协调 5 个 Agent 运行，驱动 200x 速时间循环。

每个模拟日流程：
1. [step=0] 数据Agent 生成日前数据 → 预测Agent 生成负荷预测
2. [审批门1] 工程师审批预测报告
3. 储能Agent 生成日前调度 + HVACAgent 生成日前冷机调度
4. [审批门2/3] 工程师审批储能/HVAC调度
5. 物理执行端执行已批准策略
6. [每tick] 监察Agent 心跳检测 + 异常预警

实时每个 tick (15min模拟) 从日前计划中取当前步数据并注入噪声。
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import uuid
from datetime import datetime
from typing import Any

from algorithms.carbon import account_dispatch_carbon, compute_carbon_factors
from algorithms.hvac import get_chiller_topology, optimize_hvac_dispatch
from algorithms.storage import optimize_storage_dispatch
from algorithms.tariff import calculate_tariff
from core.config import POINTS_PER_DAY, STORAGE_DEFAULTS
from core.state import (
    AgentNodeStatus,
    AgentReport,
    AgentType,
    AlertItem,
    ApprovalGate,
    NodeStatus,
    PhysicsConstraints,
    RuntimeState,
    Severity,
)
from core.time_engine import get_time_engine
from data.simulator import generate_day_ahead_data


class SimulationEngine:
    """全局模拟引擎单例。"""

    def __init__(self):
        self._rng = random.Random(123)
        self._constraints = PhysicsConstraints()
        self._time = get_time_engine()
        self._day_data: dict[str, Any] | None = None
        self._storage_plan: dict | None = None
        self._hvac_plan: dict | None = None
        self._carbon_data: dict | None = None
        self._tariff_data: dict | None = None
        self._carbon_dispatch: dict | None = None
        self._last_step = -1
        self._last_day = -1
        self._ws_clients: list = []
        self._reports: list[AgentReport] = []
        self._alerts: list[AlertItem] = []
        self._auto_approve = True
        self._agent_nodes: dict[str, AgentNodeStatus] = {}
        self._approval_gates: dict[str, ApprovalGate] = {}
        self._init_nodes()
        self._daily_metrics: dict[str, float] = {}
        self._series_cache: dict[str, list[dict]] = {}

    def _init_nodes(self):
        """初始化 Agent 节点和审批门。"""
        node_defs = [
            ("data_collect", AgentType.DATA, "数据采集", "收集传感器数据并校验"),
            ("data_archive", AgentType.DATA, "数据封存", "封存当日前数据"),
            ("prediction", AgentType.PREDICTION, "负荷预测", "日前负荷预测"),
            ("storage_dispatch", AgentType.STORAGE, "储能调度", "日前储能充放电优化"),
            ("hvac_dispatch", AgentType.HVAC, "HVAC调度", "冷机群日前调度优化"),
            ("monitor", AgentType.MONITOR, "系统监察", "心跳检测与异常预警"),
        ]
        for node_id, atype, name, _desc in node_defs:
            self._agent_nodes[node_id] = AgentNodeStatus(
                agent_type=atype, node_id=node_id, name=name,
            )

        gate_defs = [
            ("forecast_approval", "预测审批", "负荷预测报告工程师审批"),
            ("storage_approval", "储能审批", "储能调度策略工程师审批"),
            ("hvac_approval", "HVAC审批", "HVAC调度策略工程师审批"),
        ]
        for gate_id, name, desc in gate_defs:
            self._approval_gates[gate_id] = ApprovalGate(
                gate_id=gate_id, name=name, description=desc,
            )

    def _set_node(self, node_id: str, status: NodeStatus, summary: str = "", duration_ms: int | None = None):
        node = self._agent_nodes.get(node_id)
        if node:
            now = datetime.now()
            node.status = status
            node.last_result_summary = summary
            if status == NodeStatus.RUNNING:
                node.started_at = now
            elif status in (NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.WARNING):
                node.completed_at = now
                if node.started_at and duration_ms is None:
                    duration_ms = int((now - node.started_at).total_seconds() * 1000)
                node.duration_ms = duration_ms

    async def run_tick(self):
        """每个现实秒调用：检查是否需要推进模拟步。"""
        step = self._time.current_step
        day = self._time.day_count

        if self._time.is_paused:
            return

        # 新的一天：运行日前工作流
        if day != self._last_day:
            self._last_day = day
            self._last_step = -1
            await self._run_day_ahead(day)

        # 新的步：更新实时数据
        if step != self._last_step and self._day_data:
            self._last_step = step
            await self._update_realtime(step)

    async def _run_day_ahead(self, day: int):
        """运行日前工作流（每天 00:00 触发）。"""
        sim_time = self._time.sim_time
        month = sim_time.month

        # 1. 数据 Agent
        self._set_node("data_collect", NodeStatus.RUNNING)
        self._day_data = generate_day_ahead_data(day, month)
        self._set_node("data_collect", NodeStatus.COMPLETED,
                       f"已采集 {len(self._day_data['load_kw'])} 点数据 ({self._day_data['season']})")

        self._set_node("data_archive", NodeStatus.RUNNING)
        # 数据封存（模拟）
        self._set_node("data_archive", NodeStatus.COMPLETED, "日前数据已封存")

        self._add_report(AgentType.DATA, "日前数据采集报告",
                         f"采集季节:{self._day_data['season']} 负荷均值:{sum(self._day_data['load_kw'])/96:.0f}kW",
                         {"season": self._day_data["season"], "load_mean": sum(self._day_data["load_kw"])/96})

        # 2. 预测 Agent
        self._set_node("prediction", NodeStatus.RUNNING)
        forecast = self._gen_forecast(self._day_data["load_kw"])
        self._set_node("prediction", NodeStatus.COMPLETED,
                       f"负荷预测完成 峰值{max(forecast):.0f}kW 均值{sum(forecast)/96:.0f}kW")
        self._day_data["load_forecast"] = forecast

        self._add_report(AgentType.PREDICTION, "日前负荷预测报告",
                         f"预测峰值{max(forecast):.0f}kW 出现于{forecast.index(max(forecast))*15//60:02d}:{(forecast.index(max(forecast))*15%60):02d}",
                         {"peak": max(forecast), "mean": sum(forecast)/96})

        # 审批门1: 预测审批
        await self._process_approval("forecast_approval", "预测报告",
                                     f"峰值负荷 {max(forecast):.0f} kW")

        # 3. 碳因子计算
        self._carbon_data = compute_carbon_factors(self._day_data["generation_mix"])

        # 4. 储能 Agent
        self._set_node("storage_dispatch", NodeStatus.RUNNING)
        self._storage_plan = optimize_storage_dispatch(
            self._day_data["load_forecast"],
            self._day_data["price_cny_per_kwh"],
            carbon_factors=self._carbon_data["c_factors"],
            objective="min_cost",
        )
        self._set_node("storage_dispatch", NodeStatus.COMPLETED,
                       f"储能调度完成 省{self._storage_plan['saving_cny']:.0f}元 削峰{self._storage_plan['peak_reduction_kw']:.0f}kW")

        self._add_report(AgentType.STORAGE, "日前储能调度报告",
                         f"MILP优化 省{self._storage_plan['saving_cny']:.0f}元 削峰{self._storage_plan['peak_reduction_kw']:.0f}kW 末端SOC{self._storage_plan['terminal_soc']:.2%}",
                         self._storage_plan)

        # 5. HVAC Agent
        self._set_node("hvac_dispatch", NodeStatus.RUNNING)
        self._hvac_plan = optimize_hvac_dispatch(
            self._day_data["hvac_load_kw"],
            self._day_data["weather"]["temp_c"],
            self._day_data["price_cny_per_kwh"],
            self._day_data["tariff_periods"],
        )
        self._set_node("hvac_dispatch", NodeStatus.COMPLETED,
                       f"HVAC调度完成 省{self._hvac_plan['saving_cny']:.0f}元 平均COP{self._hvac_plan['avg_cop']:.1f}")

        self._add_report(AgentType.HVAC, "日前HVAC调度报告",
                         f"冷机调度 省{self._hvac_plan['saving_cny']:.0f}元 平均COP{self._hvac_plan['avg_cop']:.1f}",
                         self._hvac_plan)

        # 审批门2/3
        await self._process_approval("storage_approval", "储能调度",
                                     f"省{self._storage_plan['saving_cny']:.0f}元")
        await self._process_approval("hvac_approval", "HVAC调度",
                                     f"省{self._hvac_plan['saving_cny']:.0f}元")

        # 6. 电费和碳排放核算
        optimized_grid = self._storage_plan["grid_kw"]
        self._tariff_data = calculate_tariff(
            optimized_grid,
            self._day_data["price_cny_per_kwh"],
        )
        self._carbon_dispatch = account_dispatch_carbon(
            self._carbon_data["c_factors"],
            self._carbon_data["cr_factors"],
            self._day_data["load_forecast"],
            optimized_grid,
            0.25,
        )

        # 重置日内累计
        self._daily_metrics = {"energy": 0, "cost": 0, "carbon": 0, "peak": 0}
        self._series_cache = {}

        # 监察 Agent 日报
        self._set_node("monitor", NodeStatus.COMPLETED, "日前流程监察通过")
        self._add_report(AgentType.MONITOR, "系统监察日报",
                         f"日前流程完成 储能/HVAC计划已批准 碳减排{self._carbon_dispatch['direct_reduction_kg']:.0f}kg")

    def _gen_forecast(self, actual_load: list[float]) -> list[float]:
        """预测 Agent：基于实际负荷加入预测误差。"""
        forecast = []
        for load in actual_load:
            noise = self._rng.gauss(0, 0.02)  # 2% 预测误差
            forecast.append(max(0, load * (1 + noise)))
        return forecast

    async def _process_approval(self, gate_id: str, title: str, summary: str):
        """处理审批门：auto-approve 或等待用户决策。"""
        gate = self._approval_gates[gate_id]
        gate.status = NodeStatus.PENDING_APPROVAL
        gate.description = f"{title}: {summary}"

        if self._auto_approve:
            gate.status = NodeStatus.APPROVED
            gate.decision = "approve"
            gate.decided_at = datetime.now()
        else:
            # 等待用户决策（前端 API 调用 set_approval）
            pass

    async def _update_realtime(self, step: int):
        """每个 tick 更新实时数据。"""
        if not self._day_data or step >= POINTS_PER_DAY:
            return

        dd = self._day_data
        ts = dd["timestamps"][step].isoformat()

        # 实时值（加入少量噪声模拟传感器读数波动）
        load = dd["load_kw"][step] * (1 + self._rng.gauss(0, 0.01))
        solar = dd["solar_kw"][step]
        storage_power = self._storage_plan["power_kw"][step] if self._storage_plan else 0
        soc = self._storage_plan["soc_ratio"][step] if self._storage_plan else 0.5
        storage_temp = self._storage_plan["temp_c"][step] if self._storage_plan else 25
        hvac_power = self._hvac_plan["power_kw"][step] if self._hvac_plan else 0
        hvac_supply = self._hvac_plan["supply_temp_c"][step] if self._hvac_plan else 7
        grid = load - solar - storage_power

        c_factor = self._carbon_data["c_factors"][step] if self._carbon_data else 0.5
        price = dd["price_cny_per_kwh"][step]
        period = dd["tariff_periods"][step]

        # 日内累计
        dt_h = 0.25
        self._daily_metrics["energy"] = self._daily_metrics.get("energy", 0) + load * dt_h
        self._daily_metrics["cost"] = self._daily_metrics.get("cost", 0) + grid * price * dt_h
        self._daily_metrics["carbon"] = self._daily_metrics.get("carbon", 0) + grid * c_factor * dt_h
        self._daily_metrics["peak"] = max(self._daily_metrics.get("peak", 0), load)

        # 时间序列缓存
        for key, val in [
            ("load", load), ("solar", solar), ("grid", grid),
            ("storage_power", storage_power), ("soc", soc * 100),
            ("temp", storage_temp), ("hvac_power", hvac_power),
            ("carbon", c_factor), ("price", price),
        ]:
            if key not in self._series_cache:
                self._series_cache[key] = []
            self._series_cache[key].append({"time": ts, "step": step, "value": round(val, 2)})

        # 监察 Agent：检查约束
        await self._monitor_check(step, soc, storage_temp, hvac_supply, load)

        # 推送更新
        await self._broadcast()

    async def _monitor_check(self, step, soc, temp, hvac_supply, load):
        """监察 Agent：约束检查与异常预警。"""
        self._set_node("monitor", NodeStatus.RUNNING)

        # 检查 SOC
        if soc < self._constraints.soc_min:
            self._add_alert(Severity.CRITICAL, "monitor", f"SOC {soc:.1%} 低于下限 {self._constraints.soc_min:.0%}")
        # 检查温度
        if temp > self._constraints.temp_max_c:
            self._add_alert(Severity.CRITICAL, "monitor", f"电芯温度 {temp:.1f}°C 超过上限 {self._constraints.temp_max_c}°C")
        elif temp > self._constraints.temp_max_c - 5:
            self._add_alert(Severity.WARNING, "monitor", f"电芯温度 {temp:.1f}°C 接近上限")
        # 检查 HVAC 供水温度
        if hvac_supply < self._constraints.chilled_water_min_c:
            self._add_alert(Severity.WARNING, "hvac", f"供水温度 {hvac_supply:.1f}°C 低于下限")

        self._set_node("monitor", NodeStatus.COMPLETED, f"第{step}步监察正常" if not self._alerts else f"第{step}步发现{len(self._alerts)}项告警")

    def _add_report(self, atype: AgentType, title: str, summary: str, data: dict | None = None):
        report = AgentReport(
            report_id=f"rpt-{uuid.uuid4().hex[:8]}",
            agent_type=atype, title=title, content=summary,
            data=data or {}, created_at=datetime.now(),
        )
        self._reports.append(report)

    def _add_alert(self, severity: Severity, source: str, message: str):
        # 去重：相同消息 5 分钟内不重复
        existing = [a for a in self._alerts if a.message == message and not a.acknowledged]
        if existing:
            return
        self._alerts.append(AlertItem(
            alert_id=f"alt-{uuid.uuid4().hex[:8]}",
            severity=severity, source=source, message=message,
            timestamp=datetime.now(),
        ))
        if len(self._alerts) > 50:
            self._alerts = self._alerts[-50:]

    def get_state(self) -> dict[str, Any]:
        """返回完整运行状态（供 API）。"""
        t = self._time
        step = t.current_step
        dd = self._day_data or {}

        load = dd.get("load_kw", [0]*96)[step] if dd else 0
        solar = dd.get("solar_kw", [0]*96)[step] if dd else 0
        storage_p = self._storage_plan["power_kw"][step] if self._storage_plan else 0
        soc = self._storage_plan["soc_ratio"][step] if self._storage_plan else 0
        temp = self._storage_plan["temp_c"][step] if self._storage_plan else 25
        hvac_p = self._hvac_plan["power_kw"][step] if self._hvac_plan else 0
        hvac_st = self._hvac_plan["supply_temp_c"][step] if self._hvac_plan else 7
        grid = load - solar - storage_p if dd else 0
        c_factor = self._carbon_data["c_factors"][step] if self._carbon_data else 0.5
        price = dd.get("price_cny_per_kwh", [0]*96)[step] if dd else 0
        period = dd.get("tariff_periods", ["flat"]*96)[step] if dd else "flat"

        return {
            "time": t.tick_info(),
            "load_kw": round(load, 1),
            "solar_kw": round(solar, 1),
            "grid_kw": round(grid, 1),
            "storage_power_kw": round(storage_p, 1),
            "storage_soc": round(soc, 4),
            "storage_temp_c": round(temp, 2),
            "hvac_power_kw": round(hvac_p, 1),
            "hvac_supply_temp_c": round(hvac_st, 1),
            "carbon_factor": round(c_factor, 6),
            "price": round(price, 6),
            "tariff_period": period,
            "daily": {
                "energy_kwh": round(self._daily_metrics.get("energy", 0), 1),
                "cost_cny": round(self._daily_metrics.get("cost", 0), 2),
                "carbon_kg": round(self._daily_metrics.get("carbon", 0), 1),
                "peak_kw": round(self._daily_metrics.get("peak", 0), 1),
            },
            "agent_nodes": {k: v.model_dump(mode="json") for k, v in self._agent_nodes.items()},
            "approval_gates": {k: v.model_dump(mode="json") for k, v in self._approval_gates.items()},
            "reports": [r.model_dump(mode="json") for r in self._reports[-20:]],
            "alerts": [a.model_dump(mode="json") for a in self._alerts[-20:]],
            "series": dict(self._series_cache),
            "day_ahead": {
                "load_forecast": dd.get("load_forecast", dd.get("load_kw", [])),
                "storage_plan": self._storage_plan["power_kw"] if self._storage_plan else [],
                "soc_plan": self._storage_plan["soc_ratio"] if self._storage_plan else [],
                "hvac_plan": self._hvac_plan["power_kw"] if self._hvac_plan else [],
                "price": dd.get("price_cny_per_kwh", []),
                "carbon_c": self._carbon_data["c_factors"] if self._carbon_data else [],
                "carbon_cr": self._carbon_data["cr_factors"] if self._carbon_data else [],
            },
            "storage_summary": self._storage_plan,
            "hvac_summary": self._hvac_plan,
            "tariff_summary": self._tariff_data,
            "carbon_dispatch": self._carbon_dispatch,
            "chiller_topology": get_chiller_topology(),
            "weather": {
                "temp_c": dd.get("weather", {}).get("temp_c", [25]*96)[step] if dd else 25,
                "humidity": dd.get("weather", {}).get("humidity_pct", [60]*96)[step] if dd else 60,
                "wind": dd.get("weather", {}).get("wind_speed_ms", [3]*96)[step] if dd else 3,
            },
        }

    async def _broadcast(self):
        """推送给所有 WebSocket 客户端。"""
        state = self.get_state()
        msg = json.dumps({"type": "state_update", "data": state}, ensure_ascii=False, default=str)
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.remove(ws)

    def add_ws_client(self, ws):
        self._ws_clients.append(ws)

    def remove_ws_client(self, ws):
        if ws in self._ws_clients:
            self._ws_clients.remove(ws)

    def set_approval(self, gate_id: str, decision: str, comment: str = ""):
        """处理工程师审批决策。"""
        gate = self._approval_gates.get(gate_id)
        if gate:
            gate.decision = decision
            gate.comment = comment
            gate.decided_at = datetime.now()
            gate.status = NodeStatus.APPROVED if decision == "approve" else NodeStatus.REJECTED

    def acknowledge_alert(self, alert_id: str):
        for a in self._alerts:
            if a.alert_id == alert_id:
                a.acknowledged = True
                return True
        return False

    def set_auto_approve(self, enabled: bool):
        self._auto_approve = enabled


# 全局单例
_engine: SimulationEngine | None = None


def get_engine() -> SimulationEngine:
    global _engine
    if _engine is None:
        _engine = SimulationEngine()
    return _engine
