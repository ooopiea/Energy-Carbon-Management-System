import { useEffect } from 'react'
import { useAppStore, connectWebSocket, disconnectWebSocket } from './stores/appStore'
import { Layout } from './components/Layout'
import { Overview } from './pages/Overview'
import { AgentFlow } from './pages/AgentFlow'
import { StoragePage } from './pages/StoragePage'
import { HVACPage } from './pages/HVACPage'

export default function App() {
  const { activePage, fetchState, fetchGraph } = useAppStore()

  useEffect(() => {
    fetchState()
    fetchGraph()
    connectWebSocket()
    const interval = setInterval(() => fetchState(), 5000)
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
      default: return <Overview />
    }
  }

  return <Layout>{renderPage()}</Layout>
}