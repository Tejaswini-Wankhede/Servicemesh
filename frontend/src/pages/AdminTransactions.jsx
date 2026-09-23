import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api.js'
import { Badge, Empty, ErrorBox, Loading, Progress, when } from '../components/ui.jsx'

const STATES = ['', 'CREATED', 'WARRANTY_VERIFIED', 'PROVIDER_SELECTED',
  'PART_CONFIRMED', 'REPAIR_SCHEDULED', 'WAITING', 'RETRYING',
  'ESCALATED', 'CLOSED', 'REJECTED', 'FAILED', 'CANCELLED']

export default function AdminTransactions() {
  const [rows, setRows] = useState(null)
  const [state, setState] = useState('')
  const [error, setError] = useState(null)

  useEffect(() => {
    setRows(null)
    api.listTransactions({ state, limit: 200 }).then(setRows).catch(setError)
  }, [state])

  return (
    <>
      <div className="page-head">
        <h1>All transactions</h1>
        <p>System-wide view across every customer and organization.</p>
      </div>

      <div className="card">
        <label style={{ maxWidth: 260 }}>
          <span>Filter by state</span>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            {STATES.map((s) => <option key={s} value={s}>{s || 'All states'}</option>)}
          </select>
        </label>

        <ErrorBox error={error} />
        {!rows ? <Loading what="transactions" />
          : rows.length === 0 ? <Empty>No transactions match.</Empty> : (
          <table>
            <thead><tr>
              <th>Reference</th><th>Customer</th><th>Issue</th><th>State</th>
              <th style={{ width: 150 }}>Progress</th><th>Retries</th>
              <th>SLA</th><th>Created</th>
            </tr></thead>
            <tbody>
              {rows.map((t) => (
                <tr key={t.id}>
                  <td className="mono"><Link to={`/transactions/${t.id}`}>{t.reference}</Link></td>
                  <td>{t.customer_name}</td>
                  <td>{t.issue_type}</td>
                  <td><Badge value={t.state} />
                    {t.requires_manual_intervention &&
                      <div><span className="badge warn" style={{ marginTop: 3 }}>review</span></div>}
                  </td>
                  <td><Progress percent={t.progress_percent} />
                      <small className="muted">{t.progress_percent}%</small></td>
                  <td>{t.retry_total || <span className="muted">0</span>}</td>
                  <td>{t.sla_breached ? <span className="badge err">breached</span>
                    : <span className="badge ok">ok</span>}</td>
                  <td className="muted">{when(t.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
