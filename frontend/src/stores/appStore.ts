/** Zustand 全局状态管理：WebSocket 实时同步 + API 操作。 */
import { create } from 'zustand'
import type { RuntimeState, GraphTopology } from '../types'

const API_BASE = import.meta.env.DEV ? 'http://127.0.0.1:8000' : ''

interface AppStore {
  state: RuntimeState | null
  graph: GraphTopology | null
  activePage: string
  rightPanelCollapsed: boolean
  selectedNodeId: string | null
  wsConnected: boolean

  setActivePage: (page: string) => void
  toggleRightPanel: () => void
  setSelectedNode: (id: string | null) => void
  setState: (s: RuntimeState) => void
  setGraph: (g: GraphTopology) => void
  setWsConnected: (v: boolean) => void

  fetchState: () => Promise<void>
  fetchGraph: () => Promise<void>
  pause: () => Promise<void>
  resume: () => Promise<void>
  reset: () => Promise<void>
  approve: (gateId: string, decision: string, comment?: string) => Promise<void>
  acknowledgeAlert: (alertId: string) => Promise<void>
}

export const useAppStore = create<AppStore>((set, get) => ({
  state: null,
  graph: null,
  activePage: 'overview',
  rightPanelCollapsed: false,
  selectedNodeId: null,
  wsConnected: false,

  setActivePage: (page) => set({ activePage: page }),
  toggleRightPanel: () => set((s) => ({ rightPanelCollapsed: !s.rightPanelCollapsed })),
  setSelectedNode: (id) => set({ selectedNodeId: id }),
  setState: (s) => set({ state: s }),
  setGraph: (g) => set({ graph: g }),
  setWsConnected: (v) => set({ wsConnected: v }),

  fetchState: async () => {
    try {
      const res = await fetch(`${API_BASE}/api/state`)
      const data = await res.json()
      set({ state: data })
    } catch (e) {
      console.error('fetchState error', e)
    }
  },

  fetchGraph: async () => {
    try {
      const res = await fetch(`${API_BASE}/api/graph`)
      const data = await res.json()
      set({ graph: data })
    } catch (e) {
      console.error('fetchGraph error', e)
    }
  },

  pause: async () => {
    await fetch(`${API_BASE}/api/time/pause`, { method: 'POST' })
  },
  resume: async () => {
    await fetch(`${API_BASE}/api/time/resume`, { method: 'POST' })
  },
  reset: async () => {
    await fetch(`${API_BASE}/api/time/reset`, { method: 'POST' })
  },
  approve: async (gateId, decision, comment = '') => {
    await fetch(`${API_BASE}/api/approval/${gateId}?decision=${decision}&comment=${encodeURIComponent(comment)}`, { method: 'POST' })
  },
  acknowledgeAlert: async (alertId) => {
    await fetch(`${API_BASE}/api/alerts/${alertId}/acknowledge`, { method: 'POST' })
  },
}))

let ws: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

export function connectWebSocket() {
  const wsUrl = import.meta.env.DEV
    ? 'ws://127.0.0.1:8000/ws'
    : `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`

  const connect = () => {
    ws = new WebSocket(wsUrl)
    const store = useAppStore

    ws.onopen = () => {
      store.getState().setWsConnected(true)
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
      reconnectTimer = setTimeout(connect, 2000)
    }

    ws.onerror = () => {
      ws?.close()
    }
  }

  connect()
}

export function disconnectWebSocket() {
  if (reconnectTimer) clearTimeout(reconnectTimer)
  ws?.close()
  ws = null
}