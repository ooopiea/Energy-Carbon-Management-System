import { type ReactNode, useEffect, useState } from 'react'
import { useAppStore } from '../stores/appStore'
import {
  Workflow, LayoutDashboard, BatteryCharging, Battery, Wind, Pause, Play, RotateCcw,
  Wifi, WifiOff, PanelRightClose, PanelRightOpen, Zap, CheckCircle2, XCircle,
  MessageSquareText, Lock, SlidersHorizontal, Fan, Snowflake,
  type LucideIcon, ChevronDown,
} from 'lucide-react'
import { OverviewPanel } from '../panels/OverviewPanel'
import { AgentFlowPanel } from '../panels/AgentFlowPanel'
import { StoragePanel } from '../panels/StoragePanel'
import { HVACPanel } from '../panels/HVACPanel'
import { FacilityChatPanel } from '../panels/FacilityChatPanel'


type NavLeaf = {
  kind: 'leaf'
  id: string
  label: string
  icon: LucideIcon
}

type NavGroup = {
  kind: 'group'
  id: string
  label: string
  icon: LucideIcon
  children: NavLeaf[]
}

type NavItem = NavLeaf | NavGroup

const NAV_ITEMS: NavItem[] = [
  { kind: 'leaf', id: 'overview', label: '综合可视化', icon: LayoutDashboard },
  { kind: 'leaf', id: 'facility_chat', label: 'AI厂务协同', icon: MessageSquareText },
  { kind: 'leaf', id: 'agent_flow', label: 'AI协作流程', icon: Workflow },
  { kind: 'leaf', id: 'fixed_load', label: '不可调负荷', icon: Lock },
  { kind: 'group', id: 'adjustable', label: '可调负荷', icon: SlidersHorizontal, children: [
    { kind: 'leaf', id: 'hvac', label: '暖通空调', icon: Wind },
    { kind: 'leaf', id: 'compressor', label: '空压机', icon: Fan },
  ]},
  { kind: 'group', id: 'storage_group', label: '储能设备', icon: BatteryCharging, children: [
    { kind: 'leaf', id: 'storage', label: '电池储能', icon: Battery },
    { kind: 'leaf', id: 'ice_storage', label: '蓄冷', icon: Snowflake },
  ]},
]

export function statusColor(status: string): string {
  switch (status) {
    case 'completed': case 'approved': return '#15803d'
    case 'running': return '#176b87'
    case 'pending_approval': case 'warning': return '#d97706'
    case 'failed': case 'rejected': case 'critical': return '#dc2626'
    default: return '#94a3b8'
  }
}

export function statusLabel(status: string): string {
  const map: Record<string, string> = {
    idle: '未开始', running: '运行中', completed: '已完成', pending_approval: '等待审批',
    pending: '待审批', approved: '已批准', rejected: '已退回', failed: '异常', warning: '告警', critical: '严重',
  }
  return map[status] || status
}

export function StatusDot({ status }: { status: string }) {
  return <span className="status-dot" style={{ backgroundColor: statusColor(status) }} aria-label={statusLabel(status)} />
}

export function Card({ children, className = '', title, icon }: { children: ReactNode; className?: string; title?: string; icon?: ReactNode }) {
  return (
    <section className={`surface-card ${className}`}>
      {title && <header className="card-header">{icon}<h3>{title}</h3></header>}
      {children}
    </section>
  )
}

export function KpiCard({ label, value, unit, color = '#176b87', icon }: { label: string; value: string | number; unit?: string; color?: string; icon?: ReactNode }) {
  return (
    <article className="kpi-card">
      <div className="kpi-label"><span>{label}</span>{icon}</div>
      <div className="kpi-value" style={{ color }}>{value}{unit && <span>{unit}</span>}</div>
    </article>
  )
}

function formatTime(timeStr: string): string {
  const d = new Date(timeStr)
  return Number.isNaN(d.getTime()) ? timeStr : d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function DispatchRail() {
  const state = useAppStore(s => s.state)
  const step = Math.max(0, Math.min(95, state?.time.step ?? 0))
  const prices = state?.day_ahead.price ?? []
  const periods = state?.day_ahead.tariff_periods ?? []
  const max = Math.max(...prices, 1)
  return (
    <div className="dispatch-rail" aria-label={`96点调度轨，当前第 ${step + 1} 点`}>
      <div className="rail-meta">
        <div><span className="eyebrow">黄花园区 · 数据起点 {state?.data_timeline?.start ?? '读取中'} · 200×</span><strong>{state ? formatTime(state.time.sim_time) : '等待系统时间'}</strong></div>
        <div className="rail-step"><span>当前步</span><strong>{String(step + 1).padStart(2, '0')}</strong><small>/ 96</small></div>
      </div>
      <div className="rail-track" role="img" aria-label="全天分时电价与当前仿真位置">
        {Array.from({ length: 96 }, (_, i) => {
          const price = prices[i] ?? 0
          const level = periods[i] ?? 'flat'
          return <span key={i} className={`${level} ${i === step ? 'current' : ''}`} title={`${String(Math.floor(i / 4)).padStart(2, '0')}:${String(i % 4 * 15).padStart(2, '0')} · ${price.toFixed(3)} 元/kWh`} />
        })}
      </div>
      <div className="rail-legend"><span><i className="valley" />谷</span><span><i className="flat" />平</span><span><i className="peak" />峰</span><span><i className="sharp" />尖</span></div>
    </div>
  )
}

export function ActionHistory({ limit = 5 }: { limit?: number }) {
  const localRecords = useAppStore(s => s.actionRecords).filter(r => r.result === 'failed' || !['应用园区策略', '撤销园区策略', '下发储能指令', '下发 HVAC 设定'].includes(r.action))
  const serverRecords = useAppStore(s => s.controlActions)
  const records = [
    ...serverRecords.map(r => ({ id: r.action_id, at: r.submitted_at, action: r.action, target: r.target, result: r.status === 'rejected' ? 'failed' as const : 'success' as const, detail: `${r.value} ${r.unit} · ${r.status === 'executed' ? `已执行${r.applied_step === null ? '' : `（步 ${r.applied_step}）`}` : r.status === 'accepted' ? '已受理，等待下一步执行' : '已拒绝'}${r.reason ? ` · ${r.reason}` : ''}` })),
    ...localRecords,
  ].sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime()).slice(0, limit)
  return (
    <Card title="操作记录" icon={<Workflow className="icon-sm industrial" />}>
      <div className="history-list">
        {records.length === 0 && <p className="empty-copy">尚无人工操作</p>}
        {records.map(r => <div className="history-item" key={r.id}>
          {r.result === 'success' ? <CheckCircle2 className="icon-sm ok" /> : <XCircle className="icon-sm danger" />}
          <div><strong>{r.action}</strong><span>{r.target} · {new Date(r.at).toLocaleTimeString('zh-CN')}</span>{r.detail && <small>{r.detail}</small>}</div>
        </div>)}
      </div>
    </Card>
  )
}

export function Layout({ children }: { children: ReactNode }) {
  const { activePage, setActivePage, rightPanelCollapsed, toggleRightPanel, state, connectionState, pause, resume, reset, loadState, errorMessage, fetchState, operationMessage, clearOperationMessage } = useAppStore()
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set())
  const toggleGroup = (gid: string) => setCollapsedGroups(prev => {
    const next = new Set(prev)
    if (next.has(gid)) next.delete(gid); else next.add(gid)
    return next
  })
  const isPaused = state?.time.paused ?? false
  useEffect(() => {
    if (!operationMessage) return
    const timer = window.setTimeout(clearOperationMessage, 4000)
    return () => window.clearTimeout(timer)
  }, [operationMessage, clearOperationMessage])
  useEffect(() => {
    if (window.matchMedia('(max-width: 1199px)').matches) useAppStore.setState({ rightPanelCollapsed: true })
  }, [])

  const renderRightPanel = () => {
    switch (activePage) {
      case 'agent_flow': return <AgentFlowPanel />
      case 'storage': return <StoragePanel />
      case 'hvac': return <HVACPanel />
      case 'facility_chat': return <FacilityChatPanel />
      default: return <OverviewPanel />
    }
  }

  const resetWithConfirm = () => {
    if (window.confirm('确认将 200× 仿真时间重置到起点？该操作会改变当前运行进度。')) reset()
  }

  return (
    <div className={`app-shell ${rightPanelCollapsed ? 'panel-is-collapsed' : ''}`}>
      <aside className="sidebar" aria-label="主导航">
        <div className="brand"><div className="brand-mark"><Zap /></div><div className="brand-copy"><strong>黄花园区</strong><span>调度控制台</span></div></div>
        <nav>{NAV_ITEMS.map(item => {
          if (item.kind === 'leaf') {
            const Icon = item.icon; const active = activePage === item.id
            return <button key={item.id} onClick={() => setActivePage(item.id)} className={active ? 'active' : ''} aria-current={active ? 'page' : undefined} title={item.label}>
              <Icon /><span>{item.label}</span>
            </button>
          }
          const GIcon = item.icon; const isCollapsed = collapsedGroups.has(item.id)
          return <div key={item.id} className="nav-group">
            <button className={`nav-group-header ${isCollapsed ? '' : 'open'}`} onClick={() => toggleGroup(item.id)} title={item.label}>
              <GIcon /><span>{item.label}</span><ChevronDown className="nav-chevron" />
            </button>
            {!isCollapsed && item.children.map(child => {
              const Icon = child.icon; const active = activePage === child.id
              return <button key={child.id} onClick={() => setActivePage(child.id)} className={`nav-child ${active ? 'active' : ''}`} aria-current={active ? 'page' : undefined} title={child.label}>
                <Icon /><span>{child.label}</span>
              </button>
            })}
          </div>
        })}</nav>
        <div className="time-controls">
          <span className="connection"><i className={connectionState} />{connectionState === 'connected' ? <Wifi /> : <WifiOff />}<b>{connectionState === 'connected' ? '实时连接' : connectionState === 'reconnecting' ? '正在重连' : connectionState === 'connecting' ? '正在连接' : '连接中断'}</b></span>
          <div className="control-row">
            <button onClick={() => isPaused ? resume() : pause()} aria-label={isPaused ? '继续仿真' : '暂停仿真'}>{isPaused ? <Play /> : <Pause />}<span>{isPaused ? '继续' : '暂停'}</span></button>
            <button onClick={resetWithConfirm} aria-label="重置仿真"><RotateCcw /></button>
          </div>
        </div>
      </aside>

      <main className="main-workspace">
        <DispatchRail />
        {connectionState !== 'connected' && state && <div className="state-banner warning" role="status">实时连接暂不可用，正在展示最近一次成功数据。系统会自动重连。</div>}
        {loadState === 'loading' && <div className="page-state" role="status"><span className="spinner" />正在接入园区实时数据…</div>}
        {loadState === 'error' && !state && <div className="page-state error" role="alert"><strong>无法加载能源系统</strong><span>{errorMessage}</span><button onClick={fetchState}>重新连接</button></div>}
        {(state || loadState === 'idle') && <div className="page-content">{children}</div>}
      </main>

      {!rightPanelCollapsed && <button className="drawer-backdrop" aria-label="关闭详情面板" onClick={toggleRightPanel} />}
      <aside className={`context-panel ${rightPanelCollapsed ? 'collapsed' : ''}`} aria-label="上下文详情面板" aria-hidden={rightPanelCollapsed} {...(rightPanelCollapsed ? { inert: '' } : {})}>
        <div className="panel-top"><strong>上下文与操作</strong><button onClick={toggleRightPanel} aria-label="收起详情面板"><PanelRightClose /></button></div>
        <div className="panel-scroll">{renderRightPanel()}</div>
      </aside>
      <button className={`panel-toggle ${rightPanelCollapsed ? '' : 'open'}`} onClick={toggleRightPanel} aria-label={rightPanelCollapsed ? '展开详情面板' : '收起详情面板'} aria-expanded={!rightPanelCollapsed}>
        {rightPanelCollapsed ? <PanelRightOpen /> : <PanelRightClose />}
      </button>
      {operationMessage && <div className={`toast ${operationMessage.kind}`} role="status">{operationMessage.text}</div>}
    </div>
  )
}
