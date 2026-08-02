import { useEffect, useMemo, useRef, useState } from 'react'
import { Bot, Building2, Check, Clock3, Cpu, MessageSquareText, RotateCw, Send, ShieldCheck, Sparkles, UserRound, X } from 'lucide-react'
import { useAppStore } from '../stores/appStore'
import type { DisturbanceEvent } from '../types'

const PROMPTS = [
  '汇总当前系统形势和需要我关注的风险',
  '今天 10:30 3号冷机故障，预计 2 小时恢复',
  '今天 14:00 到 16:00 园区负荷增加 5 MW',
  '解释当前储能和 HVAC 为什么还没有执行',
]

function timeLabel(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function EventCard({ event }: { event: DisturbanceEvent }) {
  const decide = useAppStore(s => s.decideDisturbance)
  const [busy, setBusy] = useState(false)
  const apply = async (decision: 'apply' | 'cancel') => {
    setBusy(true)
    try { await decide(event.event_id, decision) } finally { setBusy(false) }
  }
  const status = event.status === 'proposed' ? '等待确认' : event.status === 'applied' ? '已应用并重算' : event.status === 'cancelled' ? '已取消' : '应用失败'
  return <article className={`disturbance-card ${event.status}`}>
    <header><div><span className="event-kicker">EVENT DRAFT · {event.event_type}</span><strong>{event.target}</strong></div><span className="event-status">{status}</span></header>
    <p>{event.summary}</p>
    <dl>
      <div><dt>影响开始</dt><dd>{timeLabel(event.start_time)}</dd></div>
      <div><dt>影响结束</dt><dd>{event.end_time ? timeLabel(event.end_time) : '待确认'}</dd></div>
      <div><dt>解析来源</dt><dd>{event.parsed_by}</dd></div>
      <div><dt>置信度</dt><dd>{Math.round(event.confidence * 100)}%</dd></div>
    </dl>
    {Object.keys(event.parameters).length > 0 && <div className="event-parameters">{Object.entries(event.parameters).map(([key, value]) => <span key={key}>{key}<b>{String(value)}</b></span>)}</div>}
    {event.impact_summary && <div className="event-impact"><RotateCw />{event.impact_summary}</div>}
    {event.status === 'proposed' && <footer><button disabled={busy} className="btn" onClick={() => apply('cancel')}><X />取消草案</button><button disabled={busy} className="btn primary" onClick={() => apply('apply')}><Check />{busy ? '正在重算…' : '确认并重算'}</button></footer>}
  </article>
}

export function FacilityChat() {
  const { state, chatMessages, chatLoading, chatRole, setChatRole, sendChatMessage, llmStatus } = useAppStore()
  const [draft, setDraft] = useState('')
  const streamRef = useRef<HTMLDivElement>(null)
  const events = state?.disturbances ?? []
  const eventMap = useMemo(() => new Map(events.map(event => [event.event_id, event])), [events])
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
  const submitKey = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send() }
  }

  return <div className="facility-page">
    <header className="facility-hero">
      <div><span className="eyebrow">GLM OPERATIONS DESK · LANGGRAPH TOOL ROUTING</span><h1>厂务协同</h1><p>把现场语言转成可审计的状态、事件与重算任务。所有改变先确认，再进入确定性调度链。</p></div>
      <div className="role-switch" role="group" aria-label="交互身份">
        <button className={chatRole === 'engineer' ? 'active' : ''} onClick={() => setChatRole('engineer')}><UserRound />工程师</button>
        <button className={chatRole === 'facility' ? 'active' : ''} onClick={() => setChatRole('facility')}><Building2 />厂务</button>
      </div>
    </header>

    <section className="situation-strip" aria-label="当前系统简报">
      <div><span>仿真时刻</span><strong>{state ? timeLabel(state.time.sim_time) : '—'}</strong></div>
      <div><span>当前负荷</span><strong>{state ? `${(state.load_kw / 1000).toFixed(1)} MW` : '—'}</strong></div>
      <div><span>工作流</span><strong>{state?.workflow.status ?? '—'}</strong></div>
      <div><span>扰动</span><strong>{events.filter(event => event.status === 'applied').length} 已应用 / {events.filter(event => event.status === 'proposed').length} 待确认</strong></div>
      <div className={llmStatus?.configured ? 'llm-live' : 'llm-fallback'}><span>语言引擎</span><strong><Cpu />{llmStatus?.configured ? llmStatus.model : '规则降级'}</strong></div>
    </section>

    <section className="conversation-console">
      <div className="conversation-head"><div><MessageSquareText /><strong>运行对话带</strong><span>每条变化都绑定仿真时间和事件编号</span></div><span className="safety-mark"><ShieldCheck />人工确认联锁已启用</span></div>
      <div className="conversation-stream" aria-live="polite" ref={streamRef}>
        {chatMessages.length === 0 && <div className="chat-welcome"><div className="welcome-orbit"><Sparkles /><i /><i /><i /></div><h2>从一句现场情况开始</h2><p>可以询问形势，也可以直接描述设备故障、负荷变化、天气或电价扰动。</p><div className="prompt-grid">{PROMPTS.map(prompt => <button key={prompt} onClick={() => setDraft(prompt)}>{prompt}</button>)}</div></div>}
        {chatMessages.map(message => <div className={`message-row ${message.role}`} key={message.message_id}>
          <div className="message-marker">{message.role === 'assistant' ? <Bot /> : message.actor_role === 'facility' ? <Building2 /> : <UserRound />}</div>
          <div className="message-body"><header><strong>{message.actor}</strong><span>{timeLabel(message.created_at)}</span>{message.mode === 'glm' && <em>GLM</em>}{message.mode === 'rule_fallback' && <em className="fallback">降级</em>}</header><p>{message.content}</p>
            {message.event_ids.map(id => eventMap.get(id)).filter((event): event is DisturbanceEvent => Boolean(event)).map(event => <EventCard event={event} key={event.event_id} />)}
          </div>
        </div>)}
        {chatLoading && <div className="message-row assistant pending"><div className="message-marker"><Bot /></div><div className="thinking-line"><i /><i /><i /><span>GLM 正在读取状态并选择工具</span></div></div>}
      </div>

      <div className="chat-composer">
        <div className="composer-context"><span><Clock3 />相对时间按仿真时钟解析</span><span>{chatRole === 'facility' ? '厂务视角' : '工程师视角'}</span></div>
        <textarea value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={submitKey} rows={3} maxLength={8000} placeholder={chatRole === 'facility' ? '例如：今天 14:00 二期 3号机组故障，预计两小时恢复…' : '用自然语言输入工况、疑问或随机干扰…'} />
        <div className="composer-footer"><span>Enter 发送 · Shift + Enter 换行</span><button onClick={() => void send()} disabled={!draft.trim() || chatLoading}><Send />发送给 GLM</button></div>
      </div>
    </section>
  </div>
}
