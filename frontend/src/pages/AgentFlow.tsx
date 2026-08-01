import { useAppStore } from '../stores/appStore'
import { Card, statusColor, StatusDot, statusLabel } from '../components/Layout'
import { Workflow, Cpu } from 'lucide-react'
import type { GraphNode } from '../types'

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
  }
  const mappedId = mapping[nodeId]
  if (mappedId && agentNodes[mappedId]) {
    return agentNodes[mappedId].status
  }
  if (gates[nodeId]) return gates[nodeId].status
  if (nodeId === 'physical_dispatch' || nodeId === 'end') {
    const statuses = Object.values(gates).map((gate: any) => gate.status)
    return statuses.length > 0 && statuses.every(s => s === 'approved') ? 'completed' : statuses.some(s => s === 'rejected') ? 'failed' : 'idle'
  }
  return nodeId === 'start' ? 'completed' : 'idle'
}

export function AgentFlow() {
  const { state, graph, selectedNodeId, setSelectedNode, setContextSelection } = useAppStore()
  if (!state) return <div className="text-slate-400">加载中...</div>

  const agentNodes = state.agent_nodes

  const renderTopology = () => {
    if (!graph) return <div className="text-slate-400 text-sm">加载拓扑图...</div>

    return (
      <svg viewBox="-30 0 860 900" className="w-full" style={{ maxHeight: 720 }} role="group" aria-label="LangGraph 工作流拓扑">
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
