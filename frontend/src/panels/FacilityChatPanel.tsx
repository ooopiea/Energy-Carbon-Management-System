import { Activity, CalendarRange, Cpu, Database, Gauge, ShieldCheck } from 'lucide-react'
import { Card } from '../components/Layout'
import { useAppStore } from '../stores/appStore'

export function FacilityChatPanel() {
  const state = useAppStore(s => s.state)
  const llm = useAppStore(s => s.llmStatus)
  if (!state) return <p className="empty-copy">loading</p>

  const proposed = state.disturbances.filter(i => i.status === 'proposed').length
  const applied = state.disturbances.filter(i => i.status === 'applied').length
  const faProposed = (state.facility_actions ?? []).filter(i => i.status === 'proposed').length
  const faApplied = (state.facility_actions ?? []).filter(i => i.status === 'applied').length
  const monitored = (state.facility_actions ?? []).filter(i => i.status === 'applied' && i.monitor_steps_remaining > 0)

  return <div className="panel-stack">
    <Card title="GLM 接入" icon={<Cpu className="icon-sm industrial" />}>
      <div className={`llm-status-card ${llm?.configured ? 'live' : 'fallback'}`}>
        <i />
        <div>
          <strong>{llm?.configured ? 'API 已接入' : '等待 GLM Key'}</strong>
          <span>{llm?.configured ? `${llm.model} · ${llm.thinking === 'enabled' ? '深度思考' : '快速'}` : '规则降级模式'}</span>
        </div>
      </div>
      {llm?.last_error && <p className="inline-warning">降级: {llm.last_error}</p>}
    </Card>

    <Card title="调度阶段" icon={<Gauge className="icon-sm industrial" />}>
      <div className={`phase-badge ${state.current_phase ?? 'day_ahead'}`}>
        {state.current_phase === 'realtime' ? '实时运行中' : '日前调度中'}
      </div>
    </Card>

    <Card title="数据时间范围" icon={<CalendarRange className="icon-sm industrial" />}>
      <div className="data-list">
        <Row label="起始" value={state.data_timeline.start ?? '—'} />
        <Row label="结束" value={state.data_timeline.end ?? '—'} />
        <Row label="当前源日" value={state.data_timeline.current_source_date ?? '—'} />
        <Row label="粒度" value={`${state.data_timeline.resolution_minutes} min`} />
      </div>
    </Card>

    <Card title="事件账本" icon={<Activity className="icon-sm industrial" />}>
      <div className="data-list">
        <Row label="等待确认" value={String(proposed)} />
        <Row label="已应用" value={String(applied)} />
        <Row label="总数" value={String(state.disturbances.length)} />
      </div>
    </Card>

    <Card title="厂务动作" icon={<Activity className="icon-sm industrial" />}>
      <div className="data-list">
        <Row label="待确认" value={String(faProposed)} />
        <Row label="已执行" value={String(faApplied)} />
        <Row label="监控中" value={String(monitored.length)} />
      </div>
      {monitored.length > 0 && <div className="monitoring-list">
        {monitored.map(a => {
          const pe = a.post_execution
          const dev = pe?.deviation as Record<string, number> | undefined
          return <div key={a.proposal_id} className="monitor-item">
            <span className="monitor-type">{a.action_type} · {a.target_system}</span>
            <span className="monitor-remaining">剩 {a.monitor_steps_remaining} 步</span>
            {dev?.cost_deviation_ratio !== undefined && dev.cost_deviation_ratio > 0.1 && <span className="monitor-warn">偏差 {(dev.cost_deviation_ratio * 100).toFixed(0)}%</span>}
          </div>
        })}
      </div>}
    </Card>

    <Card title="交互边界" icon={<ShieldCheck className="icon-sm industrial" />}>
      <ul className="boundary-list">
        <li>工程师只读，不能执行动作</li>
        <li>厂务动作需经过预览和确认</li>
        <li>确认后进入审批链和安全联锁</li>
        <li>后置监控 24 步，偏差 {'>'}10% 告警</li>
      </ul>
    </Card>
  </div>
}

function Row({ label, value }: { label: string; value: string }) {
  return <div className="data-row"><span>{label}</span><strong>{value}</strong></div>
}
