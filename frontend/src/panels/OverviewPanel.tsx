import { useAppStore } from '../stores/appStore'
import { Card, StatusDot, statusLabel } from '../components/Layout'
import { AlertTriangle, FileText, Settings } from 'lucide-react'

export function OverviewPanel() {
  const { state, acknowledgeAlert } = useAppStore()
  if (!state) return null

  const alerts = state.alerts.filter(a => !a.acknowledged).slice(0, 8)
  const reports = state.reports.slice(0, 8)

  return (
    <div className="p-3 space-y-3">
      <Card title="关键指标" icon={<Settings className="w-4 h-4 text-slate-500" />}>
        <div className="p-3 space-y-2 text-sm">
          <Row label="实时负荷" value={`${(state.load_kw / 1000).toFixed(1)} MW`} />
          <Row label="光伏发电" value={`${(state.solar_kw / 1000).toFixed(2)} MW`} />
          <Row label="电网购电" value={`${(state.grid_kw / 1000).toFixed(1)} MW`} />
          <Row label="储能SOC" value={`${(state.storage_soc * 100).toFixed(1)}%`} />
          <Row label="碳因子" value={`${state.carbon_factor.toFixed(4)} kg/kWh`} />
          <Row label="室外温度" value={`${state.weather.temp_c.toFixed(1)} °C`} />
          <Row label="湿度" value={`${state.weather.humidity.toFixed(0)}%`} />
          <Row label="风速" value={`${state.weather.wind.toFixed(1)} m/s`} />
        </div>
      </Card>

      <Card title={`告警 (${alerts.length})`} icon={<AlertTriangle className="w-4 h-4 text-amber-500" />}>
        <div className="p-2 space-y-1.5">
          {alerts.length === 0 && <div className="text-xs text-slate-400 py-2 text-center">暂无告警</div>}
          {alerts.map(a => (
            <div key={a.alert_id} className="flex items-start gap-2 p-2 rounded bg-slate-50 hover:bg-slate-100 cursor-pointer"
              onClick={() => acknowledgeAlert(a.alert_id)}>
              <StatusDot status={a.severity} />
              <div className="flex-1 min-w-0">
                <div className="text-xs text-slate-700 truncate">{a.message}</div>
                <div className="text-[10px] text-slate-400">{a.source}</div>
              </div>
            </div>
          ))}
        </div>
      </Card>

      <Card title="最近报告" icon={<FileText className="w-4 h-4 text-blue-500" />}>
        <div className="p-2 space-y-1.5">
          {reports.map(r => (
            <div key={r.report_id} className="p-2 rounded bg-slate-50">
              <div className="flex items-center gap-1.5 mb-0.5">
                <StatusDot status={r.severity === 'info' ? 'completed' : r.severity} />
                <span className="text-xs font-medium text-slate-700">{r.title}</span>
              </div>
              <div className="text-[11px] text-slate-500">{r.content}</div>
            </div>
          ))}
        </div>
      </Card>
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