import { useEffect } from 'react'
import { useAppStore, connectWebSocket, disconnectWebSocket } from './stores/appStore'
import { Layout } from './components/Layout'
import { Overview } from './pages/Overview'
import { AgentFlow } from './pages/AgentFlow'
import { StoragePage } from './pages/StoragePage'
import { HVACPage } from './pages/HVACPage'
import { FacilityChat } from './pages/FacilityChat'
import { ReportPage } from './pages/ReportPage'
import { SystemPendingPage } from './pages/SystemPendingPage'

export default function App() {
  const { activePage, fetchState, fetchGraph, fetchControlActions, fetchChatHistory, fetchLlmStatus } = useAppStore()

  useEffect(() => {
    fetchState()
    fetchGraph()
    fetchControlActions()
    fetchChatHistory()
    fetchLlmStatus()
    connectWebSocket()
    const interval = setInterval(() => { fetchState(); fetchControlActions() }, 5000)
    return () => {
      clearInterval(interval)
      disconnectWebSocket()
    }
  }, [])

  const renderPage = () => {
    switch (activePage) {
      case 'agent_flow': return <AgentFlow />
      case 'overview': return <Overview />
      case 'storage': return <StoragePage />
      case 'hvac': return <HVACPage />
      case 'facility_chat': return <FacilityChat />
      case 'report': return <ReportPage />
      case 'fixed_load': return <SystemPendingPage title="不可调负荷" />
      case 'compressor': return <SystemPendingPage title="空压机" />
      case 'ice_storage': return <SystemPendingPage title="蓄冷" />
      default: return <Overview />
    }
  }

  return <Layout>{renderPage()}</Layout>
}
