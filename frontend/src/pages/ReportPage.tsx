import { useMemo, useState } from 'react'
import { ArrowLeft, CheckCircle2, FileCheck2, ShieldAlert, Table2, XCircle } from 'lucide-react'
import { useAppStore } from '../stores/appStore'
import { statusLabel } from '../components/Layout'
import ReactECharts from 'echarts-for-react'

type ReportDetail = {
  kind: 'forecast' | 'storage' | 'hvac'
  situation_summary: string
  schedule_table: Array<Record<string, unknown>>
  key_metrics: Array<{ label: string; value: string | number; unit: string }>
  plan_table: Array<Record<string, unknown>>
  risks: unknown[]
  data_provenance?: Record<string, unknown>
}

const LABELS: Record<string, string> = {
  step: '序号', time: '时刻', shift: '班次', production_intensity: '生产强度',
  load_kw: '负荷(kW)', price_cny_per_kwh: '电价(元/kWh)', forecast_kw: '负荷指标(kW)',
  power_kw: '功率(kW)', soc_ratio: 'SOC', temperature_c: '温度(°C)', grid_kw: '电网功率(kW)',
  active_chillers: '冷机(台)', cop: 'COP', supply_temp_c: '供水(°C)', return_temp_c: '回水(°C)',
}

function ForecastChart({ rows }: { rows: Array<Record<string, unknown>> }) {
  const times = rows.map(r => String(r.time))
  const values = rows.map(r => Number(r.forecast_kw))
  return <ReactECharts option={{
    tooltip: { trigger: 'axis' },
    grid: { left: 58, right: 18, top: 30, bottom: 30 },
    xAxis: { type: 'category', data: times, axisLabel: { fontSize: 9, interval: 11 } },
    yAxis: { type: 'value', name: 'kW', axisLabel: { fontSize: 9 } },
    series: [{ name: '负荷预测', type: 'line', smooth: true, symbol: 'none', data: values, lineStyle: { width: 2, color: '#176b87' }, areaStyle: { color: 'rgba(23,107,135,.08)' } }],
  }} style={{ height: 240 }} />
}

function StorageChart({ rows }: { rows: Array<Record<string, unknown>> }) {
  const times = rows.map(r => String(r.time))
  const powerValues = rows.map(r => Number(r.power_kw))
  const socValues = rows.map(r => Number((Number(r.soc_ratio) * 100).toFixed(1)))
  return <ReactECharts option={{
    tooltip: { trigger: 'axis' },
    legend: { data: ['储能功率', 'SOC'], top: 4, textStyle: { fontSize: 10 } },
    grid: { left: 58, right: 58, top: 36, bottom: 30 },
    xAxis: { type: 'category', data: times, axisLabel: { fontSize: 9, interval: 11 } },
    yAxis: [
      { type: 'value', name: 'kW', axisLabel: { fontSize: 9 } },
      { type: 'value', name: '%', min: 0, max: 100, axisLabel: { fontSize: 9 } },
    ],
    series: [
      { name: '储能功率', type: 'line', smooth: true, symbol: 'none', yAxisIndex: 0, data: powerValues, lineStyle: { width: 2, color: '#d97706' } },
      { name: 'SOC', type: 'line', smooth: true, symbol: 'none', yAxisIndex: 1, data: socValues, lineStyle: { width: 2, color: '#15803d' }, areaStyle: { color: 'rgba(21,128,61,.06)' } },
    ],
  }} style={{ height: 240 }} />
}

function HvacChart({ rows }: { rows: Array<Record<string, unknown>> }) {
  const times = rows.map(r => String(r.time))
  const powerValues = rows.map(r => Number(r.power_kw))
  const copValues = rows.map(r => Number(r.cop))
  const chillerValues = rows.map(r => Number(r.active_chillers))
  return <ReactECharts option={{
    tooltip: { trigger: 'axis' },
    legend: { data: ['HVAC功率', 'COP', '冷机台数'], top: 4, textStyle: { fontSize: 10 } },
    grid: { left: 58, right: 58, top: 36, bottom: 30 },
    xAxis: { type: 'category', data: times, axisLabel: { fontSize: 9, interval: 11 } },
    yAxis: [
      { type: 'value', name: 'kW', axisLabel: { fontSize: 9 } },
      { type: 'value', name: 'COP', axisLabel: { fontSize: 9 } },
    ],
    series: [
      { name: 'HVAC功率', type: 'line', smooth: true, symbol: 'none', yAxisIndex: 0, data: powerValues, lineStyle: { width: 2, color: '#1688a7' } },
      { name: 'COP', type: 'line', smooth: true, symbol: 'none', yAxisIndex: 1, data: copValues, lineStyle: { width: 2, color: '#6d5b8c' } },
      { name: '冷机台数', type: 'bar', yAxisIndex: 0, data: chillerValues, itemStyle: { color: 'rgba(22,136,167,.2)' } },
    ],
  }} style={{ height: 240 }} />
}

export function ReportPage() {
  const { state, selectedReportId, closeReport, approve } = useAppStore()
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const report = state?.reports.find(item => item.report_id === selectedReportId)
    ?? Object.values(state?.approval_gates ?? {}).map(gate => gate.report).find(item => item?.report_id === selectedReportId)
  const gateEntry = useMemo(() => Object.entries(state?.approval_gates ?? {}).find(([, gate]) => gate.report_id === selectedReportId), [state, selectedReportId])
  const detail = report?.data?.report_detail as ReportDetail | undefined

  if (!state || !report) return <div className="report-page"><button className="report-back" onClick={closeReport}><ArrowLeft />返回</button><div className="report-empty">报告不存在或已切换到新一轮调度。</div></div>

  const decide = async (decision: 'approve' | 'reject' | 'revise') => {
    if (!gateEntry || (decision !== 'approve' && !comment.trim())) return
    setBusy(true)
    try { await approve(gateEntry[0], decision, comment.trim()) } finally { setBusy(false) }
  }

  return <article className="report-page">
    <button className="report-back" onClick={closeReport}><ArrowLeft />返回工作台</button>
    <header className="report-hero">
      <div><span className="report-kicker"><FileCheck2 />审批报告 · {report.report_id}</span><h1>{report.title}</h1><p>{detail?.situation_summary || report.content}</p></div>
      <dl><div><dt>报告状态</dt><dd>{statusLabel(report.status)}</dd></div><div><dt>生成时间</dt><dd>{new Date(report.created_at).toLocaleString('zh-CN')}</dd></div><div><dt>内容校验</dt><dd title={report.content_hash}>SHA-256 · {report.content_hash?.slice(0, 10)}</dd></div></dl>
    </header>

    {detail ? <>
      <section className="report-metrics">{detail.key_metrics.map(metric => <div key={metric.label}><span>{metric.label}</span><strong>{formatValue(metric.value, metric.unit)}</strong></div>)}</section>
      <ReportTable title="今日排班与负荷背景" icon={<Table2 />} rows={detail.schedule_table} columns={['time', 'shift', 'production_intensity', 'load_kw', 'price_cny_per_kwh']} />
      {detail.kind === 'forecast' && <section className="report-chart"><ForecastChart rows={detail.plan_table} /></section>}
      {detail.kind === 'storage'  && <section className="report-chart"><StorageChart rows={detail.plan_table} /></section>}
      {detail.kind === 'hvac'     && <section className="report-chart"><HvacChart rows={detail.plan_table} />{detail.kind === 'hvac' && <p className="chart-note">该图表基于日前调度计划生成，未来将接入现有调度系统以实现实时更新与闭环反馈。</p>}</section>}
      <ReportTable title="调度方案明细" icon={<FileCheck2 />} rows={detail.plan_table} columns={detail.kind === 'forecast' ? ['time', 'shift', 'production_intensity', 'forecast_kw'] : detail.kind === 'storage' ? ['time', 'power_kw', 'soc_ratio', 'temperature_c', 'grid_kw'] : ['time', 'power_kw', 'active_chillers', 'cop', 'supply_temp_c', 'return_temp_c']} />
      <section className="report-risk"><h2><ShieldAlert />边界、风险与假设</h2>{detail.risks.length ? <ul>{detail.risks.map((risk, index) => <li key={index}>{typeof risk === 'string' ? risk : JSON.stringify(risk)}</li>)}</ul> : <p>未发现计划约束越限。</p>}</section>
    </> : <section className="report-risk"><h2>情况概述</h2><p>{report.content}</p><pre>{JSON.stringify(report.data, null, 2)}</pre></section>}

    {gateEntry && <section className="report-approval">
      <div><span>审批门</span><strong>{gateEntry[1].name}</strong><small>{gateEntry[1].description}</small></div>
      {gateEntry[1].status === 'pending_approval' ? <div className="report-decision"><textarea value={comment} onChange={event => setComment(event.target.value)} placeholder="审批意见；退回或要求修订时必须填写" /><div><button disabled={busy} onClick={() => void decide('approve')}><CheckCircle2 />批准并继续</button><button disabled={busy || !comment.trim()} onClick={() => void decide('revise')}><FileCheck2 />要求修订</button><button disabled={busy || !comment.trim()} className="danger" onClick={() => void decide('reject')}><XCircle />拒绝</button></div></div> : <p>审批结果：{gateEntry[1].decision || statusLabel(gateEntry[1].status)}{gateEntry[1].comment ? ` · ${gateEntry[1].comment}` : ''}</p>}
    </section>}
  </article>
}

function ReportTable({ title, icon, rows, columns }: { title: string; icon: React.ReactNode; rows: Array<Record<string, unknown>>; columns: string[] }) {
  return <section className="report-table-section"><h2>{icon}{title}<span>{rows.length} 条</span></h2><div className="report-table-wrap"><table><thead><tr>{columns.map(column => <th key={column}>{LABELS[column] || column}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{columns.map(column => <td key={column}>{formatCell(column, row[column])}</td>)}</tr>)}</tbody></table></div></section>
}

function formatValue(value: string | number, unit: string) {
  if (unit === 'ratio' && typeof value === 'number') return `${(value * 100).toFixed(1)}%`
  return `${typeof value === 'number' ? value.toLocaleString('zh-CN') : value}${unit ? ` ${unit}` : ''}`
}

function formatCell(column: string, value: unknown) {
  if (value === null || value === undefined) return '—'
  if (column === 'soc_ratio' && typeof value === 'number') return `${(value * 100).toFixed(1)}%`
  if (typeof value === 'number') return Number.isInteger(value) ? value.toLocaleString('zh-CN') : value.toLocaleString('zh-CN', { maximumFractionDigits: 3 })
  return String(value)
}
