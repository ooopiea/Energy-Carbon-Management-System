import { useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { ActionHistory, Card } from '../components/Layout'
import { Wind, SlidersHorizontal, Crosshair } from 'lucide-react'

export function HVACPanel() {
  const { state, contextSelection, submitControlAction } = useAppStore()
  const [supply, setSupply] = useState('7.0')
  const [returnLimit, setReturnLimit] = useState('12.0')
  const [mode, setMode] = useState('auto')
  const [busy, setBusy] = useState(false)
  if (!state) return <div className="panel-stack"><p className="empty-copy">等待 HVAC 数据</p></div>
  const hs = state.hvac_summary
  const submit = async () => {
    const s = Number(supply), r = Number(returnLimit)
    if (!Number.isFinite(s) || !Number.isFinite(r) || s < 5 || s > 12 || r <= s || r > 18) { window.alert('供水设定需在 5～12°C，回水上限需高于供水且不超过 18°C。'); return }
    if (!window.confirm(`确认下发 HVAC 控制设定？\n模式：${mode}\n供水：${s}°C\n回水上限：${r}°C`)) return
    setBusy(true)
    try {
      await submitControlAction({ system: 'hvac', action: '下发 HVAC 设定', target: 'supply_temp_c', value: s, unit: '°C', reason: `模式=${mode}；回水上限=${r}°C`, actor: 'engineer' })
    } catch { /* 409/422/网络错误由 store 转换为明确反馈 */ } finally { setBusy(false) }
  }
  return <div className="panel-stack">
    <Card title={contextSelection?.page === 'hvac' ? contextSelection.label : 'HVAC 实时参数'} icon={<Wind className="icon-sm industrial" />}><div className="data-list"><Row label="运行功率" value={`${(state.hvac_power_kw / 1000).toFixed(1)} MW`} /><Row label="供水温度" value={`${state.hvac_supply_temp_c.toFixed(1)} °C`} /><Row label="回水温度" value={`${hs?.return_temp_c[state.time.step]?.toFixed(1) || '—'} °C`} /><Row label="室外温度" value={`${state.weather.temp_c.toFixed(1)} °C`} /><Row label="当前 COP" value={hs?.cop[state.time.step]?.toFixed(2) || '—'} />{contextSelection?.page === 'hvac' && Object.entries(contextSelection.detail || {}).map(([k,v]) => <Row key={k} label={k} value={String(v)} />)}</div></Card>
    {contextSelection?.kind === 'point' && contextSelection.page === 'hvac' && <Card title="曲线数据点" icon={<Crosshair className="icon-sm industrial" />}><div className="data-list">{Object.entries(contextSelection.detail || {}).map(([k, v]) => <Row key={k} label={k} value={String(v)} />)}</div></Card>}
    <Card title="控制设定" icon={<SlidersHorizontal className="icon-sm industrial" />}><div className="form-grid"><label>运行模式<select value={mode} onChange={e => setMode(e.target.value)}><option value="auto">自动优化</option><option value="comfort">工艺温控优先</option><option value="cost">电费优先</option></select></label><label>供水温度（°C）<input type="number" step="0.1" min="5" max="12" value={supply} onChange={e => setSupply(e.target.value)} /></label><label>回水温度上限（°C）<input type="number" step="0.1" min="7" max="18" value={returnLimit} onChange={e => setReturnLimit(e.target.value)} /></label><button disabled={busy} className="btn primary" onClick={submit}>{busy ? '提交中…' : '确认并下发'}</button></div></Card>
    <ActionHistory />
  </div>
}
function Row({ label, value }: { label: string; value: string }) { return <div className="data-row"><span>{label}</span><strong>{value}</strong></div> }
