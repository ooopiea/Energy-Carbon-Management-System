import ReactECharts from 'echarts-for-react'
import { useAppStore } from '../stores/appStore'
import { Card, KpiCard } from '../components/Layout'
import { Battery, Thermometer, DollarSign, Zap, Gauge, Boxes } from 'lucide-react'

const xData = Array.from({ length: 96 }, (_, i) => `${String(Math.floor(i / 4)).padStart(2, '0')}:${String((i % 4) * 15).padStart(2, '0')}`)

export function StoragePage() {
  const { state, contextSelection, setContextSelection } = useAppStore()
  if (!state) return null
  const ss = state.storage_summary
  const selectDevice = (id: string, label: string) => setContextSelection({ kind: 'device', id, label, page: 'storage', detail: { soc: `${(state.storage_soc * 100).toFixed(1)}%`, power: `${state.storage_power_kw.toFixed(0)} kW`, temperature: `${state.storage_temp_c.toFixed(1)} °C` } })
  const selectPoint = (params: any) => setContextSelection({ kind: 'point', id: `storage-${params.seriesName}-${params.dataIndex}`, label: params.seriesName || '储能曲线点', page: 'storage', detail: { time: xData[params.dataIndex], value: Number(params.value), series: params.seriesName || '' } })
  const chartBase = { tooltip: { trigger: 'axis' }, grid: { left: 58, right: 18, top: 40, bottom: 34 }, xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 9, interval: 11 } }, yAxis: { type: 'value', axisLabel: { fontSize: 9 } } }
  return <div className="page-stack">
    <div className="page-title-row"><div><h1>储能系统</h1><p>2 MWh 电池系统健康、调度计划与实际响应</p></div></div>
    <div className="kpi-grid"><KpiCard label="当前 SOC" value={(state.storage_soc * 100).toFixed(1)} unit="%" color="#15803d" icon={<Battery />} /><KpiCard label="充放电功率" value={state.storage_power_kw.toFixed(0)} unit="kW" icon={<Zap />} /><KpiCard label="电芯温度" value={state.storage_temp_c.toFixed(1)} unit="°C" color="#c8493d" icon={<Thermometer />} /><KpiCard label="日省电费" value={ss ? ss.saving_cny.toFixed(0) : '—'} unit="元" color="#d97706" icon={<DollarSign />} /></div>
    <Card title="储能设备拓扑" icon={<Boxes className="icon-sm industrial" />}><div className="storage-topology" aria-label="储能系统设备拓扑">
      <TopologyDevice id="grid-side" label="并网柜" sub="10 kV 母线" active={contextSelection?.id === 'grid-side'} onClick={() => selectDevice('grid-side', '并网柜')} />
      <span className="topology-link" />
      <TopologyDevice id="pcs" label="PCS 变流器" sub="500 kW" active={contextSelection?.id === 'pcs'} onClick={() => selectDevice('pcs', 'PCS 变流器')} />
      <span className="topology-link" />
      <TopologyDevice id="battery-cabinet" label="电池舱" sub="4 簇 · 2 MWh" active={contextSelection?.id === 'battery-cabinet'} onClick={() => selectDevice('battery-cabinet', '储能电池舱')} />
      <div className="cluster-row">{[1,2,3,4].map(n => <button className={contextSelection?.id === `cluster-${n}` ? 'active' : ''} key={n} onClick={() => selectDevice(`cluster-${n}`, `电池簇 ${n}`)}><Battery /><span>簇 {n}</span><small>{(state.storage_soc * 100 + (n - 2.5) * .4).toFixed(1)}%</small></button>)}</div>
    </div></Card>
    <div className="two-col">
      <Card title="SOC 计划与实际" icon={<Battery className="icon-sm industrial" />}><ReactECharts option={{ ...chartBase, legend: { top: 5, textStyle: { fontSize: 10 } }, yAxis: { ...chartBase.yAxis, name: 'SOC %', min: 0, max: 100 }, series: [{ name: '日前计划', type: 'line', symbol: 'none', data: state.day_ahead.soc_plan.map(v => v * 100), lineStyle: { color: '#8da1a6', type: 'dashed' } }, { name: '实际轨迹', type: 'line', symbol: 'none', data: ss?.soc_ratio.map(v => v * 100) || [], lineStyle: { color: '#15803d', width: 2 }, areaStyle: { color: 'rgba(21,128,61,.08)' } }] }} onEvents={{ click: selectPoint }} style={{ height: 260 }} /></Card>
      <Card title="充放电计划与实际" icon={<Zap className="icon-sm industrial" />}><ReactECharts option={{ ...chartBase, legend: { top: 5, textStyle: { fontSize: 10 } }, yAxis: { ...chartBase.yAxis, name: 'kW' }, series: [{ name: '日前计划', type: 'line', symbol: 'none', data: state.day_ahead.storage_plan, lineStyle: { color: '#8da1a6', type: 'dashed' } }, { name: '实际功率', type: 'bar', data: ss?.power_kw || [], itemStyle: { color: (p: any) => p.value >= 0 ? '#15803d' : '#176b87' } }] }} onEvents={{ click: selectPoint }} style={{ height: 260 }} /></Card>
    </div>
    <Card title="电芯温度与安全边界" icon={<Thermometer className="icon-sm industrial" />}><ReactECharts option={{ ...chartBase, yAxis: { ...chartBase.yAxis, name: '°C' }, series: [{ name: '电芯温度', type: 'line', symbol: 'none', data: ss?.temp_c || [], lineStyle: { color: '#c8493d', width: 2 }, markLine: { silent: true, data: [{ yAxis: 45, label: { formatter: '安全上限' }, lineStyle: { color: '#dc2626', type: 'dashed' } }] } }] }} onEvents={{ click: selectPoint }} style={{ height: 230 }} /></Card>
    {ss && <Card title="优化求解摘要" icon={<Gauge className="icon-sm industrial" />}><div className="summary-grid"><Summary label="求解状态" value={ss.solver_status} /><Summary label="峰值削减" value={`${ss.peak_reduction_kw.toFixed(0)} kW`} /><Summary label="末端 SOC" value={`${(ss.terminal_soc * 100).toFixed(1)}%`} /><Summary label="最高温度" value={`${ss.max_temp_c.toFixed(1)} °C`} /></div></Card>}
  </div>
}
function TopologyDevice({ label, sub, active, onClick }: { id: string; label: string; sub: string; active: boolean; onClick: () => void }) { return <button className={`topology-device ${active ? 'active' : ''}`} onClick={onClick}><strong>{label}</strong><span>{sub}</span></button> }
function Summary({ label, value }: { label: string; value: string }) { return <div><span>{label}</span><strong>{value}</strong></div> }
