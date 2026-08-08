import { useEffect, useMemo, useRef, useState } from 'react'
import { Activity, Bot, Building2, Check, Clock3, Cpu, MessageSquareText, RotateCw, Send, ShieldCheck, Sparkles, TrendingDown, TrendingUp, UserRound, X, Zap } from 'lucide-react'
import { useAppStore } from '../stores/appStore'
import type { DisturbanceEvent, FacilityAction } from '../types'

const ENGINEER_PROMPTS = [
  '解释当前储能调度策略',
  '系统架构是怎样的',
  '当前审批进度',
  '安全边界有哪些',
]

const FACILITY_PROMPTS = [
  '把储能目标改成省钱优先',
  '现在储能放电 2000kW',
  '帮我优化储能和空调协调',
  '设置需量上限 50000 kW',
]

function timeLabel(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function MetricDiff({ before, after, label, unit, invert }: { before: number; after: number; label: string; unit: string; invert?: boolean }) {
  const diff = after - before
  const improved = invert ? diff > 0 : diff < 0
  const sign = diff > 0 ? '+' : ''
  return <div className={`metric-diff ${diff === 0 ? 'same' : improved ? 'better' : 'worse'}`}>
    <span className="metric-label">{label}</span>
    <span className="metric-before">{before.toLocaleString(undefined, { maximumFractionDigits: 1 })}{unit}</span>
    <span className="metric-arrow">{improved ? <TrendingDown /> : diff > 0 ? <TrendingUp /> : '—'}</span>
    <span className="metric-after">{after.toLocaleString(undefined, { maximumFractionDigits: 1 })}{unit}</span>
    <span className="metric-change">{sign}{diff.toLocaleString(undefined, { maximumFractionDigits: 1 })}</span>
  </div>
}

function ActionPreviewCard({ preview }: { preview: Record<string, unknown> }) {
  const before = preview.before as Record<string, number> | undefined
  const after = preview.after as Record<string, number> | undefined
  const validation = preview.validation as Array<Record<string, unknown>> | undefined
  if (!before && !after && !validation) return null
  return <div className="action-preview">
    {(before && after) && Object.keys(before).filter(k => typeof before[k] === 'number' && typeof after?.[k] === 'number').map(k => {
      const isInverted = k.includes('saving') || k.includes('reduction')
      return <MetricDiff key={k} before={before[k]} after={after[k]} label={k.replace(/_/g, ' ')} unit="" invert={isInverted} />
    })}
    {validation && <div className="validation-list">{validation.map((v, i) => (
      <span key={i} className={`validation-item ${v.status}`}>{String(v.field)}: {String(v.value)} {v.status === 'ok' ? '✓' : '⚠ 超限 ' + v.limit}</span>
    ))}</div>}
  </div>
}

function FacilityActionCard({ action }: { action: FacilityAction }) {
  const confirm = useAppStore(s => s.confirmFacilityAction)
  const cancel = useAppStore(s => s.cancelFacilityAction)
  const [busy, setBusy] = useState(false)
  const [decision, setDecision] = useState<'none' | 'confirm' | 'cancel'>('none')
  const handleConfirm = async () => { setBusy(true); setDecision('confirm'); try { await confirm(action.proposal_id) } finally { setBusy(false) } }
  const handleCancel = async () => { setBusy(true); setDecision('cancel'); try { await cancel(action.proposal_id) } finally { setBusy(false) } }

  const typeLabel: Record<string, string> = {
    day_ahead_modification: '日前参数修改', realtime_override: '实时覆盖',
    demand_cap: '需量上限', mission: 'Mission 协同',
  }
  const statusLabel: Record<string, string> = {
    proposed: '待确认', applied: '已执行', cancelled: '已取消', failed: '执行失败', confirmed: '已确认',
  }

  return <article className={`facility-action-card ${action.status}`}>
    <header>
      <div><span className="action-kicker">{typeLabel[action.action_type] ?? action.action_type} · {action.target_system}</span></div>
      <span className="action-status">{statusLabel[action.status] ?? action.status}</span>
    </header>
    <p className="action-reasoning">{action.reasoning}</p>
    <ActionPreviewCard preview={action.impact_preview} />
    {action.status === 'proposed' && <footer>
      <button disabled={busy} className="btn" onClick={() => void handleCancel()}><X />取消</button>
      <button disabled={busy} className="btn primary" onClick={() => void handleConfirm()}><Check />{busy && decision === 'confirm' ? '正在执行…' : '确认执行'}</button>
    </footer>}
    {action.post_execution && <div className="post-exec">后置监控: 步骤 {String(action.post_execution.step ?? '?')}, 剩余 {action.monitor_steps_remaining} 步</div>}
  </article>
}

function EventCard({ event }: { event: DisturbanceEvent }) {
  const decide = useAppStore(s => s.decideDisturbance)
  const [busy, setBusy] = useState(false)
  const apply = async (d: 'apply' | 'cancel') => { setBusy(true); try { await decide(event.event_id, d) } finally { setBusy(false) } }
  const status = event.status === 'proposed' ? '等待确认' : event.status === 'applied' ? '已应用并重算' : event.status === 'cancelled' ? '已取消' : '应用失败'
  return <article className={`disturbance-card ${event.status}`}>
    <header><div><span className="event-kicker">EVENT DRAFT · {event.event_type}</span><strong>{event.target}</strong></div><span className="event-status">{status}</span></header>
    <p>{event.summary}</p>
    {event.status === 'proposed' && <footer><button disabled={busy} className="btn" onClick={() => void apply('cancel')}><X />取消草案</button><button disabled={busy} className="btn primary" onClick={() => void apply('apply')}><Check />{busy ? '正在重算…' : '确认并重算'}</button></footer>}
  </article>
}

export function FacilityChat() {
  const { state, chatMessages, chatLoading, chatRole, setChatRole, sendChatMessage, llmStatus } = useAppStore()
  const [draft, setDraft] = useState('')
  const streamRef = useRef<HTMLDivElement>(null)
  const events = state?.disturbances ?? []
  const facilityActions = state?.facility_actions ?? []
  const eventMap = useMemo(() => new Map(events.map(e => [e.event_id, e])), [events])
  const actionMap = useMemo(() => new Map(facilityActions.map(a => [a.proposal_id, a])), [facilityActions])
  const phase = state?.current_phase ?? 'day_ahead'
  const prompts = chatRole === 'facility' ? FACILITY_PROMPTS : ENGINEER_PROMPTS
  const pendingActions = facilityActions.filter(a => a.status === 'proposed').length
  const appliedActions = facilityActions.filter(a => a.status === 'applied').length

  useEffect(() => {
    const stream = streamRef.current
    if (stream) stream.scrollTo({ top: stream.scrollHeight, behavior: 'smooth' })
  }, [chatMessages.length, chatLoading])

  const send = async () => {
    const text = draft.trim()
    if (!text) return
    setDraft('')
    try { await sendChatMessage(text) } catch { setDraft(text) }
  }
  const submitKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() }
  }

  return <div className="facility-page">
    <header className="facility-hero">
      <div>
        <span className="eyebrow">GLM OPERATIONS DESK · ROLE-SEPARATED TOOL ROUTING</span>
        <h1>厂务协同</h1>
        <p>工程师只读问答，厂务可执行动作。所有动作先预览再确认，确保安全。</p>
      </div>
      <div className="role-switch" role="group" aria-label="交互身份">
        <button className={chatRole === 'engineer' ? 'active' : ''} onClick={() => setChatRole('engineer')}><UserRound />工程师</button>
        <button className={chatRole === 'facility' ? 'active' : ''} onClick={() => setChatRole('facility')}><Building2 />厂务</button>
      </div>
    </header>

    <section className="situation-strip" aria-label="当前系统简报">
      <div><span>仿真时刻</span><strong>{state ? timeLabel(state.time.sim_time) : '—'}</strong></div>
      <div><span>当前负荷</span><strong>{state ? `${(state.load_kw / 1000).toFixed(1)} MW` : '—'}</strong></div>
      <div className={phase === 'realtime' ? 'phase-realtime' : 'phase-day-ahead'}>
        <span>调度阶段</span>
        <strong>{phase === 'realtime' ? '实时运行中' : '日前调度中'}</strong>
      </div>
      <div><span>待确认动作</span><strong>{pendingActions} 项</strong></div>
      <div><span>执行中</span><strong>{appliedActions} 已应用</strong></div>
      <div className={llmStatus?.configured ? 'llm-live' : 'llm-fallback'}><span>语言引擎</span><strong><Cpu />{llmStatus?.configured ? llmStatus.model : '规则降级'}</strong></div>
    </section>

    <section className="conversation-console">
      <div className="conversation-head">
        <div><MessageSquareText /><strong>{chatRole === 'facility' ? '厂务对话' : '工程师对话'}</strong></div>
        <span className="safety-mark"><ShieldCheck />人工确认联锁已启用</span>
      </div>
      <div className="conversation-stream" aria-live="polite" ref={streamRef}>
        {chatMessages.length === 0 && <div className="chat-welcome">
          <div className="welcome-orbit"><Sparkles /><i /><i /><i /></div>
          <h2>{chatRole === 'facility' ? '描述你要执行的调度动作' : '询问系统状态和架构'}</h2>
          <div className="prompt-grid">{prompts.map(p => <button key={p} onClick={() => setDraft(p)}>{p}</button>)}</div>
        </div>}
        {chatMessages.map(message => <div className={`message-row ${message.role}`} key={message.message_id}>
          <div className="message-marker">{message.role === 'assistant' ? <Bot /> : message.actor_role === 'facility' ? <Building2 /> : <UserRound />}</div>
          <div className="message-body">
            <header><strong>{message.actor}</strong><span>{timeLabel(message.created_at)}</span>{message.mode === 'glm' && <em>GLM</em>}{message.mode === 'rule_fallback' && <em className="fallback">降级</em>}</header>
            <p>{message.content}</p>
            {(message.event_ids ?? []).map(id => eventMap.get(id)).filter((e): e is DisturbanceEvent => Boolean(e)).map(e => <EventCard event={e} key={e.event_id} />)}
            {(message.facility_action_ids ?? []).map(id => actionMap.get(id)).filter((a): a is FacilityAction => Boolean(a)).map(a => <FacilityActionCard action={a} key={a.proposal_id} />)}
          </div>
        </div>)}
        {chatLoading && <div className="message-row assistant pending"><div className="message-marker"><Bot /></div><div className="thinking-line"><i /><i /><i /><span>GLM 正在读取状态并选择工具</span></div></div>}
      </div>

      <div className="chat-composer">
        <div className="composer-context">
          <span><Clock3 />相对时间按仿真时钟解析</span>
          <span>{chatRole === 'facility' ? '厂务视角 · 可执行动作' : '工程师视角 · 只读'}</span>
        </div>
        <textarea value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={submitKey} rows={3} maxLength={8000}
          placeholder={chatRole === 'facility' ? '例如：把储能目标改成省钱优先，末端 SOC 保持 50%' : '用自然语言询问工况、架构或状态'} />
        <div className="composer-footer"><span>Enter 发送 · Shift + Enter 换行</span><button onClick={() => void send()} disabled={!draft.trim() || chatLoading}><Send />发送给 GLM</button></div>
      </div>
    </section>
  </div>
}
