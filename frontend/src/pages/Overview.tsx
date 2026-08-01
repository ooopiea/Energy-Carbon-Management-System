import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import { useAppStore } from '../stores/appStore'
import { Card, KpiCard } from '../components/Layout'
import { Zap, Sun, Grid3x3, Battery, Leaf, TrendingUp, Thermometer, DollarSign } from 'lucide-react'

export function Overview() {
  const { state } = useAppStore()
  if (!state) return <div className="text-slate-400">加载中...</div>

  const step = state.time.step
  const da = state.day_ahead
  const series = state.series

  const loadChartOption = useMemo(() => {
    const xData = Array.from({ length: 96 }, (_, i) => `${String(Math.floor(i / 4)).padStart(2, '0')}:${String((i % 4) * 15).padStart(2, '0')}`)
    const seriesList: any[] = []

    if (da.load_forecast.length > 0) {
      seriesList.push({
        name: '负荷预测', type: 'line', smooth: true, symbol: 'none',
        data: da.load_forecast.map((v: number) => Math.round(v)),
        lineStyle: { width: 2, color: '#3b82f6' },
        areaStyle: { color: 'rgba(59,130,246,0.08)' },
        markLine: step > 0 ? { silent: true, symbol: 'none', data: [{ xAxis: step, lineStyle: { color: '#ef4444', type: 'dashed', width: 1.5 }, label: { formatter: '当前', position: 'start' } }] } : undefined,
      })
    }
    if (da.storage_plan.length > 0) {
      seriesList.push({
        name: '储能功率', type: 'line', smooth: true, symbol: 'none',
        data: da.storage_plan.map((v: number) => Math.round(v)),
        lineStyle: { width: 1.5, color: '#f59e0b' },
      })
    }
    if (series.load) {
      seriesList.push({
        name: '实际负荷', type: 'line', smooth: true, symbol: 'none',
        data: series.load.map((p: any) => Math.round(p.value)),
        lineStyle: { width: 2.5, color: '#22c55e' },
      })
    }

    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['负荷预测', '储能功率', '实际负荷'], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 60, right: 20, top: 35, bottom: 30 },
      xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 10, interval: 11 } },
      yAxis: { type: 'value', name: 'kW', axisLabel: { fontSize: 10 } },
      series: seriesList,
    }
  }, [da, series, step])

  const carbonChartOption = useMemo(() => {
    const xData = Array.from({ length: 96 }, (_, i) => `${String(Math.floor(i / 4)).padStart(2, '0')}:00`)
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['C(τ) 直接因子', 'Cr(τ) 责任因子'], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 50, right: 20, top: 35, bottom: 30 },
      xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 10, interval: 11 } },
      yAxis: { type: 'value', name: 'kgCO2/kWh', axisLabel: { fontSize: 10 } },
      series: [
        { name: 'C(τ) 直接因子', type: 'line', smooth: true, symbol: 'none', data: da.carbon_c, lineStyle: { width: 2, color: '#64748b' } },
        { name: 'Cr(τ) 责任因子', type: 'line', smooth: true, symbol: 'none', data: da.carbon_cr, lineStyle: { width: 2, color: '#8b5cf6' }, areaStyle: { color: 'rgba(139,92,246,0.08)' } },
      ],
    }
  }, [da])

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold text-slate-800">综合能源可视化</h2>
        <div className="flex items-center gap-2 text-xs">
          <span className="px-2 py-1 rounded bg-orange-50 text-orange-600 border border-orange-200">
            {state.tariff_period === 'sharp' ? '尖' : state.tariff_period === 'peak' ? '峰' : state.tariff_period === 'valley' ? '谷' : '平'}时段
          </span>
          <span className="px-2 py-1 rounded bg-blue-50 text-blue-600 border border-blue-200">{state.price.toFixed(4)} 元/kWh</span>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-3">
        <KpiCard label="实时负荷" value={(state.load_kw / 1000).toFixed(1)} unit="MW" color="#3b82f6" icon={<Zap className="w-4 h-4 text-blue-400" />} />
        <KpiCard label="光伏发电" value={(state.solar_kw / 1000).toFixed(2)} unit="MW" color="#f59e0b" icon={<Sun className="w-4 h-4 text-amber-400" />} />
        <KpiCard label="电网购电" value={(state.grid_kw / 1000).toFixed(1)} unit="MW" color="#64748b" icon={<Grid3x3 className="w-4 h-4 text-slate-400" />} />
        <KpiCard label="储能功率" value={state.storage_power_kw.toFixed(0)} unit="kW" color="#22c55e" icon={<Battery className="w-4 h-4 text-green-400" />} />
        <KpiCard label="HVAC功率" value={(state.hvac_power_kw / 1000).toFixed(1)} unit="MW" color="#06b6d4" icon={<Thermometer className="w-4 h-4 text-cyan-400" />} />
        <KpiCard label="碳因子" value={state.carbon_factor.toFixed(4)} unit="kg/kWh" color="#8b5cf6" icon={<Leaf className="w-4 h-4 text-violet-400" />} />
        <KpiCard label="日电费" value={(state.daily.cost_cny / 10000).toFixed(1)} unit="万元" color="#ef4444" icon={<DollarSign className="w-4 h-4 text-red-400" />} />
        <KpiCard label="日碳排放" value={(state.daily.carbon_kg / 1000).toFixed(1)} unit="tCO2" color="#78716c" icon={<TrendingUp className="w-4 h-4 text-stone-400" />} />
      </div>

      <Card title="日前负荷与储能调度" icon={<Zap className="w-4 h-4 text-blue-500" />}>
        <ReactECharts option={loadChartOption} style={{ height: 280 }} />
      </Card>

      <div className="grid grid-cols-2 gap-4">
        <Card title="电碳因子 C(τ) / Cr(τ)" icon={<Leaf className="w-4 h-4 text-violet-500" />}>
          <ReactECharts option={carbonChartOption} style={{ height: 220 }} />
        </Card>
        <Card title="电价时段" icon={<DollarSign className="w-4 h-4 text-red-500" />}>
          <ReactECharts option={{
            tooltip: { trigger: 'axis' },
            grid: { left: 50, right: 20, top: 20, bottom: 30 },
            xAxis: { type: 'category', data: Array.from({ length: 96 }, (_, i) => i), axisLabel: { fontSize: 10, interval: 11 } },
            yAxis: { type: 'value', name: '元/kWh', axisLabel: { fontSize: 10 } },
            series: [{ type: 'line', step: 'end', symbol: 'none', data: da.price, lineStyle: { width: 2, color: '#ef4444' }, areaStyle: { color: 'rgba(239,68,68,0.08)' } }],
          }} style={{ height: 220 }} />
        </Card>
      </div>
    </div>
  )
}