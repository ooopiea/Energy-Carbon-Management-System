import { useAppStore } from '../stores/appStore'
import { Card, StatusDot } from '../components/Layout'
import { Battery, Thermometer, Zap, DollarSign } from 'lucide-react'

export function StoragePanel() {
  const { state } = useAppStore()
  if (!state) return null
  const ss = state.storage_summary
  const isCharging = state.storage_power_kw < 0

  return (
    <div className="p-3 space-y-3">
      <Card title="实时参数" icon={<Battery className="w-4 h-4 text-green-500" />}>
        <div className="p-3 space-y-2 text-sm">
          <Row label="SOC" value={`${(state.storage_soc * 100).toFixed(1)}%`} bar={state.storage_soc * 100} barColor="#22c55e" />
          <Row label="功率" value={`${Math.abs(state.storage_power_kw).toFixed(0)} kW (${isCharging ? '充电' : '放电'})`} />
          <Row label="电芯温度" value={`${state.storage_temp_c.toFixed(1)} °C`} bar={state.storage_temp_c / 45 * 100} barColor="#ef4444" />
          <Row label="状态" value={isCharging ? '充电中' : state.storage_power_kw > 0 ? '放电中' : '待机'} />
        </div>
      </Card>

      <Card title="设备参数" icon={<Zap className="w-4 h-4 text-blue-500" />}>
        <div className="p-3 space-y-2 text-sm">
          <Row label="额定容量" value="2000 kWh" />
          <Row label="额定功率" value="500 kW" />
          <Row label="SOC范围" value="10% - 90%" />
          <Row label="充放电效率" value="95%" />
          <Row label="温度上限" value="45 °C" />
          <Row label="爬坡率" value="300 kW/步" />
        </div>
      </Card>

      {ss && (
        <Card title="调度摘要" icon={<DollarSign className="w-4 h-4 text-amber-500" />}>
          <div className="p-3 space-y-2 text-sm">
            <Row label="优化目标" value="最小电费" />
            <Row label="电费节省" value={`${ss.saving_cny.toFixed(0)} 元`} />
            <Row label="峰值削减" value={`${ss.peak_reduction_kw.toFixed(0)} kW`} />
            <Row label="末端SOC" value={`${(ss.terminal_soc * 100).toFixed(1)}%`} />
            <Row label="最高温度" value={`${ss.max_temp_c.toFixed(1)} °C`} />
            <Row label="求解器" value={ss.solver_status} />
          </div>
        </Card>
      )}
    </div>
  )
}

function Row({ label, value, bar, barColor }: { label: string; value: string; bar?: number; barColor?: string }) {
  return (
    <div>
      <div className="flex items-center justify-between">
        <span className="text-slate-500">{label}</span>
        <span className="font-medium text-slate-700">{value}</span>
      </div>
      {bar !== undefined && (
        <div className="mt-1 h-1.5 rounded-full bg-slate-100 overflow-hidden">
          <div className="h-full rounded-full" style={{ width: `${Math.min(100, Math.max(0, bar))}%`, backgroundColor: barColor || '#3b82f6' }} />
        </div>
      )}
    </div>
  )
}