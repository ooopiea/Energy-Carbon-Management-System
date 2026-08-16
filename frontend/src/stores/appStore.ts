/** Zustand 全局状态管理：WebSocket 实时同步 + API 操作。 */
import { create } from 'zustand'
import type { ActionRecord, ChatMessage, ContextSelection, ControlActionPayload, ControlActionRecord, LlmStatus, RuntimeState, GraphTopology } from '../types'

const API_BASE = import.meta.env.DEV ? 'http://127.0.0.1:8000' : ''
const ACTION_STORAGE_KEY = 'huanghua-energy-action-records'

function loadActionRecords(): ActionRecord[] {
  try {
    const value = window.localStorage.getItem(ACTION_STORAGE_KEY)
    return value ? (JSON.parse(value) as ActionRecord[]).slice(0, 30) : []
  } catch { return [] }
}

interface AppStore {
  state: RuntimeState | null
  graph: GraphTopology | null
  activePage: string
  rightPanelCollapsed: boolean
  selectedNodeId: string | null
  contextSelection: ContextSelection | null
  selectedReportId: string | null
  reportReturnPage: string
  wsConnected: boolean
  connectionState: 'connecting' | 'connected' | 'reconnecting' | 'offline'
  loadState: 'idle' | 'loading' | 'ready' | 'error'
  errorMessage: string | null
  actionRecords: ActionRecord[]
  controlActions: ControlActionRecord[]
  operationMessage: { kind: 'success' | 'error'; text: string } | null
  chatMessages: ChatMessage[]
  chatLoading: boolean
  chatRole: 'engineer' | 'facility'
  llmStatus: LlmStatus | null

  setActivePage: (page: string) => void
  toggleRightPanel: () => void
  setSelectedNode: (id: string | null) => void
  setContextSelection: (selection: ContextSelection | null) => void
  setSelectedReport: (id: string | null) => void
  openReport: (id: string) => void
  closeReport: () => void
  setState: (s: RuntimeState) => void
  setGraph: (g: GraphTopology) => void
  setWsConnected: (v: boolean) => void
  setConnectionState: (v: AppStore['connectionState']) => void
  recordAction: (action: string, target: string, result: ActionRecord['result'], detail?: string) => void
  clearOperationMessage: () => void
  setChatRole: (role: 'engineer' | 'facility') => void

  fetchState: () => Promise<void>
  fetchGraph: () => Promise<void>
  pause: () => Promise<void>
  resume: () => Promise<void>
  reset: () => Promise<void>
  approve: (gateId: string, decision: string, comment?: string) => Promise<void>
  acknowledgeAlert: (alertId: string) => Promise<void>
  submitControlAction: (payload: ControlActionPayload) => Promise<ControlActionRecord>
  fetchControlActions: () => Promise<void>
  fetchChatHistory: () => Promise<void>
  fetchLlmStatus: () => Promise<void>
 sendChatMessage: (message: string) => Promise<void>
 decideDisturbance: (eventId: string, decision: 'apply' | 'cancel') => Promise<void>
  confirmFacilityAction: (proposalId: string) => Promise<void>
  cancelFacilityAction: (proposalId: string) => Promise<void>
  reviseFacilityAction: (proposalId: string, parameters: Record<string, unknown>) => Promise<void>
  resumeMission: (missionId: string, humanInput: string) => Promise<void>
}

export const useAppStore = create<AppStore>((set, get) => ({
  state: null,
  graph: null,
  activePage: 'overview',
  rightPanelCollapsed: typeof window !== 'undefined' && window.innerWidth < 1200,
  selectedNodeId: null,
  contextSelection: null,
  selectedReportId: null,
  reportReturnPage: 'agent_flow',
  wsConnected: false,
  connectionState: 'connecting',
  loadState: 'idle',
  errorMessage: null,
  actionRecords: loadActionRecords(),
  controlActions: [],
  operationMessage: null,
  chatMessages: [],
  chatLoading: false,
  chatRole: 'engineer',
  llmStatus: null,

  setActivePage: (page) => set({ activePage: page, contextSelection: null, selectedReportId: null, rightPanelCollapsed: typeof window !== 'undefined' && window.innerWidth < 1200 ? true : get().rightPanelCollapsed }),
  toggleRightPanel: () => set((s) => ({ rightPanelCollapsed: !s.rightPanelCollapsed })),
  setSelectedNode: (id) => set({ selectedNodeId: id }),
  setContextSelection: (selection) => set({ contextSelection: selection, rightPanelCollapsed: selection && typeof window !== 'undefined' && window.innerWidth < 1200 ? false : get().rightPanelCollapsed }),
  setSelectedReport: (id) => set({ selectedReportId: id }),
  openReport: (id) => set((state) => ({
    selectedReportId: id,
    reportReturnPage: state.activePage === 'report' ? state.reportReturnPage : state.activePage,
    activePage: 'report',
    rightPanelCollapsed: true,
  })),
  closeReport: () => set((state) => ({
    activePage: state.reportReturnPage,
    selectedReportId: null,
    // Reports use the full workspace and temporarily hide the action drawer.
    // Returning must reveal the approval context again, especially on narrow screens.
    rightPanelCollapsed: false,
  })),
  setState: (s) => set({ state: s }),
  setGraph: (g) => set({ graph: g }),
  setWsConnected: (v) => set({ wsConnected: v }),
  setConnectionState: (v) => set({ connectionState: v, wsConnected: v === 'connected' }),
  recordAction: (action, target, result, detail) => set((s) => {
    const actionRecords = [{ id: crypto.randomUUID(), at: new Date().toISOString(), action, target, result, detail }, ...s.actionRecords].slice(0, 30)
    try { window.localStorage.setItem(ACTION_STORAGE_KEY, JSON.stringify(actionRecords)) } catch { /* 浏览器禁用存储时仍保留本次会话 */ }
    return { actionRecords, operationMessage: { kind: result === 'success' ? 'success' : 'error', text: `${action}：${result === 'success' ? '已完成' : '失败'}${detail ? ` · ${detail}` : ''}` } }
  }),
  clearOperationMessage: () => set({ operationMessage: null }),
  setChatRole: (role) => {
    set({ chatRole: role })
    get().fetchChatHistory()
  },

  fetchState: async () => {
    if (get().loadState === 'idle') set({ loadState: 'loading', errorMessage: null })
    try {
      const res = await fetch(`${API_BASE}/api/state`)
      if (!res.ok) throw new Error(`状态接口返回 ${res.status}`)
      const data = await res.json()
      set({ state: data, loadState: 'ready', errorMessage: null })
    } catch (e) {
      console.error('fetchState error', e)
      if (!get().state) set({ loadState: 'error', errorMessage: e instanceof Error ? e.message : '无法获取系统状态' })
    }
  },

  fetchGraph: async () => {
    try {
      const res = await fetch(`${API_BASE}/api/graph`)
      if (!res.ok) throw new Error(`拓扑接口返回 ${res.status}`)
      const data = await res.json()
      set({ graph: data })
    } catch (e) {
      console.error('fetchGraph error', e)
    }
  },

  pause: async () => {
    try {
      const res = await fetch(`${API_BASE}/api/time/pause`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      get().recordAction('暂停仿真', '200× 时间引擎', 'success')
      await get().fetchState()
    } catch (e) { get().recordAction('暂停仿真', '200× 时间引擎', 'failed', e instanceof Error ? e.message : '请求失败') }
  },
  resume: async () => {
    try {
      const res = await fetch(`${API_BASE}/api/time/resume`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      get().recordAction('继续仿真', '200× 时间引擎', 'success')
      await get().fetchState()
    } catch (e) { get().recordAction('继续仿真', '200× 时间引擎', 'failed', e instanceof Error ? e.message : '请求失败') }
  },
  reset: async () => {
    try {
      const res = await fetch(`${API_BASE}/api/time/reset`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      get().recordAction('重置仿真', '200× 时间引擎', 'success')
      await get().fetchState()
    } catch (e) { get().recordAction('重置仿真', '200× 时间引擎', 'failed', e instanceof Error ? e.message : '请求失败') }
  },
  approve: async (gateId, decision, comment = '') => {
    try {
      const params = new URLSearchParams({ decision, comment })
      const res = await fetch(`${API_BASE}/api/approval/${gateId}?${params}`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      get().recordAction(decision === 'approve' ? '批准报告' : '退回修改', gateId, 'success', comment || undefined)
      await get().fetchState()
    } catch (e) {
      get().recordAction(decision === 'approve' ? '批准报告' : '退回修改', gateId, 'failed', e instanceof Error ? e.message : '请求失败')
      throw e
    }
  },
  acknowledgeAlert: async (alertId) => {
    try {
      const res = await fetch(`${API_BASE}/api/alerts/${alertId}/acknowledge`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      get().recordAction('确认告警', alertId, 'success')
      await get().fetchState()
    } catch (e) {
      get().recordAction('确认告警', alertId, 'failed', e instanceof Error ? e.message : '请求失败')
      throw e
    }
  },
  fetchControlActions: async () => {
    try {
      const res = await fetch(`${API_BASE}/api/control-actions?limit=30`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      set({ controlActions: await res.json() })
    } catch (e) {
      console.error('fetchControlActions error', e)
    }
  },
  submitControlAction: async (payload) => {
    try {
      const res = await fetch(`${API_BASE}/api/control-actions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) {
        const detail = typeof body.detail === 'string' ? body.detail : `控制接口返回 ${res.status}`
        throw new Error(res.status === 409 ? `审批尚未满足：${detail}` : detail)
      }
      const record = body as ControlActionRecord
      set((s) => ({
        controlActions: [record, ...s.controlActions.filter(item => item.action_id !== record.action_id)].slice(0, 30),
        operationMessage: { kind: 'success', text: `${payload.action}：${record.status === 'executed' ? '已执行' : '已受理，将在下一仿真步执行'}` },
      }))
      await get().fetchControlActions()
      return record
    } catch (e) {
      const message = e instanceof Error ? e.message : '控制请求失败'
      get().recordAction(payload.action, payload.target, 'failed', message)
      throw e
    }
  },

  fetchChatHistory: async () => {
    try {
      const role = get().chatRole
      const res = await fetch(`${API_BASE}/api/chat/history?session_id=huanghua-main&actor_role=${role}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const body = await res.json()
      set({ chatMessages: body.messages ?? [], llmStatus: body.llm ?? null })
    } catch (e) {
      console.error('fetchChatHistory error', e)
    }
  },

  fetchLlmStatus: async () => {
    try {
      const res = await fetch(`${API_BASE}/api/llm/status`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      set({ llmStatus: await res.json() })
    } catch (e) {
      console.error('fetchLlmStatus error', e)
    }
  },

  sendChatMessage: async (message) => {
    const text = message.trim()
    if (!text || get().chatLoading) return
    set({ chatLoading: true })
    try {
      const role = get().chatRole
      const res = await fetch(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          actor: role === 'facility' ? '厂务值班员' : '值班工程师',
          actor_role: role,
          session_id: 'huanghua-main',
        }),
      })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`)
      await get().fetchChatHistory()
      await get().fetchState()
    } catch (e) {
      const detail = e instanceof Error ? e.message : '请求失败'
      get().recordAction('自然语言交互', 'GLM 厂务助手', 'failed', detail)
      throw e
    } finally {
      set({ chatLoading: false })
    }
  },

  decideDisturbance: async (eventId, decision) => {
    try {
      const res = await fetch(`${API_BASE}/api/disturbances/${eventId}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, actor: get().chatRole === 'facility' ? '厂务值班员' : '值班工程师' }),
      })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`)
      if (body.state) set({ state: body.state })
      get().recordAction(decision === 'apply' ? '确认扰动并重算' : '取消扰动', eventId, 'success')
    } catch (e) {
      const detail = e instanceof Error ? e.message : '请求失败'
      get().recordAction(decision === 'apply' ? '确认扰动并重算' : '取消扰动', eventId, 'failed', detail)
      throw e
    }
  },
 confirmFacilityAction: async (proposalId) => {
   try {
     const res = await fetch(`${API_BASE}/api/facility-actions/${proposalId}/confirm`, {
       method: 'POST',
       headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ actor: get().chatRole === 'facility' ? '厂务值班员' : '值班工程师' }),
     })
     const body = await res.json().catch(() => ({}))
     if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`)
      if (body.state) set({ state: body.state }); else await get().fetchState()
     get().recordAction('confirm facility action', proposalId, 'success')
   } catch (e) {
     get().recordAction('confirm facility action', proposalId, 'failed', e instanceof Error ? e.message : 'failed')
     throw e
   }
 },
 cancelFacilityAction: async (proposalId) => {
   try {
     const res = await fetch(`${API_BASE}/api/facility-actions/${proposalId}/cancel`, {
       method: 'POST',
       headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ actor: get().chatRole === 'facility' ? '厂务值班员' : '值班工程师' }),
     })
     const body = await res.json().catch(() => ({}))
     if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`)
      if (body.state) set({ state: body.state }); else await get().fetchState()
     get().recordAction('cancel facility action', proposalId, 'success')
   } catch (e) {
    get().recordAction('cancel facility action', proposalId, 'failed', e instanceof Error ? e.message : 'failed')
    throw e
  }
},
 reviseFacilityAction: async (proposalId, parameters) => {
   try {
     const res = await fetch(`${API_BASE}/api/facility-actions/${proposalId}/revise`, {
       method: 'POST',
       headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ parameters, actor: get().chatRole === 'facility' ? '厂务值班员' : '值班工程师' }),
     })
     const body = await res.json().catch(() => ({}))
     if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`)
      if (body.state) set({ state: body.state }); else await get().fetchState()
      // revise creates a new proposal_id; attach it to the latest
      // assistant message so its action card renders in the chat.
      const newId = body.action?.proposal_id
      if (newId) {
        set((s) => {
          const msgs = [...s.chatMessages]
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].role === 'assistant') {
              const ids = [...new Set([...(msgs[i].facility_action_ids ?? []), newId])]
              msgs[i] = { ...msgs[i], facility_action_ids: ids }
              break
            }
          }
          return { chatMessages: msgs }
        })
      }
     get().recordAction('revise facility action', proposalId, 'success')
   } catch (e) {
     get().recordAction('revise facility action', proposalId, 'failed', e instanceof Error ? e.message : 'failed')
     throw e
   }
 },
  resumeMission: async (missionId, humanInput) => {
    try {
      const res = await fetch(`${API_BASE}/api/mission/${missionId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ human_input: humanInput }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await get().fetchState()
    } catch (e) { console.error('resumeMission error', e) }
  },
}))

let ws: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let allowReconnect = false
let reconnectAttempts = 0

export function connectWebSocket() {
  allowReconnect = true
  const wsUrl = import.meta.env.DEV
    ? 'ws://127.0.0.1:8000/ws'
    : `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`

  const connect = () => {
    if (!allowReconnect) return
    useAppStore.getState().setConnectionState(reconnectAttempts ? 'reconnecting' : 'connecting')
    ws = new WebSocket(wsUrl)
    const store = useAppStore

    ws.onopen = () => {
      store.getState().setWsConnected(true)
      store.getState().setConnectionState('connected')
      reconnectAttempts = 0
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.type === 'state_update' && msg.data) {
          store.getState().setState(msg.data)
        }
      } catch (e) {
        console.error('WS parse error', e)
      }
    }

    ws.onclose = () => {
      store.getState().setWsConnected(false)
      if (!allowReconnect) return
      reconnectAttempts += 1
      store.getState().setConnectionState(reconnectAttempts >= 5 ? 'offline' : 'reconnecting')
      reconnectTimer = setTimeout(connect, Math.min(15000, 1000 * 2 ** Math.min(reconnectAttempts, 4)))
    }

    ws.onerror = () => {
      ws?.close()
    }
  }

  connect()
}

export function disconnectWebSocket() {
  allowReconnect = false
  if (reconnectTimer) clearTimeout(reconnectTimer)
  if (ws) {
    ws.onclose = null
    ws.onerror = null
    ws.close()
  }
  ws = null
}
