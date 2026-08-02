import { Activity, CalendarRange, Cpu, Database, ShieldCheck } from 'lucide-react'
import { Card } from '../components/Layout'
import { useAppStore } from '../stores/appStore'

export function FacilityChatPanel() {
  const state = useAppStore(s => s.state)
  const llm = useAppStore(s => s.llmStatus)
  if (!state) return <p className="empty-copy">等待系统状态</p>
  const proposed = state.disturbances.filter(item => item.status === 'proposed').length
  const applied = state.disturbances.filter(item => item.status === 'applied').length
  return <div className="panel-stack">
    <Card title="GLM 接入状态" icon={<Cpu className="icon-sm industrial" />}>
      <div className={`llm-status-card ${llm?.configured ? 'live' : 'fallback'}`}><i /><div><strong>{llm?.configured ? '智谱 API 已接入' : '等待 GLM API Key'}</strong><span>{llm?.configured ? `${llm.model} · ${llm.thinking === 'enabled' ? '深度思考' : '快速模式'}` : '页面仍可用规则解析故障和查询'}</span></div></div>
      {llm?.last_error && <p className="inline-warning">最近一次调用已降级：{llm.last_error}</p>}
    </Card>
    <Card title="数据时间范围" icon={<CalendarRange className="icon-sm industrial" />}><div className="data-list"><Row label="起始" value={state.data_timeline.start ?? '—'} /><Row label="结束" value={state.data_timeline.end ?? '—'} /><Row label="当前源日" value={state.data_timeline.current_source_date ?? '—'} /><Row label="处理粒度" value={`${state.data_timeline.resolution_minutes} min`} /></div></Card>
    <Card title="事件账本" icon={<Activity className="icon-sm industrial" />}><div className="data-list"><Row label="等待确认" value={String(proposed)} /><Row label="已应用" value={String(applied)} /><Row label="事件总数" value={String(state.disturbances.length)} /></div></Card>
    <Card title="交互边界" icon={<ShieldCheck className="icon-sm industrial" />}><ul className="boundary-list"><li>GLM 负责理解语言和选择工具</li><li>能源算法负责数值、优化与物理约束</li><li>运行变化必须经过事件卡确认</li><li>重算后重新进入三道审批门</li></ul></Card>
    <Card title="当前数据链" icon={<Database className="icon-sm industrial" />}><p className="small-copy">负荷处理 Agent 当前只做质量校验与粗粒度 → 15 分钟转换，不把随机噪声包装成预测。</p></Card>
  </div>
}

function Row({ label, value }: { label: string; value: string }) { return <div className="data-row"><span>{label}</span><strong>{value}</strong></div> }
