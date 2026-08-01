import ReactECharts from 'echarts-for-react'
import { useAppStore } from '../stores/appStore'
import { Card, KpiCard } from '../components/Layout'
import { Wind, Thermometer, Gauge, Snowflake, Fan, Waves } from 'lucide-react'

const xData = Array.from({ length: 96 }, (_, i) => `${String(Math.floor(i / 4)).padStart(2, '0')}:${String((i % 4) * 15).padStart(2, '0')}`)

export function HVACPage() {
  const { state, contextSelection, setContextSelection } = useAppStore()
  if (!state) return null
  const hs = state.hvac_summary
  const totalChillers = state.chiller_topology.reduce((s, c) => s + c.chiller_count, 0)
  const activeCount = hs?.active_chillers[state.time.step] || 0
  const select = (id: string, label: string, detail: Record<string, string | number>) => setContextSelection({ kind: 'device', id, label, page: 'hvac', detail })
  const selectPoint = (params: any) => setContextSelection({ kind: 'point', id: `hvac-${params.seriesName}-${params.dataIndex}`, label: params.seriesName || 'HVAC 曲线点', page: 'hvac', detail: { time: xData[params.dataIndex], value: Number(params.value), series: params.seriesName || '' } })
  const base = { tooltip: { trigger: 'axis' }, grid: { left: 56, right: 42, top: 40, bottom: 34 }, xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 9, interval: 11 } } }
  return <div className="page-stack">
    <div className="page-title-row"><div><h1>HVAC 系统</h1><p>冷站设备工况、供回水网络与负荷调节</p></div></div>
    <div className="kpi-grid"><KpiCard label="HVAC 功率" value={(state.hvac_power_kw / 1000).toFixed(1)} unit="MW" color="#176b87" icon={<Wind />} /><KpiCard label="供水温度" value={state.hvac_supply_temp_c.toFixed(1)} unit="°C" icon={<Snowflake />} /><KpiCard label="平均 COP" value={hs ? hs.avg_cop.toFixed(2) : '—'} color="#15803d" icon={<Gauge />} /><KpiCard label="投运冷机" value={activeCount} unit={`/ ${totalChillers} 台`} color="#6d5b8c" icon={<Fan />} /></div>
    <Card title="冷站水系统工况" icon={<Waves className="icon-sm industrial" />}><div className="hvac-topology">
      <HvacDevice id="chillers" label="冷水机组" value={`${activeCount}/${totalChillers} 台`} active={contextSelection?.id === 'chillers'} onClick={() => select('chillers', '冷水机组', { active: `${activeCount}/${totalChillers} 台`, power: `${(state.hvac_power_kw / 1000).toFixed(1)} MW`, cop: hs?.cop[state.time.step]?.toFixed(2) || '—' })} icon={<Snowflake />} />
      <span className="pipe supply">供水 {state.hvac_supply_temp_c.toFixed(1)} °C</span>
      <HvacDevice id="pumps" label="冷冻水泵" value="变频运行" active={contextSelection?.id === 'pumps'} onClick={() => select('pumps', '冷冻水泵', { status: '变频运行', flow: `${(820 + state.hvac_power_kw / 100).toFixed(0)} m³/h` })} icon={<Fan />} />
      <span className="pipe supply">送往末端</span>
      <HvacDevice id="terminals" label="生产末端" value="工艺冷负荷" active={contextSelection?.id === 'terminals'} onClick={() => select('terminals', '生产冷负荷末端', { load: `${(state.hvac_power_kw * (hs?.cop[state.time.step] || 4.5) / 1000).toFixed(1)} MWth` })} icon={<Wind />} />
      <span className="pipe return">回水 {hs?.return_temp_c[state.time.step]?.toFixed(1) || '—'} °C</span>
    </div></Card>
    <div className="two-col">
      <Card title="供回水温度" icon={<Thermometer className="icon-sm industrial" />}><ReactECharts option={{ ...base, legend: { top: 5, textStyle: { fontSize: 10 } }, yAxis: { type: 'value', name: '°C', axisLabel: { fontSize: 9 } }, series: [{ name: '供水温度', type: 'line', symbol: 'none', data: hs?.supply_temp_c || [], lineStyle: { color: '#176b87', width: 2 } }, { name: '回水温度', type: 'line', symbol: 'none', data: hs?.return_temp_c || [], lineStyle: { color: '#d97706', width: 2 } }] }} onEvents={{ click: selectPoint }} style={{ height: 260 }} /></Card>
      <Card title="COP 与运行功率" icon={<Gauge className="icon-sm industrial" />}><ReactECharts option={{ ...base, legend: { top: 5, textStyle: { fontSize: 10 } }, yAxis: [{ type: 'value', name: 'COP', axisLabel: { fontSize: 9 } }, { type: 'value', name: 'kW', axisLabel: { fontSize: 9 } }], series: [{ name: 'COP', type: 'line', symbol: 'none', data: hs?.cop || [], lineStyle: { color: '#15803d', width: 2 } }, { name: '运行功率', type: 'line', symbol: 'none', yAxisIndex: 1, data: hs?.power_kw || [], lineStyle: { color: '#176b87', width: 2 } }] }} onEvents={{ click: selectPoint }} style={{ height: 260 }} /></Card>
    </div>
    <Card title="冷机站群" icon={<Snowflake className="icon-sm industrial" />}><div className="station-grid">{state.chiller_topology.map((station, i) => { const allocated = Math.min(station.chiller_count, Math.max(0, activeCount - state.chiller_topology.slice(0, i).reduce((n, s) => n + s.chiller_count, 0))); return <button key={station.name} className={contextSelection?.id === `station-${i}` ? 'active' : ''} onClick={() => select(`station-${i}`, station.name, { active: `${allocated}/${station.chiller_count} 台`, ratedPower: `${(station.total_rated_kw / 1000).toFixed(1)} MW` })}><Snowflake /><strong>{station.name}</strong><span>{allocated}/{station.chiller_count} 台运行</span><small>{(station.total_rated_kw / 1000).toFixed(1)} MW 额定</small></button>})}</div></Card>
  </div>
}
function HvacDevice({ label, value, icon, active, onClick }: { id: string; label: string; value: string; icon: React.ReactNode; active: boolean; onClick: () => void }) { return <button className={`hvac-device ${active ? 'active' : ''}`} onClick={onClick}>{icon}<strong>{label}</strong><span>{value}</span></button> }
