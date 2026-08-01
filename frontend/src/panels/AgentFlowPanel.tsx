import { useAppStore } from '../stores/appStore'
import { Card, StatusDot, statusLabel } from '../components/Layout'
import { FileText, CheckCircle2, XCircle, Clock } from 'lucide-react'

export function AgentFlowPanel() {
  const { state, approve, selectedNodeId } = useAppStore()
  if (!state) return null

  const agentNodes = Object.entries(state.agent_nodes)
  const gates = Object.entries(state.approval_gates)
  const reports = state.reports

  return (
    <div className="p-3 space-y-3">
      <Card title="Agent 运行状态" icon={<Clock className="w-4 h-4 text-blue-500" />}>
        <div className="p-2 space-y-1.5">
          {agentNodes.map(([id, node]: [string, any]) => (
            <div key={id} className="flex items-center gap-2 p-2 rounded hover:bg-slate-50">
              <StatusDot status={node.status} />
              <div className="flex-1 min-w-0">
                <div className="text-xs font-medium text-slate-700">{node.name}</div>
                <div className="text-[10px] text-slate-400 truncate">{node.last_result_summary || '等待'}</div>
              </div>
              <span className="text-[10px] text-slate-400">{node.duration_ms ? `${node.duration_ms}ms` : ''}</span>
            </div>
          ))}
        </div>
      </Card>

      <Card title="工程师审批" icon={<CheckCircle2 className="w-4 h-4 text-amber-500" />}>
        <div className="p-2 space-y-2">
          {gates.map(([id, gate]: [string, any]) => (
            <div key={id} className="p-2 rounded border border-slate-200">
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-medium text-slate-700">{gate.name}</span>
                <StatusDot status={gate.status} />
              </div>
              <div className="text-[10px] text-slate-500 mb-1.5">{gate.description}</div>
              {gate.status === 'pending_approval' && (
                <div className="flex gap-1.5">
                  <button onClick={() => approve(id, 'approve')} className="flex-1 flex items-center justify-center gap-1 px-2 py-1 rounded bg-green-50 hover:bg-green-100 text-green-600 text-xs">
                    <CheckCircle2 className="w-3 h-3" /> 批准
                  </button>
                  <button onClick={() => approve(id, 'reject')} className="flex-1 flex items-center justify-center gap-1 px-2 py-1 rounded bg-red-50 hover:bg-red-100 text-red-600 text-xs">
                    <XCircle className="w-3 h-3" /> 退回
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </Card>

      <Card title="Agent 报告" icon={<FileText className="w-4 h-4 text-blue-500" />}>
        <div className="p-2 space-y-1.5 max-h-60 overflow-y-auto">
          {reports.map(r => (
            <div key={r.report_id} className="p-2 rounded bg-slate-50">
              <div className="text-xs font-medium text-slate-700 mb-0.5">{r.title}</div>
              <div className="text-[11px] text-slate-500">{r.content}</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}