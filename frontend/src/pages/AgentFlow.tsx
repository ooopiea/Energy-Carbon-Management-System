import { useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { Card, statusColor, StatusDot, statusLabel } from '../components/Layout'
import { Workflow, Cpu, ShieldCheck, RotateCcw, FileText, ChevronDown, ChevronRight } from 'lucide-react'
import type { GraphNode, CheckpointView } from '../types'

const NODE_COLORS: Record<string, string> = {
  start: '#3b82f6', end: '#64748b',
  agent: '#2563eb', approval: '#f59e0b',
  database: '#ef4444', physical: '#22c55e',
}

function getNodeStatus(nodeId: string, agentNodes: any, gates: any): string {
  const mapping: Record<string, string> = {
    data_agent: 'data_collect',
    prediction_agent: 'prediction',
    storage_agent: 'storage_dispatch',
    hvac_agent: 'hvac_dispatch',
    monitor_agent: 'monitor',
    dispatch_approvals: 'storage_approval',
  }
  const mappedId = mapping[nodeId]
  if (mappedId && agentNodes[mappedId]) {
    return agentNodes[mappedId].status
  }
  if (gates[nodeId]) return gates[nodeId].status
  // dispatch_approvals merges storage + hvac approval status
  if (nodeId === 'dispatch_approvals') {
    const s = gates['storage_approval']?.status
    const h = gates['hvac_approval']?.status
   if (s === 'approved' && h === 'approved') return 'completed'
   if (s === 'rejected' || h === 'rejected') return 'failed'
   if (s === 'idle' && h === 'idle') return 'idle'
   return 'pending_approval'
  }
  if (nodeId === 'physical_dispatch' || nodeId === 'end') {
    const statuses = Object.values(gates).map((gate: any) => gate.status)
    return statuses.length > 0 && statuses.every(s => s === 'approved') ? 'completed' : statuses.some(s => s === 'rejected') ? 'failed' : 'idle'
  }
  return nodeId === 'start' ? 'completed' : 'idle'
}

export function AgentFlow() {
 const { state, graph, selectedNodeId, setSelectedNode, setContextSelection } = useAppStore()
  const [expandedGates, setExpandedGates] = useState<Set<string>>(new Set())
 if (!state) return <div className="text-slate-400">加载中...</div>

 const agentNodes = state.agent_nodes

  const toggleGate = (gateId: string) => {
    setExpandedGates(prev => {
      const next = new Set(prev)
      if (next.has(gateId)) next.delete(gateId)
      else next.add(gateId)
      return next
    })
  }

  const renderGateReport = (report: any) => {
    const entries = report.data
      ? Object.entries(report.data).filter(([, v]) => v != null && !Array.isArray(v))
      : []
    return (
      <div className="mt-2 space-y-1.5 rounded-md bg-slate-50 p-2.5 text-xs">
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span className="text-slate-400">报告 ID</span>
          <span className="font-mono text-slate-600">{report.report_id}</span>
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span className="text-slate-400">类型</span>
          <span className="text-slate-600">{report.agent_type}</span>
          <span className="text-slate-400 ml-2">状态</span>
          <span className="text-slate-600">{report.status}</span>
        </div>
        <div className="text-slate-600">{report.content}</div>
        {entries.length > 0 && (
          <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 border-t border-slate-200 pt-1.5">
            {entries.map(([k, v]) => (
              <div key={k} className="flex justify-between">
                <span className="text-slate-400">{k}</span>
                <span className="font-mono text-slate-600">{String(v)}</span>
              </div>
            ))}
          </div>
        )}
        <div className="flex flex-wrap gap-x-4 gap-y-0.5 border-t border-slate-200 pt-1">
          <span className="text-slate-400">哈希</span>
          <span className="font-mono text-[10px] text-slate-500 break-all">{report.content_hash?.slice(0, 24)}...</span>
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-0.5">
          <span className="text-slate-400">生成时间</span>
          <span className="font-mono text-slate-500">{report.created_at ? new Date(report.created_at).toLocaleString() : '-'}</span>
        </div>
      </div>
    )
  }

  const renderTopology = () => {
    if (!graph) return <div className="text-slate-400 text-sm">加载拓扑图...</div>

    return (
      <svg viewBox="-30 0 860 900" className="w-full mx-auto" style={{ maxHeight: 520, display: 'block' }} role="group" aria-label="LangGraph 工作流拓扑">
        {/* 边 */}
        {graph.edges.map((edge, i) => {
          const from = graph.nodes.find((n: GraphNode) => n.id === edge.from)
          const to = graph.nodes.find((n: GraphNode) => n.id === edge.to)
          if (!from || !to) return null
          const isMonitor = edge.type === 'monitor'
          const isDashed = edge.style === 'dashed' || isMonitor
          const strokeColor = isMonitor ? '#94a3b8' : edge.type === 'approve' ? '#22c55e' : '#cbd5e1'
          return (
            <line
              key={i}
              x1={from.x} y1={from.y}
              x2={to.x} y2={to.y}
              stroke={strokeColor}
              strokeWidth={isMonitor ? 1 : 1.5}
              strokeDasharray={isDashed ? '4 3' : undefined}
              opacity={isMonitor ? 0.4 : 0.8}
            />
          )
        })}

        {/* 节点 */}
        {graph.nodes.map((node: GraphNode) => {
          const status = getNodeStatus(node.id, agentNodes, state.approval_gates)
          const color = statusColor(status)
          const isSelected = selectedNodeId === node.id
          const w = 120
          const h = 36

          return (
            <g key={node.id} role="button" tabIndex={0} aria-label={`${node.label}，${statusLabel(status)}`}
              onClick={() => { setSelectedNode(node.id); setContextSelection({ kind: 'node', id: node.id, label: node.label, page: 'agent_flow', detail: { status } }) }}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedNode(node.id); setContextSelection({ kind: 'node', id: node.id, label: node.label, page: 'agent_flow', detail: { status } }) } }} style={{ cursor: 'pointer' }}>
              <rect
                x={node.x - w / 2} y={node.y - h / 2}
                width={w} height={h} rx={6}
                fill={node.type === 'agent' ? color : '#fff'}
                stroke={isSelected ? '#1d4ed8' : color}
                strokeWidth={isSelected ? 2.5 : 1.5}
                opacity={node.type === 'agent' ? 0.9 : 1}
              />
              {(node.type === 'agent' || node.type === 'approval') && (
                <circle cx={node.x - w / 2 + 12} cy={node.y} r={3} fill="#fff" opacity={0.8} />
              )}
              <text
                x={node.x} y={node.y + 4}
                textAnchor="middle"
                fontSize={11}
                fontWeight={600}
                fill={node.type === 'agent' ? '#fff' : color}
              >
                {node.label}
              </text>
            </g>
          )
        })}
      </svg>
    )
  }

  // 选中节点的详情
  const selectedNode = graph?.nodes.find((n: GraphNode) => n.id === selectedNodeId)
  const selectedAgentStatus = selectedNode ? getNodeStatus(selectedNode.id, agentNodes, state.approval_gates) : null

  return (
    <div className="page-stack">
      <div className="page-title-row">
        <div><h1>Agent 工作流</h1><p>监视 LangGraph 执行链、审批门与物理调度状态</p></div>
        <div className="flex items-center gap-3 text-xs">
          {['completed', 'running', 'pending_approval', 'failed'].map(s => (
            <div key={s} className="flex items-center gap-1">
              <StatusDot status={s} />
              <span className="text-slate-500">{statusLabel(s)}</span>
            </div>
          ))}
        </div>
      </div>

      <Card title="LangGraph 工作流拓扑" icon={<Workflow className="w-4 h-4 text-blue-500" />}>
        {renderTopology()}
      </Card>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {Object.entries(agentNodes).map(([id, node]: [string, any]) => (
          <Card key={id} className="p-3">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <StatusDot status={node.status} />
                <span className="text-sm font-semibold text-slate-700">{node.name}</span>
              </div>
              <span className="text-[10px] text-slate-400">{node.duration_ms ? `${node.duration_ms}ms` : '-'}</span>
            </div>
            <div className="text-xs text-slate-500 truncate">{node.last_result_summary || '等待运行'}</div>
          </Card>
       ))}
     </div>

      {/* 审批门 — 每门自带可展开报告，不集中堆放 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {Object.entries(state.approval_gates).map(([id, gate]: [string, any]) => {
          const expanded = expandedGates.has(id)
          const report = gate.report
          return (
            <Card key={id} className="p-3">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <StatusDot status={gate.status} />
                  <span className="text-sm font-semibold text-slate-700">{gate.name}</span>
                </div>
                <span className="text-[10px] text-slate-400">{statusLabel(gate.status)}</span>
              </div>
              <div className="text-xs text-slate-500 mb-2">{gate.description}</div>
              {gate.decision && (
                <div className="text-[10px] text-slate-400 mb-2">
                  {gate.decision === 'approve' ? '批准' : gate.decision === 'reject' ? '退回' : '要求修订'} {gate.decided_by ? `· ${gate.decided_by}` : ''} {gate.comment ? `· "${gate.comment}"` : ''}
                </div>
              )}
              {report ? (
                <div>
                  <button
                    onClick={() => toggleGate(id)}
                    className="flex w-full items-center gap-1.5 rounded bg-slate-100 hover:bg-slate-200 transition-colors px-2 py-1.5 text-left"
                  >
                    {expanded
                      ? <ChevronDown className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                      : <ChevronRight className="w-3.5 h-3.5 text-slate-500 shrink-0" />}
                    <FileText className="w-3.5 h-3.5 text-amber-500 shrink-0" />
                    <span className="text-xs font-medium text-slate-600 truncate">{report.title}</span>
                  </button>
                  {expanded && renderGateReport(report)}
                </div>
              ) : (
                <div className="text-xs text-slate-400 italic">等待上游 Agent 产出报告</div>
              )}
            </Card>
          )
        })}
      </div>

     {state.checkpoint?.available && (
        <Card title="运行检查点与恢复状态" icon={<ShieldCheck className="w-4 h-4 text-emerald-500" />}>
          <div className="p-3 grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
            <div>
              <div className="text-slate-400 mb-0.5">当前子图</div>
              <div className="font-semibold text-slate-700">{state.checkpoint.subgraph === 'realtime' ? '实时控制' : '日前规划'}</div>
            </div>
            <div>
              <div className="text-slate-400 mb-0.5">工作流状态</div>
              <div className="font-semibold text-slate-700">{state.checkpoint.workflow_status}</div>
            </div>
            <div>
              <div className="text-slate-400 mb-0.5">最后完成 Tick</div>
              <div className="font-semibold text-slate-700">{state.checkpoint.last_completed_tick ?? '-'}</div>
            </div>
            <div>
              <div className="text-slate-400 mb-0.5">调度已激活</div>
              <div className="font-semibold text-slate-700">{state.checkpoint.dispatch_enabled ? '是' : '否'}</div>
            </div>
            <div>
              <div className="text-slate-400 mb-0.5">最后命令</div>
              <div className="font-mono text-slate-600 truncate">{state.checkpoint.last_command_id ?? '-'}</div>
            </div>
            <div>
              <div className="text-slate-400 mb-0.5">ACK 状态</div>
              <div className="font-semibold text-slate-700">
                {state.checkpoint.last_ack_accepted === true ? '已确认' : state.checkpoint.last_ack_accepted === false ? '已拒绝' : '-'}
              </div>
            </div>
            <div>
              <div className="text-slate-400 mb-0.5">检查点更新</div>
              <div className="font-mono text-slate-600">{state.checkpoint.updated_at ? new Date(state.checkpoint.updated_at).toLocaleTimeString() : '-'}</div>
            </div>
            <div className="flex items-center gap-1">
              <RotateCcw className="w-3 h-3 text-emerald-500" />
              <span className="text-emerald-600 font-medium">可恢复</span>
            </div>
          </div>
        </Card>
      )}

      {selectedNode && selectedNode.type === 'agent' && (
        <Card title={`${selectedNode.label} 详情`} icon={<Cpu className="w-4 h-4 text-blue-500" />}>
          <div className="p-3 space-y-2">
            <p className="text-sm text-slate-600">{selectedNode.description}</p>
            {selectedNode.tools && (
              <div className="flex flex-wrap gap-1.5">
                {selectedNode.tools.map((t: string) => (
                  <span key={t} className="px-2 py-0.5 rounded bg-slate-100 text-slate-600 text-xs">{t}</span>
                ))}
              </div>
            )}
            {selectedAgentStatus && (
              <div className="flex items-center gap-2 text-xs">
                <StatusDot status={selectedAgentStatus} />
                <span className="text-slate-500">{statusLabel(selectedAgentStatus)}</span>
              </div>
            )}
          </div>
        </Card>
      )}
    </div>
  )
}
