import ReactECharts from 'echarts-for-react'
import { useAppStore } from '../stores/appStore'
import { Card, KpiCard } from '../components/Layout'
import { Zap, Sun, Grid3x3, Battery, Leaf, TrendingUp, Thermometer, DollarSign, Factory, AlertTriangle } from 'lucide-react'

const xData = Array.from({ length: 96 }, (_, i) => `${String(Math.floor(i / 4)).padStart(2, '0')}:${String((i % 4) * 15).padStart(2, '0')}`)

export function Overview() {
  const { state, contextSelection, setContextSelection } = useAppStore()
  if (!state) return null
  const da = state.day_ahead
  const selectPoint = (params: any) => setContextSelection({ kind: 'point', id: `${params.seriesName}-${params.dataIndex}`, label: params.seriesName || '曲线数据点', page: 'overview', detail: { time: xData[params.dataIndex] || '-', value: Number(params.value), unit: params.seriesName?.includes('因子') ? 'kgCO₂/kWh' : 'kW' } })
  const selectDevice = (id: string, label: string, detail: Record<string, string | number>) => setContextSelection({ kind: 'device', id, label, page: 'overview', detail })
  const loadSeries = [
    { name: '负荷预测', type: 'line', smooth: true, symbol: 'none', data: da.load_forecast, lineStyle: { width: 2, color: '#176b87' }, areaStyle: { color: 'rgba(23,107,135,.08)' } },
    { name: '储能功率', type: 'line', smooth: true, symbol: 'none', data: da.storage_plan, lineStyle: { width: 1.5, color: '#d97706' } },
    { name: '实际负荷', type: 'line', smooth: true, symbol: 'none', data: state.series.load?.map(p => p.value) || [], lineStyle: { width: 2.5, color: '#15803d' } },
  ]
  const baseGrid = { left: 58, right: 18, top: 42, bottom: 34 }
  return <div className="page-stack">
    <div className="page-title-row"><div><h1>综合能源态势</h1><p>黄花园区电、冷、储、光实时协同总览</p></div><div className="flex gap-2 text-[10px]"><span className="rounded border border-amber-200 bg-amber-50 px-2 py-1 text-amber-700">{periodLabel(state.tariff_period)}时段</span><span className="rounded border border-cyan-200 bg-cyan-50 px-2 py-1 text-cyan-800">{state.price.toFixed(4)} 元/kWh</span></div></div>
    <div className="kpi-grid">
      <KpiCard label="实时负荷" value={(state.load_kw / 1000).toFixed(1)} unit="MW" icon={<Zap />} />
      <KpiCard label="光伏发电" value={(state.solar_kw / 1000).toFixed(2)} unit="MW" color="#d97706" icon={<Sun />} />
      <KpiCard label="电网购电" value={(state.grid_kw / 1000).toFixed(1)} unit="MW" color="#52666d" icon={<Grid3x3 />} />
      <KpiCard label="储能功率" value={state.storage_power_kw.toFixed(0)} unit="kW" color="#15803d" icon={<Battery />} />
      <KpiCard label="HVAC功率" value={(state.hvac_power_kw / 1000).toFixed(1)} unit="MW" color="#1688a7" icon={<Thermometer />} />
      <KpiCard label="碳因子" value={state.carbon_factor.toFixed(4)} unit="kg/kWh" color="#6d5b8c" icon={<Leaf />} />
      <KpiCard label="日电费" value={(state.daily.cost_cny / 10000).toFixed(1)} unit="万元" color="#c8493d" icon={<DollarSign />} />
      <KpiCard label="日碳排放" value={(state.daily.carbon_kg / 1000).toFixed(1)} unit="tCO₂" color="#59686b" icon={<TrendingUp />} />
    </div>

    <Card title="园区能源平面图" icon={<Factory className="icon-sm industrial" />}>
      <div className="plant-map" aria-label="黄花园区设备位置图">
        <div className="plant-road">黄花园区能源母线</div>
        <Device id="grid" label="10 kV 变电站" status="正常供电" selected={contextSelection?.id === 'grid'} onClick={() => selectDevice('grid', '10 kV 变电站', { power: `${(state.grid_kw / 1000).toFixed(1)} MW`, status: '并网' })} icon={<Grid3x3 />} />
        <Device id="solar" label="屋顶光伏" status={`${(state.solar_kw / 1000).toFixed(2)} MW`} selected={contextSelection?.id === 'solar'} onClick={() => selectDevice('solar', '屋顶光伏', { power: `${(state.solar_kw / 1000).toFixed(2)} MW`, status: '发电中' })} icon={<Sun />} />
        <Device id="factory" label="生产车间" status={`${(state.load_kw / 1000).toFixed(1)} MW`} selected={contextSelection?.id === 'factory'} onClick={() => selectDevice('factory', '生产车间', { load: `${(state.load_kw / 1000).toFixed(1)} MW`, status: '生产中' })} icon={<Factory />} />
        <Device id="storage" label="储能站" status={`SOC ${(state.storage_soc * 100).toFixed(1)}%`} selected={contextSelection?.id === 'storage'} onClick={() => selectDevice('storage', '储能站', { soc: `${(state.storage_soc * 100).toFixed(1)}%`, power: `${state.storage_power_kw.toFixed(0)} kW` })} icon={<Battery />} />
        <Device id="hvac" label="制冷机房" status={`${(state.hvac_power_kw / 1000).toFixed(1)} MW`} selected={contextSelection?.id === 'hvac'} onClick={() => selectDevice('hvac', '制冷机房', { power: `${(state.hvac_power_kw / 1000).toFixed(1)} MW`, supply: `${state.hvac_supply_temp_c.toFixed(1)} °C` })} icon={<Thermometer />} />
        {state.alerts.some(a => !a.acknowledged) && <span className="map-alert" title="存在未确认告警"><AlertTriangle /></span>}
      </div>
    </Card>

    <Card title="日前负荷与储能调度" icon={<Zap className="icon-sm industrial" />}><ReactECharts option={{ tooltip: { trigger: 'axis' }, legend: { top: 5, textStyle: { fontSize: 10 } }, grid: baseGrid, xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 9, interval: 11 } }, yAxis: { type: 'value', name: 'kW', axisLabel: { fontSize: 9 } }, series: loadSeries }} onEvents={{ click: selectPoint }} style={{ height: 300 }} /></Card>
    <div className="two-col">
      <Card title="电碳责任因子" icon={<Leaf className="icon-sm industrial" />}><ReactECharts option={{ tooltip: { trigger: 'axis' }, legend: { top: 5, textStyle: { fontSize: 10 } }, grid: baseGrid, xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 9, interval: 11 } }, yAxis: { type: 'value', name: 'kgCO₂/kWh', axisLabel: { fontSize: 9 } }, series: [{ name: 'C(τ) 直接因子', type: 'line', symbol: 'none', data: da.carbon_c, lineStyle: { color: '#61767c' } }, { name: 'Cr(τ) 责任因子', type: 'line', symbol: 'none', data: da.carbon_cr, lineStyle: { color: '#6d5b8c', width: 2 } }] }} onEvents={{ click: selectPoint }} style={{ height: 240 }} /></Card>
      <Card title="分时电价" icon={<DollarSign className="icon-sm industrial" />}><ReactECharts option={{ tooltip: { trigger: 'axis' }, grid: { ...baseGrid, top: 22 }, xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 9, interval: 11 } }, yAxis: { type: 'value', name: '元/kWh', axisLabel: { fontSize: 9 } }, series: [{ name: '分时电价', type: 'line', step: 'end', symbol: 'none', data: da.price, lineStyle: { color: '#c8493d', width: 2 }, areaStyle: { color: 'rgba(200,73,61,.08)' } }] }} onEvents={{ click: selectPoint }} style={{ height: 240 }} /></Card>
    </div>
  </div>
}

function periodLabel(period: string) { return period === 'sharp' ? '尖' : period === 'peak' ? '峰' : period === 'valley' ? '谷' : '平' }
function Device({ id, label, status, icon, selected, onClick }: { id: string; label: string; status: string; icon: React.ReactNode; selected: boolean; onClick: () => void }) { return <button className={`plant-device ${id} ${selected ? 'selected' : ''}`} onClick={onClick}>{icon}<strong>{label}</strong><span>{status}</span></button> }
