import { useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { NaturalLanguagePrompt } from '../components/NaturalLanguagePrompt'
import { ActionHistory, Card } from '../components/Layout'
import { Battery, SlidersHorizontal, Zap } from 'lucide-react'
import { ApprovalReportButton } from '../components/ApprovalReportButton'

export function StoragePanel() {
  const { state, contextSelection, submitControlAction } = useAppStore()
  const [power, setPower] = useState('0')
  const [socFloor, setSocFloor] = useState('15')
  const [curveFile, setCurveFile] = useState('')
  const [busy, setBusy] = useState(false)
  if (!state) return <div className="panel-stack"><p className="empty-copy">等待储能数据</p></div>
  const isCharging = state.storage_power_kw < 0
  const submit = async () => {
    const numericPower = Number(power)
    const floor = Number(socFloor)
    if (!Number.isFinite(numericPower) || Math.abs(numericPower) > 15000 || !Number.isFinite(floor) || floor < 10 || floor > 90) { window.alert('功率需在 -15000～15000 kW，SOC 下限需在 10～90%。'); return }
    if (!window.confirm(`确认下发储能指令？\n目标功率：${numericPower} kW\nSOC 下限：${floor}%`)) return
    setBusy(true)
    try {
      await submitControlAction({ system: 'storage', action: '下发储能指令', target: 'power_kw', value: numericPower, unit: 'kW', reason: `SOC下限=${floor}%${curveFile ? `；参考曲线=${curveFile}` : ''}`, actor: 'engineer' })
    } catch { /* 409/422/网络错误由 store 转换为明确反馈 */ } finally { setBusy(false) }
  }
  return <div className="panel-stack">
    <NaturalLanguagePrompt placeholder="例如：今天 16:30 储能系统停机 1 小时，重新计算调度…" />
    <Card title="储能审批报告"><ApprovalReportButton gateId="storage_approval" /></Card>
    <Card title={contextSelection?.page === 'storage' ? contextSelection.label : '储能实时参数'} icon={<Battery className="icon-sm industrial" />}><div className="data-list"><Row label="SOC" value={`${(state.storage_soc * 100).toFixed(1)}%`} bar={state.storage_soc * 100} /><Row label="SOH（估算）" value="97.6%" bar={97.6} /><Row label="功率" value={`${Math.abs(state.storage_power_kw).toFixed(0)} kW · ${isCharging ? '充电' : state.storage_power_kw > 0 ? '放电' : '待机'}`} /><Row label="电芯温度" value={`${state.storage_temp_c.toFixed(1)} °C`} bar={state.storage_temp_c / 45 * 100} /><Row label="直流电压（估算）" value="768 V" /><Row label="直流电流（估算）" value={`${Math.abs(state.storage_power_kw / .768).toFixed(0)} A`} /><Row label="当前电价" value={`${state.price.toFixed(4)} 元/kWh`} /></div></Card>
    {contextSelection?.kind === 'point' && contextSelection.page === 'storage' && <Card title="曲线数据点" icon={<Zap className="icon-sm industrial" />}><div className="data-list">{Object.entries(contextSelection.detail || {}).map(([k, v]) => <div className="data-row" key={k}><span>{k}</span><strong>{String(v)}</strong></div>)}</div></Card>}
    <Card title="人工调度" icon={<SlidersHorizontal className="icon-sm industrial" />}><div className="form-grid"><label>目标功率（kW，正值放电）<input type="number" min="-15000" max="15000" value={power} onChange={e => setPower(e.target.value)} /></label><label>SOC 安全下限（%）<input type="number" min="10" max="90" value={socFloor} onChange={e => setSocFloor(e.target.value)} /></label><label>上传 96 点目标曲线<input type="file" accept=".csv,.xlsx" onChange={e => setCurveFile(e.target.files?.[0]?.name || '')} />{curveFile && <small>已选择：{curveFile}</small>}</label><button disabled={busy} className="btn primary" onClick={submit}>{busy ? '提交中…' : '确认并下发'}</button></div></Card>
    <ActionHistory />
  </div>
}
function Row({ label, value, bar }: { label: string; value: string; bar?: number }) { return <div><div className="data-row"><span>{label}</span><strong>{value}</strong></div>{bar !== undefined && <div className="bar"><i style={{ width: `${Math.max(0, Math.min(100, bar))}%` }} /></div>}</div> }
