import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import { useAppStore } from '../stores/appStore'
import { Card, KpiCard } from '../components/Layout'
import { Wind, Thermometer, Gauge, Snowflake, Fan } from 'lucide-react'

export function HVACPage() {
  const { state } = useAppStore()
  if (!state) return <div className="text-slate-400">加载中...</div>

  const hs = state.hvac_summary
  const step = state.time.step
  const xData = Array.from({ length: 96 }, (_, i) => `${String(Math.floor(i / 4)).padStart(2, '0')}:${String((i % 4) * 15).padStart(2, '0')}`)

  const tempOption = useMemo(() => {
    if (!hs) return {}
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['供水温度', '回水温度'], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 50, right: 20, top: 35, bottom: 30 },
      xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 10, interval: 11 } },
      yAxis: { type: 'value', name: '°C', axisLabel: { fontSize: 10 } },
      series: [
        { name: '供水温度', type: 'line', smooth: true, symbol: 'none', data: hs.supply_temp_c, lineStyle: { width: 2, color: '#06b6d4' } },
        { name: '回水温度', type: 'line', smooth: true, symbol: 'none', data: hs.return_temp_c, lineStyle: { width: 2, color: '#f59e0b' } },
      ],
    }
  }, [hs])

  const copOption = useMemo(() => {
    if (!hs) return {}
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['COP', '功率', '投运冷机'], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 50, right: 50, top: 35, bottom: 30 },
      xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 10, interval: 11 } },
      yAxis: [
        { type: 'value', name: 'COP', position: 'left', axisLabel: { fontSize: 10 } },
        { type: 'value', name: 'kW', position: 'right', axisLabel: { fontSize: 10 } },
      ],
      series: [
        { name: 'COP', type: 'line', smooth: true, symbol: 'none', data: hs.cop, lineStyle: { width: 2, color: '#22c55e' }, yAxisIndex: 0 },
        { name: '功率', type: 'line', smooth: true, symbol: 'none', data: hs.power_kw.map((v: number) => Math.round(v)), lineStyle: { width: 2, color: '#3b82f6' }, yAxisIndex: 1 },
        { name: '投运冷机', type: 'bar', data: hs.active_chillers, itemStyle: { color: 'rgba(139,92,246,0.3)' }, yAxisIndex: 1 },
      ],
    }
  }, [hs])

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-bold text-slate-800">HVAC 系统</h2>

      <div className="grid grid-cols-4 gap-3">
        <KpiCard label="HVAC功率" value={(state.hvac_power_kw / 1000).toFixed(1)} unit="MW" color="#06b6d4" icon={<Wind className="w-4 h-4 text-cyan-400" />} />
        <KpiCard label="供水温度" value={state.hvac_supply_temp_c.toFixed(1)} unit="°C" color="#3b82f6" icon={<Snowflake className="w-4 h-4 text-blue-400" />} />
        <KpiCard label="平均COP" value={hs ? hs.avg_cop.toFixed(2) : '-'} color="#22c55e" icon={<Gauge className="w-4 h-4 text-green-400" />} />
        <KpiCard label="冷机总数" value={hs ? state.chiller_topology.reduce((s: number, c: any) => s + c.chiller_count, 0) : '-'} unit="台" color="#8b5cf6" icon={<Fan className="w-4 h-4 text-violet-400" />} />
      </div>

      <Card title="冷机群工况示意" icon={<Snowflake className="w-4 h-4 text-cyan-500" />}>
        <div className="grid grid-cols-5 gap-3 p-4">
          {state.chiller_topology.map((station: any, i: number) => {
            const activeAtStep = hs ? hs.active_chillers[step] || 0 : 0
            const ratio = station.chiller_count > 0 ? Math.min(1, activeAtStep / state.chiller_topology.reduce((s: number, c: any) => s + c.chiller_count, 0)) : 0
            return (
              <div key={i} className="border border-slate-200 rounded-lg p-3 text-center">
                <Snowflake className="w-6 h-6 mx-auto mb-1" style={{ color: ratio > 0.3 ? '#06b6d4' : '#cbd5e1' }} />
                <div className="text-xs font-semibold text-slate-700">{station.name}</div>
                <div className="text-[10px] text-slate-400">{station.chiller_count}台 × {station.per_unit_kw}kW</div>
                <div className="text-[10px] text-slate-500 mt-1">总额定 {(station.total_rated_kw / 1000).toFixed(1)}MW</div>
              </div>
            )
          })}
        </div>
      </Card>

      <div className="grid grid-cols-2 gap-4">
        <Card title="供回水温度" icon={<Thermometer className="w-4 h-4 text-blue-500" />}>
          {hs && <ReactECharts option={tempOption} style={{ height: 240 }} />}
        </Card>
        <Card title="COP 与功率" icon={<Gauge className="w-4 h-4 text-green-500" />}>
          {hs && <ReactECharts option={copOption} style={{ height: 240 }} />}
        </Card>
      </div>
    </div>
  )
}