import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api.js'
import { Badge, Empty, ErrorBox, Loading, Progress, humanize, when } from '../components/ui.jsx'

export default function CustomerTransactions() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.listTransactions().then(setRows).catch(setError)
  }, [])

  if (error) return <ErrorBox error={error} />
  if (!rows) return <Loading what="your requests" />
  const active = rows.find((t) => !['CLOSED', 'CANCELLED', 'REJECTED', 'FAILED'].includes(t.state))
  const nextAction = active && ({
    WAITING: 'We are waiting for a provider to confirm the next step.',
    REPAIR_SCHEDULED: 'Your service centre is ready to accept the repair.',
    REPAIR_IN_PROGRESS: 'Your repair person is working on the device.',
    ESCALATED: 'Review the timeline and resume the request when ready.',
  }[active.state] || 'We are coordinating the next repair step.')

  return (
    <>
      <div className="page-head">
        <h1>Repair requests</h1>
        <p>Track your electronics repair from first diagnosis to parts, warranty and service-centre coordination.</p>
      </div>
      {active && <div className="dashboard-highlight">
        <div><span className="eyebrow">Active repair · {active.reference}</span><h2>{humanize(active.state)}</h2>
          <p>{active.issue_description || humanize(active.issue_type)} · {nextAction}</p>
          <Progress percent={active.progress_percent} /></div>
        <Link className="btn" to={`/transactions/${active.id}`}>View repair timeline</Link>
      </div>}
      <div className="card">
        {rows.length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon" aria-hidden="true">⚡</div>
            <h2>Your repair desk is ready</h2>
            <p>No repair requests yet. Tell us what is happening with your laptop, phone or other electronics and ServiceMesh will route it to the right partners.</p>
            <Link className="btn" to="/new">Start a repair request</Link>
            <small className="muted">Have your order reference and device serial number nearby.</small>
          </div>
        ) : (
          <div className="table-scroll"><table>
            <thead>
              <tr>
                <th>Reference</th><th>Issue</th><th>Device / order</th>
                <th>State</th><th style={{ width: 160 }}>Progress</th><th>Created</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr key={t.id}>
                  <td className="mono"><Link to={`/transactions/${t.id}`}>{t.reference}</Link></td>
                  <td>{humanize(t.issue_type)}</td>
                  <td><span className="mono">{t.serial_number}</span><div className="muted">{t.order_ref}</div></td>
                  <td>
                    <Badge value={t.state} />
                    {t.requires_manual_intervention &&
                      <span className="badge warn" style={{ marginLeft: 6 }}>needs review</span>}
                  </td>
                  <td>
                    <Progress percent={t.progress_percent} />
                    <small className="muted">{t.progress_percent}%</small>
                  </td>
                  <td className="muted">{when(t.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>
    </>
  )
}
