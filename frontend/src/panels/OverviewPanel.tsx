import { useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { ActionHistory, Card, StatusDot } from '../components/Layout'
import { AlertTriangle, Crosshair, Settings, SlidersHorizontal } from 'lucide-react'
import { NaturalLanguagePrompt } from '../components/NaturalLanguagePrompt'
import { ApprovalReportButton } from '../components/ApprovalReportButton'

export function OverviewPanel() {
  const { state, acknowledgeAlert, contextSelection, setContextSelection, setActivePage, submitControlAction } = useAppStore()
  const [strategy, setStrategy] = useState('cost')
  const [cap, setCap] = useState('90000')
  const [busy, setBusy] = useState(false)
  if (!state) return <div className="panel-stack"><p className="empty-copy">等待园区数据</p></div>
  const alerts = state.alerts.filter(a => !a.acknowledged)
  const locate = (a: typeof alerts[number]) => {
    const source = a.source.toLowerCase()
    const page = source.includes('storage') || source.includes('储能') ? 'storage' : source.includes('hvac') || source.includes('冷') ? 'hvac' : source.includes('agent') ? 'agent_flow' : 'overview'
    setActivePage(page)
    setContextSelection({ kind: 'alert', id: a.alert_id, label: a.message, page, detail: { source: a.source, severity: a.severity, time: a.timestamp } })
  }
  const apply = async () => {
    const value = Number(cap)
    if (!Number.isFinite(value) || value <= 0) { window.alert('请输入有效的需量上限。'); return }
    if (!window.confirm(`确认应用“${strategy === 'cost' ? '电费最优' : strategy === 'carbon' ? '低碳优先' : '需量抑制'}”策略，并将需量上限设为 ${cap} kW？`)) return
    setBusy(true)
    try { await submitControlAction({ system: 'overview', action: '应用园区策略', target: 'demand_cap_kw', value, unit: 'kW', reason: `优化目标=${strategy}`, actor: 'engineer' }) } catch { /* store 已展示服务端错误 */ } finally { setBusy(false) }
  }
  const undo = async () => {
    if (!window.confirm('确认撤销最近一次园区策略？')) return
    setBusy(true)
    try { await submitControlAction({ system: 'overview', action: '撤销园区策略', target: 'demand_cap_kw', value: 0, unit: 'kW', reason: '工程师手工撤销', actor: 'engineer' }) } catch { /* store 已展示服务端错误 */ } finally { setBusy(false) }
  }
  return <div className="panel-stack">
    <NaturalLanguagePrompt placeholder="例如：今天 14:00 园区负荷增加 5 MW，并说明重算影响…" />
    <Card title="日前负荷审批报告"><ApprovalReportButton gateId="forecast_approval" /></Card>
    {contextSelection && <Card title="当前选择" icon={<Crosshair className="icon-sm industrial" />}><div className="data-list"><div className="data-row"><span>对象</span><strong>{contextSelection.label}</strong></div>{Object.entries(contextSelection.detail || {}).map(([k, v]) => <div className="data-row" key={k}><span>{k}</span><strong>{String(v)}</strong></div>)}</div></Card>}
    <Card title="关键指标" icon={<Settings className="icon-sm industrial" />}><div className="data-list"><Row label="实时负荷" value={`${(state.load_kw / 1000).toFixed(1)} MW`} /><Row label="储能 SOC" value={`${(state.storage_soc * 100).toFixed(1)}%`} /><Row label="碳因子" value={`${state.carbon_factor.toFixed(4)} kg/kWh`} /><Row label="室外温度" value={`${state.weather.temp_c.toFixed(1)} °C`} /><Row label="湿度" value={`${state.weather.humidity.toFixed(0)}%`} /></div></Card>
    <Card title="未确认告警" icon={<AlertTriangle className="icon-sm" style={{ color: '#d97706' }} />}><div className="p-2 space-y-2">{alerts.length === 0 && <p className="empty-copy">当前无未确认告警</p>}{alerts.slice(0, 8).map(a => <div key={a.alert_id} className="rounded border border-slate-200 bg-white p-2"><div className="flex gap-2"><StatusDot status={a.severity} /><div className="min-w-0 flex-1"><strong className="block text-[11px] text-slate-700">{a.message}</strong><span className="text-[9px] text-slate-400">{a.source}</span></div></div><div className="button-row mt-2"><button className="btn" onClick={() => locate(a)}><Crosshair className="inline h-3 w-3" /> 定位</button><button className="btn" onClick={() => acknowledgeAlert(a.alert_id)}>确认告警</button></div></div>)}</div></Card>
    <Card title="园区控制策略" icon={<SlidersHorizontal className="icon-sm industrial" />}><div className="form-grid"><label>优化目标<select value={strategy} onChange={e => setStrategy(e.target.value)}><option value="cost">电费最优</option><option value="carbon">低碳优先</option><option value="demand">需量抑制</option></select></label><label>需量上限（kW）<input type="number" min="30000" max="120000" value={cap} onChange={e => setCap(e.target.value)} /></label><div className="button-row"><button disabled={busy} className="btn primary" onClick={apply}>{busy ? '提交中…' : '确认应用'}</button><button disabled={busy} className="btn" onClick={undo}>撤销</button></div></div></Card>
    <ActionHistory />
  </div>
}
function Row({ label, value }: { label: string; value: string }) { return <div className="data-row"><span>{label}</span><strong>{value}</strong></div> }
