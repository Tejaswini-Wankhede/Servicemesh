import React, { useEffect, useState } from 'react'
import {
  Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { api } from '../lib/api.js'
import { ErrorBox, Loading, Metric, tone, when } from '../components/ui.jsx'

const COLOR = { ok: '#3fb950', warn: '#d29922', err: '#f85149',
                info: '#a371f7', muted: '#8b98a5' }

// Every figure here is computed by the backend from persisted rows. No value on
// this page is a constant.
export default function AdminDashboard() {
  const [m, setM] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    const load = () => api.metrics().then(setM).catch(setError)
    load()
    const t = setInterval(load, 8000)
    return () => clearInterval(t)
  }, [])

  if (error) return <ErrorBox error={error} />
  if (!m) return <Loading what="metrics" />

  const states = Object.entries(m.by_state)
    .map(([state, count]) => ({ state, count }))
    .sort((a, b) => b.count - a.count)

  return (
    <>
      <div className="page-head">
        <h1>Operations dashboard</h1>
        <p>Computed from persisted transactions · generated {when(m.generated_at)}</p>
      </div>

      <div className="grid cols-4">
        <Metric label="Transactions" value={m.total_transactions} />
        <Metric label="Completed" value={m.completed}
                sub={`${(m.completion_rate * 100).toFixed(1)}% completion rate`} />
        <Metric label="In progress" value={m.in_progress}
                sub={`${m.recovering} recovering`} />
        <Metric label="Escalated" value={m.escalated}
                sub={`${m.manual_interventions} manual interventions`} />
        <Metric label="Rejected" value={m.rejected} sub="terminal business rejections" />
        <Metric label="Failed" value={m.failed} />
        <Metric label="SLA breaches" value={m.sla_breaches} />
        <Metric label="Retries" value={m.total_retries}
                sub={`${m.total_failures} failures recorded`} />
        <Metric label="Recovery success" value={`${(m.recovery_success_rate * 100).toFixed(1)}%`}
                sub={`${m.total_recovery_actions} recovery actions`} />
        <Metric label="Duplicates prevented" value={m.duplicate_operations_prevented}
                sub="idempotent retries of mutating operations" />
        <Metric label="Avg resolution"
                value={m.avg_resolution_seconds === null ? '—'
                  : `${m.avg_resolution_seconds.toFixed(1)}s`}
                sub="created to closed" />
        <Metric label="Ops / transaction" value={m.avg_operations_per_transaction} />
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Transactions by state</h2>
        {states.length === 0 ? <p className="muted">No transactions yet.</p> : (
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={states}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2a3441" />
              <XAxis dataKey="state" stroke="#8b98a5" fontSize={11} angle={-25}
                     textAnchor="end" height={80} interval={0} />
              <YAxis stroke="#8b98a5" fontSize={11} allowDecimals={false} />
              <Tooltip contentStyle={{ background: '#171d26', border: '1px solid #2a3441',
                                       borderRadius: 8, color: '#e6edf3' }} />
              <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                {states.map((s) => (
                  <Cell key={s.state} fill={COLOR[tone(s.state)] || COLOR.info} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </>
  )
}
