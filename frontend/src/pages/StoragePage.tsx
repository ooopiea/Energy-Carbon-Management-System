import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import { useAppStore } from '../stores/appStore'
import { Card, KpiCard } from '../components/Layout'
import { Battery, Thermometer, TrendingDown, DollarSign, Zap, Gauge } from 'lucide-react'

export function StoragePage() {
  const { state } = useAppStore()
  if (!state) return <div className="text-slate-400">加载中...</div>

  const ss = state.storage_summary
  const step = state.time.step
  const xData = Array.from({ length: 96 }, (_, i) => `${String(Math.floor(i / 4)).padStart(2, '0')}:${String((i % 4) * 15).padStart(2, '0')}`)

  const socOption = useMemo(() => {
    if (!ss) return {}
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 50, right: 20, top: 20, bottom: 30 },
      xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 10, interval: 11 } },
      yAxis: { type: 'value', name: 'SOC %', min: 0, max: 100, axisLabel: { fontSize: 10 } },
      series: [{
        type: 'line', smooth: true, symbol: 'none',
        data: ss.soc_ratio.map((v: number) => +(v * 100).toFixed(1)),
        lineStyle: { width: 2, color: '#22c55e' },
        areaStyle: { color: 'rgba(34,197,94,0.12)' },
        markLine: step > 0 ? { silent: true, symbol: 'none', data: [{ xAxis: step, lineStyle: { color: '#ef4444', type: 'dashed' } }] } : undefined,
      }],
    }
  }, [ss, step])

  const powerOption = useMemo(() => {
    if (!ss) return {}
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['充放电功率', '电网购电', '负荷'], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 60, right: 20, top: 35, bottom: 30 },
      xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 10, interval: 11 } },
      yAxis: { type: 'value', name: 'kW', axisLabel: { fontSize: 10 } },
      series: [
        { name: '充放电功率', type: 'bar', data: ss.power_kw.map((v: number) => Math.round(v)), itemStyle: { color: (p: any) => p.value >= 0 ? '#22c55e' : '#3b82f6' } },
        { name: '电网购电', type: 'line', smooth: true, symbol: 'none', data: ss.grid_kw.map((v: number) => Math.round(v)), lineStyle: { width: 1.5, color: '#94a3b8' } },
        { name: '负荷', type: 'line', smooth: true, symbol: 'none', data: state.day_ahead.load_forecast.map((v: number) => Math.round(v)), lineStyle: { width: 1.5, color: '#cbd5e1', type: 'dashed' } },
      ],
    }
  }, [ss, state])

  const tempOption = useMemo(() => {
    if (!ss) return {}
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 50, right: 20, top: 20, bottom: 30 },
      xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 10, interval: 11 } },
      yAxis: { type: 'value', name: '°C', axisLabel: { fontSize: 10 } },
      series: [{
        type: 'line', smooth: true, symbol: 'none',
        data: ss.temp_c, lineStyle: { width: 2, color: '#ef4444' },
        markLine: { silent: true, data: [{ yAxis: 45, lineStyle: { color: '#ef4444', type: 'dashed' }, label: { formatter: '安全上限', fontSize: 10 } }] },
      }],
    }
  }, [ss])

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-bold text-slate-800">储能系统</h2>

      <div className="grid grid-cols-4 gap-3">
        <KpiCard label="当前SOC" value={(state.storage_soc * 100).toFixed(1)} unit="%" color="#22c55e" icon={<Battery className="w-4 h-4 text-green-400" />} />
        <KpiCard label="充放电功率" value={state.storage_power_kw.toFixed(0)} unit="kW" color="#3b82f6" icon={<Zap className="w-4 h-4 text-blue-400" />} />
        <KpiCard label="电芯温度" value={state.storage_temp_c.toFixed(1)} unit="°C" color="#ef4444" icon={<Thermometer className="w-4 h-4 text-red-400" />} />
        <KpiCard label="日省电费" value={ss ? ss.saving_cny.toFixed(0) : '-'} unit="元" color="#f59e0b" icon={<DollarSign className="w-4 h-4 text-amber-400" />} />
      </div>

      <Card title="SOC 变化曲线" icon={<Battery className="w-4 h-4 text-green-500" />}>
        {ss && <ReactECharts option={socOption} style={{ height: 240 }} />}
      </Card>

      <div className="grid grid-cols-2 gap-4">
        <Card title="充放电调度" icon={<Zap className="w-4 h-4 text-blue-500" />}>
          {ss && <ReactECharts option={powerOption} style={{ height: 260 }} />}
        </Card>
        <Card title="电芯温度" icon={<Thermometer className="w-4 h-4 text-red-500" />}>
          {ss && <ReactECharts option={tempOption} style={{ height: 260 }} />}
        </Card>
      </div>

      {ss && (
        <Card title="调度方案摘要" icon={<Gauge className="w-4 h-4 text-slate-500" />}>
          <div className="grid grid-cols-4 gap-3 p-4 text-sm">
            <div><span className="text-slate-500">优化目标</span><div className="font-semibold text-slate-700">最小电费</div></div>
            <div><span className="text-slate-500">求解器状态</span><div className="font-semibold text-green-600">{ss.solver_status}</div></div>
            <div><span className="text-slate-500">峰值削减</span><div className="font-semibold text-slate-700">{ss.peak_reduction_kw.toFixed(0)} kW</div></div>
            <div><span className="text-slate-500">末端SOC</span><div className="font-semibold text-slate-700">{(ss.terminal_soc * 100).toFixed(1)}%</div></div>
            <div><span className="text-slate-500">最高温度</span><div className="font-semibold text-slate-700">{ss.max_temp_c.toFixed(1)} °C</div></div>
            <div><span className="text-slate-500">电费节省</span><div className="font-semibold text-amber-600">{ss.saving_cny.toFixed(0)} 元</div></div>
            <div><span className="text-slate-500">容量</span><div className="font-semibold text-slate-700">2000 kWh</div></div>
            <div><span className="text-slate-500">额定功率</span><div className="font-semibold text-slate-700">500 kW</div></div>
          </div>
        </Card>
      )}
    </div>
  )
}