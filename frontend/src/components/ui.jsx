import React from 'react'

export function Loading({ what = 'data' }) {
  return <div className="spinner">Loading {what}…</div>
}

export function ErrorBox({ error }) {
  if (!error) return null
  return <div className="error-box">{error.message || String(error)}</div>
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

// Maps backend state/status vocabulary onto a colour. Kept in one place so the
// same state never renders two different ways across pages.
const TONE = {
  CLOSED: 'ok', SUCCEEDED: 'ok', COMPLETED: 'ok', RESOLVED: 'ok',
  COMPATIBLE: 'ok', SELECTED: 'ok', ALLOWED: 'ok', CONFIRMED: 'ok',
  FAILED: 'err', REJECTED: 'err', INCOMPATIBLE: 'err', DENIED: 'err',
  TIMED_OUT: 'err', PERMANENT: 'err', OUT_OF_STOCK: 'err',
  RETRYING: 'warn', WAITING: 'warn', TIMEOUT: 'warn', ESCALATED: 'warn',
  COMPENSATING: 'warn', UNKNOWN: 'warn', TRANSIENT: 'warn', CURRENT: 'warn',
  NONE_ELIGIBLE: 'warn', NONE_AVAILABLE: 'warn',
  CANCELLED: 'muted', PENDING: 'muted', SKIPPED: 'muted',
}

const LABELS = {
  REPAIR_SCHEDULED: 'Repair scheduled', REPAIR_IN_PROGRESS: 'Repair in progress',
  PARTS_PENDING: 'Waiting for parts', PARTS_RESERVED: 'Parts reserved',
  WAITING: 'Waiting for provider', RETRYING: 'Retrying', TIMEOUT: 'Timed out',
  ESCALATED: 'Needs attention', CLOSED: 'Completed', SUCCEEDED: 'Succeeded',
  CANCELLED: 'Cancelled', REJECTED: 'Rejected', FAILED: 'Failed',
  DIAGNOSING: 'Diagnosing', REPAIRING: 'Repairing', COMPLETED: 'Completed',
  PENDING: 'Pending', CURRENT: 'In progress', CHECKED_IN: 'Checked in',
  SERVICE_CENTRE: 'Service centre', PARTS_SUPPLIER: 'Parts supplier',
  REPAIR_PERSON: 'Repair person', CUSTOMER: 'Customer', ADMIN: 'Administrator',
  LOW: 'Low', NORMAL: 'Normal', HIGH: 'High', URGENT: 'Urgent',
}

export function humanize(value) {
  if (!value) return '—'
  return LABELS[value] || String(value).toLowerCase().replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

export function tone(value) {
  return TONE[value] || 'info'
}

export function Badge({ value, children }) {
  return <span className={`badge ${tone(value)}`}>{children ?? humanize(value)}</span>
}

export function Progress({ percent }) {
  return (
    <div className="progress" title={`${percent}%`}>
      <div style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} />
    </div>
  )
}

export function Metric({ label, value, sub }) {
  return (
    <div className="metric">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  )
}

export function when(ts) {
  if (!ts) return '—'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString()
}

export function duration(ms) {
  if (ms === null || ms === undefined) return '—'
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`
}

export function Json({ data, label = 'raw payload' }) {
  if (!data || Object.keys(data).length === 0) return null
  return (
    <details>
      <summary>{label}</summary>
      <pre className="json">{JSON.stringify(data, null, 2)}</pre>
    </details>
  )
}

/** Workflow visualisation. Every node's status comes from the backend. */
export function Timeline({ nodes }) {
  if (!nodes || nodes.length === 0) return <Empty>No timeline yet.</Empty>
  return (
    <div className="timeline">
      {nodes.map((n) => (
        <div key={n.state} className={`tl-node ${n.status}`} title={n.note || ''}>
          <div className="tl-label">{n.label}</div>
          <div className="tl-meta">
            {n.provider_code && <div className="mono">{n.provider_code}</div>}
            {n.reached_at && <div>{new Date(n.reached_at).toLocaleTimeString()}</div>}
          </div>
        </div>
      ))}
    </div>
  )
}
