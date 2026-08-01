import { useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { ActionHistory, Card, StatusDot, statusLabel } from '../components/Layout'
import { FileText, CheckCircle2, XCircle, Clock, Eye } from 'lucide-react'

export function AgentFlowPanel() {
  const { state, approve, selectedNodeId, setSelectedReport, selectedReportId } = useAppStore()
  const [comments, setComments] = useState<Record<string, string>>({})
  const [busyGate, setBusyGate] = useState<string | null>(null)
  if (!state) return <div className="panel-stack"><p className="empty-copy">等待 Agent 状态</p></div>

  const selectedRuntimeId: Record<string, string> = { data_agent: 'data_collect', prediction_agent: 'prediction', storage_agent: 'storage_dispatch', hvac_agent: 'hvac_dispatch', monitor_agent: 'monitor' }
  const selectedAgent = selectedNodeId ? state.agent_nodes[selectedRuntimeId[selectedNodeId]] : null
  const selectedGate = selectedNodeId ? state.approval_gates[selectedNodeId] : null
  const selectedReport = state.reports.find(r => r.report_id === selectedReportId)

  const decide = async (id: string, decision: 'approve' | 'reject') => {
    const comment = comments[id]?.trim() || ''
    if (decision === 'reject' && !comment) { window.alert('退回修改需要填写原因。'); return }
    if (!window.confirm(`${decision === 'approve' ? '批准并继续执行' : '退回该报告修改'}？${comment ? `\n备注：${comment}` : ''}`)) return
    setBusyGate(id)
    try { await approve(id, decision, comment) } finally { setBusyGate(null) }
  }

  return (
    <div className="panel-stack">
      <Card title="当前节点" icon={<Clock className="icon-sm industrial" />}>
        <div className="data-list">
          {!selectedAgent && !selectedGate && <p className="empty-copy">点击流程节点查看执行详情</p>}
          {selectedAgent && <>
            <Row label="Agent" value={selectedAgent.name} />
            <Row label="状态" value={statusLabel(selectedAgent.status)} />
            <Row label="开始" value={selectedAgent.started_at ? new Date(selectedAgent.started_at).toLocaleString('zh-CN') : '尚未开始'} />
            <Row label="结束" value={selectedAgent.completed_at ? new Date(selectedAgent.completed_at).toLocaleString('zh-CN') : '—'} />
            <Row label="耗时" value={selectedAgent.duration_ms ? `${selectedAgent.duration_ms} ms` : '—'} />
            <p className="text-[11px] text-slate-600 leading-5">{selectedAgent.last_result_summary || selectedAgent.message || '等待本轮执行结果'}</p>
          </>}
          {selectedGate && <><Row label="审批门" value={selectedGate.name} /><Row label="状态" value={statusLabel(selectedGate.status)} /><p className="text-[11px] text-slate-600">{selectedGate.description}</p></>}
        </div>
      </Card>

      <Card title="工程师审批" icon={<CheckCircle2 className="icon-sm" style={{ color: '#d97706' }} />}>
        <div className="form-grid">
          {Object.entries(state.approval_gates).map(([id, gate]) => <div key={id} className="rounded border border-slate-200 p-2.5">
            <div className="flex items-center justify-between gap-2"><strong className="text-xs text-slate-700">{gate.name}</strong><span className="flex items-center gap-1 text-[10px] text-slate-500"><StatusDot status={gate.status} />{statusLabel(gate.status)}</span></div>
            <p className="my-1.5 text-[10px] text-slate-500">{gate.description}</p>
            {gate.status === 'pending_approval' && <>
              <label><span>审批意见</span><textarea value={comments[id] || ''} onChange={e => setComments(v => ({ ...v, [id]: e.target.value }))} placeholder="批准可选填；退回必须说明原因" /></label>
              <div className="button-row"><button disabled={busyGate === id} onClick={() => decide(id, 'approve')} className="btn approve"><CheckCircle2 className="inline h-3.5 w-3.5" /> 批准</button><button disabled={busyGate === id} onClick={() => decide(id, 'reject')} className="btn danger"><XCircle className="inline h-3.5 w-3.5" /> 退回</button></div>
            </>}
            {gate.decision && <p className="mt-2 text-[10px] text-slate-500">结果：{gate.decision} {gate.comment && `· ${gate.comment}`}</p>}
          </div>)}
        </div>
      </Card>

      <Card title="Agent 报告" icon={<FileText className="icon-sm industrial" />}>
        <div className="p-2 space-y-1.5 max-h-72 overflow-y-auto">
          {state.reports.length === 0 && <p className="empty-copy">本轮尚未生成报告</p>}
          {state.reports.map(r => <button key={r.report_id} onClick={() => setSelectedReport(r.report_id)} className="w-full rounded border border-slate-200 bg-white p-2 text-left hover:border-cyan-600">
            <span className="flex items-center justify-between gap-2"><strong className="text-[11px] text-slate-700">{r.title}</strong><Eye className="h-3.5 w-3.5 text-slate-400" /></span><small className="text-[9px] text-slate-400">{new Date(r.created_at).toLocaleString('zh-CN')} · {r.status}</small>
          </button>)}
        </div>
      </Card>
      {selectedReport && <Card title="报告详情" icon={<FileText className="icon-sm industrial" />}><article className="p-3"><h4 className="m-0 text-sm text-slate-800">{selectedReport.title}</h4><p className="whitespace-pre-wrap text-[11px] leading-5 text-slate-600">{selectedReport.content}</p><details className="text-[10px] text-slate-500"><summary>结构化数据</summary><pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify(selectedReport.data, null, 2)}</pre></details></article></Card>}
      <ActionHistory />
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) { return <div className="data-row"><span>{label}</span><strong>{value}</strong></div> }
