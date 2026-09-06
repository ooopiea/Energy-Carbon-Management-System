/** 全局类型定义，与后端 RuntimeState 对齐。 */

export interface TimeInfo {
  sim_time: string
  step: number
  day: number
  hour: number
  progress: number
  paused: boolean
  time_scale: number
  cycle?: number
  loop_enabled?: boolean
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
  report_id: string | null
  report_hash: string | null
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
  content_hash: string
  run_id: string
}

export interface Alert {
  alert_id: string
  severity: string
  source: string
  message: string
  timestamp: string
  acknowledged: boolean
}

export type ContextKind = 'node' | 'device' | 'point' | 'alert' | 'report'

export interface ContextSelection {
  kind: ContextKind
  id: string
  label: string
  page: string
  detail?: Record<string, string | number | boolean | null>
}

export interface ActionRecord {
  id: string
  at: string
  action: string
  target: string
  result: 'success' | 'failed'
  detail?: string
}

export interface ControlActionPayload {
  system: 'overview' | 'storage' | 'hvac'
  action: string
  target: string
  value: number
  unit: string
  reason: string
  actor: string
}

export interface ControlActionRecord extends ControlActionPayload {
  action_id: string
  run_id: string
  submitted_at: string
  status: 'accepted' | 'executed' | 'rejected'
  applied_step: number | null
  command_id: string | null
}

export interface DisturbanceEvent {
  event_id: string
  run_id: string
  actor: string
  actor_role: 'engineer' | 'facility'
  source_text: string
  event_type: 'equipment_failure' | 'equipment_recovery' | 'load_adjustment' | 'weather_override' | 'price_override' | 'schedule_change' | 'operational_note'
  target: string
  start_time: string
  end_time: string | null
  parameters: Record<string, string | number | boolean>
  summary: string
  confidence: number
  parsed_by: string
  status: 'proposed' | 'applied' | 'cancelled' | 'failed'
  created_at: string
  decided_at: string | null
  decided_by: string | null
  impact_summary: string
}

export interface FacilityAction {
  proposal_id: string
  action_type: 'day_ahead_modification' | 'realtime_override' | 'demand_cap' | 'mission'
  target_system: 'overview' | 'storage' | 'hvac'
  parameters: Record<string, unknown>
  impact_preview: Record<string, unknown>
  reasoning: string
  confidence: number
  status: 'proposed' | 'confirmed' | 'applied' | 'cancelled' | 'failed'
  target_day?: number | null
  created_at: string
  decided_at: string | null
  decided_by: string | null
  post_execution: Record<string, unknown> | null
  monitor_steps_remaining: number
}

export interface PendingDayPlan {
  target_day: number
  target_date: string
  status: 'draft' | 'pending_approval' | 'approved' | 'executed'
  gate_status: Record<string, string>
  objective_mode: string
  daily_soc_override: Record<string, number> | null
  modifications: Array<Record<string, unknown>>
  metrics: {
    storage_saving_cny: number
    storage_terminal_soc: number
    hvac_saving_cny: number
    hvac_avg_cop: number
    peak_reduction_kw: number
  }
}

export interface ChatMessage {
  message_id: string
  role: 'user' | 'assistant'
  actor_role: 'engineer' | 'facility'
  actor: string
  content: string
  created_at: string
  event_ids: string[]
  facility_action_ids: string[]
  tool_trace: Array<{ name: string; summary?: string; result: unknown }>
  mode: 'user' | 'glm' | 'rule_fallback'
  reasoning?: string
}

export interface LlmStatus {
  provider: string
  configured: boolean
  model: string
  base_url: string
  thinking: string
  last_error: string | null
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
  tariff_periods: string[]
  carbon_c: number[]
  carbon_cr: number[]
}

export interface StorageSummary {
  power_kw: number[]
  soc_ratio: number[]
  temp_c: number[]
  grid_kw: number[]
  saving_cny: number
  baseline_carbon_kg?: number
  optimized_carbon_kg?: number
  carbon_reduction_kg?: number
  baseline_energy_cost_cny?: number
  optimized_energy_cost_cny?: number
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

export interface CheckpointView {
  available: boolean
  run_id?: string
  schema_version?: number
  subgraph?: string
  workflow_status?: string
  current_node?: string
  last_completed_tick?: number | null
  dispatch_enabled?: boolean
  last_command_id?: string | null
  last_ack_accepted?: boolean | null
  updated_at?: string
  approval_bindings?: Record<string, { status: string; report_id: string; report_hash: string }>
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
  hvac_return_temp_c: number
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
  workflow: { run_id: string; status: string; current_node: string }
  checkpoint: CheckpointView
  physical_dispatch: { enabled: boolean; last_execution: any; command_count: number }
  data_timeline: {
    start: string | null
    end: string | null
    current_source_date: string | null
    resolution_minutes: number
    provenance: Record<string, unknown>
  }
 disturbances: DisturbanceEvent[]
  current_phase: string
  facility_actions: FacilityAction[]
 active_strategy: { demand_cap_kw: number | null; enabled: boolean; objective_mode?: string; monthly_peak_kw?: number }
  pending_day_plan: PendingDayPlan | null
}
