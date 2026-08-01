/** 全局类型定义，与后端 RuntimeState 对齐。 */

export interface TimeInfo {
  sim_time: string
  step: number
  day: number
  hour: number
  progress: number
  paused: boolean
  time_scale: number
}

export interface AgentNode {
  agent_type: string
  node_id: string
  name: string
  status: string
  started_at: string | null
  completed_at: string | null
  duration_ms: number | null
  last_result_summary: string
  message: string
}

export interface ApprovalGate {
  gate_id: string
  name: string
  description: string
  status: string
  report: any | null
  decision: string | null
  comment: string
  decided_at: string | null
}

export interface Report {
  report_id: string
  agent_type: string
  title: string
  content: string
  data: any
  created_at: string
  status: string
  severity: string
}

export interface Alert {
  alert_id: string
  severity: string
  source: string
  message: string
  timestamp: string
  acknowledged: boolean
}

export interface SeriesPoint {
  time: string
  step: number
  value: number
}

export interface DayAheadData {
  load_forecast: number[]
  storage_plan: number[]
  soc_plan: number[]
  hvac_plan: number[]
  price: number[]
  carbon_c: number[]
  carbon_cr: number[]
}

export interface StorageSummary {
  power_kw: number[]
  soc_ratio: number[]
  temp_c: number[]
  grid_kw: number[]
  saving_cny: number
  peak_reduction_kw: number
  terminal_soc: number
  max_temp_c: number
  solver_status: string
}

export interface HVACSummary {
  power_kw: number[]
  cop: number[]
  supply_temp_c: number[]
  return_temp_c: number[]
  active_chillers: number[]
  saving_cny: number
  avg_cop: number
}

export interface ChillerStation {
  name: string
  total_rated_kw: number
  chiller_count: number
  per_unit_kw: number
}

export interface GraphNode {
  id: string
  label: string
  type: string
  agent: string | null
  x: number
  y: number
  tools?: string[]
  description?: string
}

export interface GraphEdge {
  from: string
  to: string
  type: string
  label?: string
  style?: string
}

export interface GraphTopology {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface RuntimeState {
  time: TimeInfo
  load_kw: number
  solar_kw: number
  grid_kw: number
  storage_power_kw: number
  storage_soc: number
  storage_temp_c: number
  hvac_power_kw: number
  hvac_supply_temp_c: number
  carbon_factor: number
  price: number
  tariff_period: string
  daily: {
    energy_kwh: number
    cost_cny: number
    carbon_kg: number
    peak_kw: number
  }
  agent_nodes: Record<string, AgentNode>
  approval_gates: Record<string, ApprovalGate>
  reports: Report[]
  alerts: Alert[]
  series: Record<string, SeriesPoint[]>
  day_ahead: DayAheadData
  storage_summary: StorageSummary | null
  hvac_summary: HVACSummary | null
  tariff_summary: any
  carbon_dispatch: any
  chiller_topology: ChillerStation[]
  weather: {
    temp_c: number
    humidity: number
    wind: number
  }
}
