import { type ReactNode } from 'react'
import { useAppStore } from '../stores/appStore'
import {
  Workflow, LayoutDashboard, BatteryCharging, Wind,
  Pause, Play, RotateCcw, Wifi, WifiOff, ChevronRight, Zap, AlertTriangle, FileText, CheckCircle2, Clock, XCircle
} from 'lucide-react'
import { OverviewPanel } from '../panels/OverviewPanel'
import { AgentFlowPanel } from '../panels/AgentFlowPanel'
import { StoragePanel } from '../panels/StoragePanel'
import { HVACPanel } from '../panels/HVACPanel'

const NAV_ITEMS = [
  { id: 'overview', label: '综合可视化', icon: LayoutDashboard },
  { id: 'agent_flow', label: 'Agent 流程', icon: Workflow },
  { id: 'storage', label: '储能系统', icon: BatteryCharging },
  { id: 'hvac', label: 'HVAC 系统', icon: Wind },
]

export function statusColor(status: string): string {
  switch (status) {
    case 'completed': case 'approved': return '#22c55e'
    case 'running': return '#3b82f6'
    case 'pending_approval': case 'warning': return '#f59e0b'
    case 'failed': case 'rejected': case 'critical': return '#ef4444'
    default: return '#9ca3af'
  }
}

export function statusLabel(status: string): string {
  const map: Record<string, string> = {
    idle: '未开始', running: '运行中', completed: '已完成',
    pending_approval: '等待审批', approved: '已批准',
    rejected: '已退回', failed: '异常', warning: '告警',
  }
  return map[status] || status
}

export function StatusDot({ status }: { status: string }) {
  return (
    <span
      className="inline-block w-2.5 h-2.5 rounded-full flex-shrink-0"
      style={{ backgroundColor: statusColor(status) }}
    />
  )
}

export function Card({ children, className = '', title, icon }: { children: ReactNode; className?: string; title?: string; icon?: ReactNode }) {
  return (
    <div className={`bg-white rounded-lg border border-slate-200 shadow-sm ${className}`}>
      {title && (
        <div className="flex items-center gap-2 px-4 py-3 border-b border-slate-100">
          {icon}
          <h3 className="text-sm font-semibold text-slate-700">{title}</h3>
        </div>
      )}
      {children}
    </div>
  )
}

export function KpiCard({ label, value, unit, color = '#3b82f6', icon }: { label: string; value: string | number; unit?: string; color?: string; icon?: ReactNode }) {
  return (
    <div className="bg-white rounded-lg border border-slate-200 p-3 shadow-sm">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-slate-500">{label}</span>
        {icon}
      </div>
      <div className="flex items-baseline gap-1">
        <span className="text-xl font-bold" style={{ color }}>{value}</span>
        {unit && <span className="text-xs text-slate-400">{unit}</span>}
      </div>
    </div>
  )
}

function formatTime(timeStr: string): string {
  try {
    const d = new Date(timeStr)
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return timeStr
  }
}

export function Layout({ children }: { children: ReactNode }) {
  const {
    activePage, setActivePage, rightPanelCollapsed, toggleRightPanel,
    state, wsConnected, pause, resume, reset,
  } = useAppStore()

  const time = state?.time
  const isPaused = time?.paused ?? false

  const renderRightPanel = () => {
    switch (activePage) {
      case 'agent_flow': return <AgentFlowPanel />
      case 'storage': return <StoragePanel />
      case 'hvac': return <HVACPanel />
      default: return <OverviewPanel />
    }
  }

  return (
    <div className="flex h-screen bg-slate-50 overflow-hidden">
      {/* 左侧导航栏 */}
      <div className="w-52 bg-slate-800 flex flex-col flex-shrink-0">
        <div className="px-4 py-4 flex items-center gap-2 border-b border-slate-700">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500 to-cyan-400 flex items-center justify-center">
            <Zap className="w-5 h-5 text-white" />
          </div>
          <div>
            <div className="text-white text-sm font-bold leading-tight">能源管理</div>
            <div className="text-slate-400 text-[10px]">V3 Agent Platform</div>
          </div>
        </div>

        <nav className="flex-1 py-2">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon
            const active = activePage === item.id
            return (
              <button
                key={item.id}
                onClick={() => setActivePage(item.id)}
                className={`w-full flex items-center gap-3 px-4 py-2.5 text-sm transition-colors ${
                  active
                    ? 'bg-blue-600 text-white border-r-2 border-blue-400'
                    : 'text-slate-300 hover:bg-slate-700'
                }`}
              >
                <Icon className="w-4 h-4 flex-shrink-0" />
                <span>{item.label}</span>
              </button>
            )
          })}
        </nav>

        {/* 时间显示与控制 */}
        {time && (
          <div className="px-3 py-3 border-t border-slate-700 space-y-2">
            <div className="text-slate-400 text-[10px]">模拟时间 ({time.time_scale}x)</div>
            <div className="text-white text-sm font-mono">{formatTime(time.sim_time)}</div>
            <div className="text-slate-500 text-[10px]">第 {time.day + 1} 天 | 步 {time.step}/96 | {Math.round(time.progress * 100)}%</div>
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => isPaused ? resume() : pause()}
                className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded bg-slate-700 hover:bg-slate-600 text-slate-200 text-xs"
              >
                {isPaused ? <Play className="w-3 h-3" /> : <Pause className="w-3 h-3" />}
                {isPaused ? '继续' : '暂停'}
              </button>
              <button
                onClick={() => reset()}
                className="px-2 py-1.5 rounded bg-slate-700 hover:bg-slate-600 text-slate-200"
              >
                <RotateCcw className="w-3 h-3" />
              </button>
            </div>
            <div className="flex items-center gap-1 text-[10px]">
              {wsConnected ? (
                <><Wifi className="w-3 h-3 text-green-400" /><span className="text-green-400">已连接</span></>
              ) : (
                <><WifiOff className="w-3 h-3 text-red-400" /><span className="text-red-400">未连接</span></>
              )}
            </div>
          </div>
        )}
      </div>

      {/* 中央主工作区 */}
      <div className="flex-1 overflow-y-auto p-4">
        {children}
      </div>

      {/* 右侧上下文面板 */}
      {!rightPanelCollapsed && (
        <div className="w-80 bg-white border-l border-slate-200 overflow-y-auto flex-shrink-0">
          {renderRightPanel()}
        </div>
      )}
      <button
        onClick={toggleRightPanel}
        className="absolute right-0 top-1/2 -translate-y-1/2 z-10 bg-white border border-slate-200 rounded-l-md p-1 shadow-sm hover:bg-slate-50"
        style={{ right: rightPanelCollapsed ? 0 : 'auto', left: rightPanelCollapsed ? 'auto' : undefined }}
      >
        <ChevronRight className={`w-4 h-4 transition-transform ${rightPanelCollapsed ? '' : 'rotate-180'}`} />
      </button>
    </div>
  )
}