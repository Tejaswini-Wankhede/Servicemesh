import React, { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api.js'
import { Badge, Empty, ErrorBox, Loading, humanize, when } from '../components/ui.jsx'

export default function ProviderJobs() {
  const [jobs, setJobs] = useState(null)
  const [provider, setProvider] = useState(null)
  const [error, setError] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [technicians, setTechnicians] = useState({})
  const [repairPersons, setRepairPersons] = useState([])
  const [feedback, setFeedback] = useState({})
  const [filter, setFilter] = useState('active')

  const load = useCallback(async () => {
    try {
      const requests = [api.myProvider(), api.myJobs()]
      const [p, j] = await Promise.all(requests)
      setProvider(p); setJobs(j)
      if (p?.kind === 'SERVICE_CENTRE') setRepairPersons(await api.repairPersons())
    } catch (err) { setError(err) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [load])

  async function act(id, label, fn) {
    setBusyId(id); setError(null)
    setFeedback((current) => ({ ...current, [id]: { type: 'loading', text: `${label}…` } }))
    try {
      const result = await fn()
      await load()
      setFeedback((current) => ({ ...current, [id]: { type: 'success', text: `${label} succeeded${result?.final_state ? ` (${result.final_state})` : ''}.` } }))
    } catch (err) {
      setFeedback((current) => ({ ...current, [id]: { type: 'error', text: err.message || 'Action failed.' } }))
      setError(err)
    } finally { setBusyId(null) }
  }

  if (error && !jobs) return <ErrorBox error={error} />
  if (!jobs) return <Loading what="assigned work" />
  const kind = provider?.kind
  const isService = kind === 'SERVICE_CENTRE'
  const isSupplier = kind === 'PARTS_SUPPLIER'
  const unique = [...new Map(jobs.map(j => [j.transaction_id, j])).values()]
  const visible = unique.filter((j) => filter === 'all' || (filter === 'action' ? j.transaction_state === 'REPAIR_SCHEDULED' || j.reservation_ref : j.transaction_state !== 'CLOSED'))

  return (
    <>
      <div className="page-head">
        <h1>{isSupplier ? 'Parts supplier portal' : 'Service centre portal'}</h1>
        <p>{provider?.name} · <span className="mono">{provider?.code}</span></p>
      </div>
      <ErrorBox error={error} />
      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <div className="metric"><div className="label">Operations</div><div className="value">{provider?.total_operations ?? 0}</div></div>
        <div className="metric"><div className="label">Observed success</div><div className="value">{((provider?.success_rate ?? 0) * 100).toFixed(1)}%</div></div>
        <div className="metric"><div className="label">Capacity</div><div className="value">{provider?.available_capacity ?? 0}/{provider?.capacity_total ?? 0}</div></div>
        <div className="metric"><div className="label">SLA</div><div className="value">{provider?.sla_hours ?? 0}h</div></div>
      </div>
      <div className="card">
        <h2>{isSupplier ? 'Incoming part requests' : 'Repair jobs'}</h2>
        <div className="tabs"><button className={filter === 'active' ? '' : 'secondary'} onClick={() => setFilter('active')}>Active</button><button className={filter === 'action' ? '' : 'secondary'} onClick={() => setFilter('action')}>Needs action</button><button className={filter === 'all' ? '' : 'secondary'} onClick={() => setFilter('all')}>All</button></div>
        {unique.length === 0 ? <div className="empty-state compact">
          <div className="empty-icon" aria-hidden="true">{isSupplier ? '▣' : '✓'}</div>
          <h2>{isSupplier ? 'No parts requests yet' : 'No repair jobs yet'}</h2>
          <p>{isSupplier ? 'Compatible component requests will appear here when a customer repair needs your inventory.' : 'New bookings and repair decisions will appear here as customers are routed to your centre.'}</p>
        </div> : visible.length === 0 ? <Empty>No jobs match this view. Try “All” to see completed work.</Empty> : (
          <table><thead><tr>
            <th>Transaction</th><th>Customer</th><th>Issue</th><th>Part / booking</th><th>State</th>{isService && <th>Repair person</th>}<th>Actions</th><th>Result</th>
          </tr></thead><tbody>
          {visible.map(j => {
            const busy = busyId === j.transaction_id
            return <tr key={j.transaction_id}>
              <td className="mono"><Link to={`/transactions/${j.transaction_id}`}>{j.reference}</Link><div className="muted">{when(j.created_at)}</div></td>
              <td>{j.customer_name}</td>
              <td>{humanize(j.issue_type)}<div className="muted">{j.issue_description}</div></td>
              <td className="mono">{j.component_sku || j.booking_ref || j.reservation_ref || '—'}</td>
              <td><Badge value={j.transaction_state} /></td>
              {isService && <td>
                <div className="btn-row">
                  <select aria-label={`Repair person for ${j.reference}`}
                    value={technicians[j.transaction_id] || j.assigned_repair_person || ''}
                    onChange={(e) => setTechnicians((current) => ({ ...current, [j.transaction_id]: e.target.value }))}
                    disabled={busy || !j.booking_ref || j.transaction_state === 'CLOSED'}>
                    <option value="">Choose repair person</option>
                    {repairPersons.map((person) => <option key={person.id} value={person.full_name}>{person.full_name}</option>)}
                  </select>
                  <button className="secondary" disabled={busy || !j.booking_ref || j.transaction_state === 'CLOSED' || !technicians[j.transaction_id]?.trim()}
                    onClick={() => act(j.transaction_id, 'Assign repair person', () => api.assignTechnician(j.transaction_id, technicians[j.transaction_id].trim()))}>
                    Assign
                  </button>
                </div>
                {j.assigned_repair_person && <div className="muted">Assigned: {j.assigned_repair_person}</div>}
              </td>}
              <td><div className="btn-row">
                {isService && j.booking_ref && j.transaction_state === 'REPAIR_SCHEDULED' && <>
                  <button disabled={busy} onClick={() => act(j.transaction_id, 'Accept job', () => api.serviceDecision(j.transaction_id, 'ACCEPT'))}>Accept</button>
                  <button className="danger" disabled={busy} onClick={() => window.confirm('Reject this booking and route it to a fallback service centre?') && act(j.transaction_id, 'Reject job', () => api.serviceDecision(j.transaction_id, 'REJECT'))}>Reject → fallback</button>
                </>}
                {isSupplier && j.reservation_ref && j.transaction_state !== 'CLOSED' &&
                  <button disabled={busy} onClick={() => act(j.transaction_id, 'Dispatch part', () => api.supplierDispatch(j.transaction_id, {eta:'1 business day'}))}>Dispatch part</button>}
              </div></td>
              <td>{feedback[j.transaction_id] && <span className={`action-feedback ${feedback[j.transaction_id].type}`}>
                {feedback[j.transaction_id].text}
              </span>}</td>
            </tr>
          })}
          </tbody></table>
        )}
      </div>
      <div className="card">
        <h3>Important</h3>
        <p className="muted">Actions above call the ServiceMesh backend and the organization simulator. Refreshing the page reads persisted state; no frontend animation advances a transaction.</p>
      </div>
    </>
  )
}
