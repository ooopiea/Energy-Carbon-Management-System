import { FileText } from 'lucide-react'
import { useAppStore } from '../stores/appStore'

export function ApprovalReportButton({ gateId }: { gateId: 'forecast_approval' | 'storage_approval' | 'hvac_approval' }) {
  const gate = useAppStore(state => state.state?.approval_gates[gateId])
  const openReport = useAppStore(state => state.openReport)
  if (!gate?.report_id) return null
  return <button className="approval-report-link" onClick={() => openReport(gate.report_id!)}><span>{gate.report?.title || gate.name}</span><FileText /></button>
}
