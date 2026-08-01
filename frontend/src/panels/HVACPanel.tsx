import { useAppStore } from '../stores/appStore'
import { Card } from '../components/Layout'
import { Wind, Snowflake, Thermometer, Gauge } from 'lucide-react'

export function HVACPanel() {
  const { state } = useAppStore()
  if (!state) return null
  const hs = state.hvac_summary

  return (
    <div className="p-3 space-y-3">
      <Card title="实时参数" icon={<Wind className="w-4 h-4 text-cyan-500" />}>
        <div className="p-3 space-y-2 text-sm">
          <Row label="运行功率" value={`${(state.hvac_power_kw / 1000).toFixed(1)} MW`} />
          <Row label="供水温度" value={`${state.hvac_supply_temp_c.toFixed(1)} °C`} />
          <Row label="室外温度" value={`${state.weather.temp_c.toFixed(1)} °C`} />
          <Row label="当前COP" value={hs ? hs.cop[state.time.step]?.toFixed(2) || '-' : '-'} />
        </div>
      </Card>

      <Card title="冷机群状态" icon={<Snowflake className="w-4 h-4 text-blue-500" />}>
        <div className="p-3 space-y-1.5 text-sm">
          {state.chiller_topology.map((s: any, i: number) => (
            <div key={i} className="flex items-center justify-between">
              <span className="text-slate-600 text-xs">{s.name}</span>
              <span className="text-xs text-slate-400">{s.chiller_count}台 · {(s.total_rated_kw / 1000).toFixed(1)}MW</span>
            </div>
          ))}
        </div>
      </Card>

      {hs && (
        <Card title="调度摘要" icon={<Gauge className="w-4 h-4 text-green-500" />}>
          <div className="p-3 space-y-2 text-sm">
            <Row label="平均COP" value={hs.avg_cop.toFixed(2)} />
            <Row label="电费节省" value={`${hs.saving_cny.toFixed(0)} 元`} />
            <Row label="供水设定" value="7.0 °C" />
            <Row label="回水上限" value="12.0 °C" />
            <Row label="冰蓄冷容量" value="5000 kWh" />
          </div>
        </Card>
      )}
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-slate-500">{label}</span>
      <span className="font-medium text-slate-700">{value}</span>
    </div>
  )
}