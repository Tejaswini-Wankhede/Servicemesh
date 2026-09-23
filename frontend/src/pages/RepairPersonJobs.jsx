import React, { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api.js'
import { Badge, Empty, ErrorBox, Loading, humanize, when } from '../components/ui.jsx'

const actions = [
  ['DIAGNOSING', 'Diagnose', 'Diagnosis started by repair person'],
  ['REPAIRING', 'Start repair', 'Repair started by repair person'],
  ['COMPLETED', 'Complete', 'Repair completed; device ready for verification'],
]

export default function RepairPersonJobs() {
  const [jobs, setJobs] = useState(null)
  const [provider, setProvider] = useState(null)
  const [error, setError] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [feedback, setFeedback] = useState({})
  const [filter, setFilter] = useState('action')
  const [notes, setNotes] = useState({})
  const [checks, setChecks] = useState({})

  const load = useCallback(async () => {
    try {
      const [p, j] = await Promise.all([api.myProvider(), api.myJobs()])
      setProvider(p)
      setJobs(j)
    } catch (err) {
      setError(err)
    }
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    const timer = setInterval(load, 5000)
    return () => clearInterval(timer)
  }, [load])

  async function act(id, label, fn) {
    setBusyId(id); setError(null)
    setFeedback((current) => ({ ...current, [id]: { type: 'loading', text: `${label}…` } }))
    try {
      const result = await fn()
      await load()
      setFeedback((current) => ({ ...current, [id]: {
        type: 'success',
        text: `${label} succeeded${result?.final_state ? ` (${result.final_state})` : ''}.`,
      } }))
    } catch (err) {
      setFeedback((current) => ({ ...current, [id]: { type: 'error', text: err.message || 'Action failed.' } }))
      setError(err)
    } finally { setBusyId(null) }
  }

  if (error && !jobs) return <ErrorBox error={error} />
  if (!jobs) return <Loading what="repair assignments" />

  const repairJobs = [...new Map(jobs
    .filter((job) => job.booking_ref)
    .map((job) => [job.transaction_id, job])).values()]
  const visibleJobs = repairJobs.filter((job) => filter === 'all' || !['CLOSED', 'COMPLETED'].includes(job.transaction_state))

  return (
    <>
      <div className="page-head">
        <h1>Repair person portal</h1>
        <p>{provider?.name} · <span className="mono">{provider?.code}</span></p>
      </div>
      <ErrorBox error={error} />
      <div className="card">
        <h2>Assigned repair work</h2>
        <div className="tabs"><button className={filter === 'action' ? '' : 'secondary'} onClick={() => setFilter('action')}>Action needed</button><button className={filter === 'all' ? '' : 'secondary'} onClick={() => setFilter('all')}>All assignments</button></div>
        {repairJobs.length === 0 ? <Empty>No repair work is assigned to you. New bookings will appear here.</Empty> : visibleJobs.length === 0 ? <Empty>There is nothing waiting for your action.</Empty> : (
          <table><thead><tr>
            <th>Transaction</th><th>Customer</th><th>Issue</th><th>Technician</th><th>State</th><th>Actions / result</th>
          </tr></thead><tbody>
            {visibleJobs.map((job) => {
              const busy = busyId === job.transaction_id
              const state = job.transaction_state
              const workflowState = state === 'WAITING' ? job.resume_state : state
              const phase = job.repair_phase
              const allowed = [
                workflowState === 'REPAIR_SCHEDULED' && !phase,
                workflowState === 'REPAIR_IN_PROGRESS' && phase === 'DIAGNOSING',
                workflowState === 'REPAIR_IN_PROGRESS' && phase === 'REPAIRING',
              ]
              return <tr key={job.transaction_id}>
                <td className="mono"><Link to={`/transactions/${job.transaction_id}`}>{job.reference}</Link><div className="muted">{when(job.created_at)}</div></td>
                <td>{job.customer_name}</td>
                <td>{humanize(job.issue_type)}<div className="muted">{job.issue_description}</div></td>
                <td>{job.assigned_repair_person || <span className="muted">Assigned to you</span>}</td>
                <td><Badge value={job.transaction_state} /></td>
                <td><div className="checklist">
                  <label><input type="checkbox" checked={checks[job.transaction_id]?.device || false} onChange={(e) => setChecks((c) => ({ ...c, [job.transaction_id]: { ...c[job.transaction_id], device: e.target.checked } }))} /> Device checked in</label>
                  <label><input type="checkbox" checked={checks[job.transaction_id]?.safety || false} onChange={(e) => setChecks((c) => ({ ...c, [job.transaction_id]: { ...c[job.transaction_id], safety: e.target.checked } }))} /> Safety check complete</label>
                </div><textarea className="compact-input" value={notes[job.transaction_id] || ''} onChange={(e) => setNotes((n) => ({ ...n, [job.transaction_id]: e.target.value }))} placeholder="Add diagnosis or repair notes…" /><div className="btn-row">
                  {actions.map(([nextStatus, label, defaultNotes], index) => (
                    <button key={nextStatus} className={nextStatus === 'COMPLETED' ? '' : 'secondary'}
                      disabled={busy || !allowed[index]}
                      title={!allowed[index] ? 'Invalid for the current persisted state' : ''}
                      onClick={() => act(job.transaction_id, label, () => api.updateRepair(job.transaction_id, { status: nextStatus, notes: notes[job.transaction_id] || defaultNotes }))}>
                      {label}
                    </button>
                  ))}
                </div>
                {feedback[job.transaction_id] && <div className={`action-feedback ${feedback[job.transaction_id].type}`}>
                  {feedback[job.transaction_id].text}
                </div>}</td>
              </tr>
            })}
          </tbody></table>
        )}
      </div>
      <div className="card">
        <h3>Repair workflow</h3>
        <p className="muted">Diagnose, start repair, and complete work here. Each action is submitted to the ServiceMesh repair API and the list reloads from persisted backend state.</p>
      </div>
    </>
  )
}
