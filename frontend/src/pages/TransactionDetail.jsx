import React, { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../lib/api.js'
import {
  Badge, Empty, ErrorBox, Json, Loading, Progress, Timeline, duration, humanize, when,
} from '../components/ui.jsx'

/**
 * The single most important screen: one cross-company distributed transaction,
 * fully accounted for. Every value shown is read from the backend - the states,
 * the per-attempt latencies, the failure classifications, the recovery rule
 * that fired and why, the compatibility verdict, the provider score breakdown.
 * Nothing here is illustrative.
 */
export default function TransactionDetail({ user }) {
  const { id } = useParams()
  const [txn, setTxn] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState('operations')

  const load = useCallback(async () => {
    try {
      setTxn(await api.getTransaction(id))
    } catch (err) {
      setError(err)
    }
  }, [id])

  useEffect(() => { load() }, [load])

  // Poll while the transaction is still moving, so provider-side updates and
  // recovery progress appear without a manual refresh.
  useEffect(() => {
    if (!txn) return undefined
    const settled = ['CLOSED', 'REJECTED', 'CANCELLED', 'FAILED', 'ESCALATED']
    if (settled.includes(txn.state)) return undefined
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [txn, load])

  async function act(fn) {
    setBusy(true); setError(null)
    try { await fn(); await load() } catch (err) { setError(err) } finally { setBusy(false) }
  }

  if (error && !txn) return <ErrorBox error={error} />
  if (!txn) return <Loading what="transaction" />

  const canResume = ['ESCALATED', 'WAITING', 'RETRYING', 'TIMEOUT'].includes(txn.state)
  const canCancel = !['CLOSED', 'REJECTED', 'CANCELLED', 'FAILED'].includes(txn.state)
  const isAdmin = user.role === 'ADMIN'

  return (
    <>
      <div className="page-head">
        <h1 className="mono">{txn.reference}</h1>
        <p>
          {humanize(txn.issue_type)} · {txn.serial_number} · {txn.customer_name}
          {' · correlation '}<span className="mono">{txn.correlation_id.slice(0, 8)}</span>
        </p>
      </div>

      <ErrorBox error={error} />

      {/* ----------------------------------------------------- status bar */}
      <div className="card">
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
          <Badge value={txn.state} />
          {txn.outcome && <Badge value={txn.outcome} />}
          {txn.sla_breached && <span className="badge err">SLA breached</span>}
          {txn.requires_manual_intervention &&
            <span className="badge warn">manual review required</span>}
          {txn.retry_total > 0 &&
            <span className="badge info">{txn.retry_total} retries</span>}
          <div style={{ flex: 1, minWidth: 200 }}>
            <Progress percent={txn.progress_percent} />
          </div>
          <span className="muted">{txn.progress_percent}%</span>
          {(canResume || canCancel) && (
            <div className="btn-row">
              {canResume && (user.role === 'CUSTOMER' || isAdmin) && (
                <button disabled={busy}
                        onClick={() => act(() => api.resumeTransaction(txn.id))}>
                  Resume
                </button>
              )}
              {canCancel && (
                <button className="danger" disabled={busy}
                        onClick={() => window.confirm('Cancel this repair request? This cannot be undone.') && act(() => api.cancelTransaction(txn.id))}>
                  Cancel
                </button>
              )}
            </div>
          )}
        </div>
        {txn.outcome_reason && (
          <p className="muted" style={{ marginBottom: 0, marginTop: 10 }}>
            {txn.outcome_reason}
          </p>
        )}
      </div>

      {/* ------------------------------------------------------- timeline */}
      <div className="card">
        <h2>Workflow across organizations</h2>
        <Timeline nodes={txn.timeline} />
        <p className="muted" style={{ fontSize: 12, marginBottom: 0, marginTop: 10 }}>
          Resume point: <span className="mono">{txn.resume_state}</span> — recovery
          restarts from here, so completed cross-organization work is never repeated.
        </p>
      </div>

      {/* --------------------------------------------------------- facts */}
      <div className="grid cols-2">
        <div className="card">
          <h3>Request</h3>
          <dl className="kv">
            <dt>Order</dt><dd className="mono">{txn.order_ref}</dd>
            <dt>Serial</dt><dd className="mono">{txn.serial_number}</dd>
            <dt>Model</dt><dd className="mono">{txn.model_code || '—'}</dd>
            <dt>Issue</dt><dd>{humanize(txn.issue_type)} ({humanize(txn.urgency)})</dd>
            <dt>Description</dt><dd>{txn.issue_description}</dd>
            <dt>SLA due</dt><dd>{when(txn.sla_due_at)}</dd>
          </dl>
        </div>
        <div className="card">
          <h3>Resolution</h3>
          <dl className="kv">
            <dt>Component type</dt><dd>{txn.required_component_type || '—'}</dd>
            <dt>Selected part</dt><dd className="mono">{txn.selected_component_sku || '—'}</dd>
            <dt>Service centre</dt><dd className="mono">{txn.service_provider_code || '—'}</dd>
            <dt>Supplier</dt><dd className="mono">{txn.supplier_code || '—'}</dd>
            <dt>Reservation</dt><dd className="mono">{txn.part_reservation_ref || '—'}</dd>
            <dt>Booking</dt><dd className="mono">{txn.service_booking_ref || '—'}</dd>
            <dt>Technician</dt><dd>{txn.assigned_repair_person || '—'}</dd>
            <dt>Diagnosis</dt><dd>{txn.diagnosis_notes || '—'}</dd>
            <dt>Repair notes</dt><dd>{txn.repair_notes || '—'}</dd>
            <dt>Part delivery</dt><dd>{txn.part_delivery_status || '—'}{txn.part_eta ? ` · ETA ${txn.part_eta}` : ''}</dd>
            <dt>Verification</dt><dd>{txn.verification_result || 'Pending'}</dd>
            {txn.excluded_provider_codes.length > 0 && (<>
              <dt>Ruled out</dt>
              <dd className="mono">{txn.excluded_provider_codes.join(', ')}</dd>
            </>)}
            {txn.excluded_component_skus.length > 0 && (<>
              <dt>Parts rejected</dt>
              <dd className="mono">{txn.excluded_component_skus.join(', ')}</dd>
            </>)}
          </dl>
        </div>
      </div>

      <div className="card">
        <h2>Customer notifications</h2>
        {txn.notifications?.length ? txn.notifications.map(n => (
          <div key={n.id} className="notice" style={{ padding: 12, borderBottom: '1px solid var(--border)' }}>
            <strong>{n.title}</strong><div>{n.body}</div><small className="muted">{when(n.created_at)}</small>
          </div>
        )) : <Empty>No notifications recorded yet.</Empty>}
      </div>

      {/* -------------------------------------------------- participants */}
      <div className="card">
        <h2>Participating organizations</h2>
        {txn.participants.length === 0 ? <Empty>None yet.</Empty> : (
          <table>
            <thead><tr>
              <th>Organization</th><th>Role</th><th>Joined at</th>
              <th>Operations</th><th>Failed</th>
            </tr></thead>
            <tbody>
              {txn.participants.map((p) => (
                <tr key={p.provider_code}>
                  <td><strong>{p.provider_name}</strong>
                      <div className="mono muted">{p.provider_code}</div></td>
                  <td><Badge value={p.kind}>{p.kind}</Badge></td>
                  <td className="mono muted">{p.joined_state}</td>
                  <td>{p.operations_total}</td>
                  <td>{p.operations_failed > 0
                    ? <span className="badge err">{p.operations_failed}</span>
                    : <span className="muted">0</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* ---------------------------------------------------------- tabs */}
      <div className="card">
        <div className="btn-row" style={{ marginBottom: 14 }}>
          {[
            ['operations', `Operations (${txn.operations.length})`],
            ['decisions', `Decisions (${txn.decisions.length})`],
            ['failures', `Failures & recovery (${txn.failures.length + txn.recovery_actions.length})`],
            ['history', `State history (${txn.state_history.length})`],
            ['events', `Events (${txn.events.length})`],
            ['evidence', 'Evidence'],
          ].map(([key, label]) => (
            <button key={key} className={tab === key ? '' : 'secondary'}
                    onClick={() => setTab(key)}>{label}</button>
          ))}
        </div>

        {tab === 'operations' && <Operations ops={txn.operations} />}
        {tab === 'decisions' && <Decisions decisions={txn.decisions} />}
        {tab === 'failures' && (
          <Failures failures={txn.failures} recoveries={txn.recovery_actions} />
        )}
        {tab === 'history' && <History history={txn.state_history} />}
        {tab === 'events' && <Events events={txn.events} />}
        {tab === 'evidence' && <Evidence txn={txn} />}
      </div>
    </>
  )
}

function Operations({ ops }) {
  if (ops.length === 0) return <Empty>No operations recorded.</Empty>
  return (
    <table>
      <thead><tr>
        <th>#</th><th>Operation</th><th>Organization</th><th>Status</th>
        <th>Attempts</th><th>Latency</th><th>Idempotency</th>
      </tr></thead>
      <tbody>
        {ops.map((o) => (
          <tr key={o.id}>
            <td className="muted">{o.sequence}</td>
            <td className="mono">{o.operation_type}</td>
            <td className="mono">{o.provider_code || '—'}</td>
            <td><Badge value={o.status} /></td>
            <td>
              {o.attempt_count}/{o.max_attempts}
              {o.attempt_count > 1 && (
                <details>
                  <summary>attempts</summary>
                  <ul className="reason-list">
                    {o.attempts.map((a) => (
                      <li key={a.attempt_number}>
                        #{a.attempt_number} {a.status}
                        {a.http_status ? ` HTTP ${a.http_status}` : ''}
                        {a.latency_ms ? ` · ${duration(a.latency_ms)}` : ''}
                        {a.error_message ? ` · ${a.error_message}` : ''}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </td>
            <td className="muted">{duration(o.latency_ms)}</td>
            <td>
              {o.has_idempotency_protection
                ? <span className="badge ok" title={o.idempotency_key || ''}>protected</span>
                : <span className="muted">n/a (read-only)</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Decisions({ decisions }) {
  if (decisions.length === 0) return <Empty>No decisions recorded.</Empty>
  return (
    <table>
      <thead><tr>
        <th>Type</th><th>Subject</th><th>Verdict</th><th>Engine</th><th>Reasons</th>
      </tr></thead>
      <tbody>
        {decisions.map((d) => (
          <tr key={d.id}>
            <td className="mono">{d.decision_type}</td>
            <td className="mono">{d.subject_label || '—'}</td>
            <td><Badge value={d.verdict} /></td>
            <td className="muted" style={{ fontSize: 12 }}>
              {d.engine}<div>{d.rule_version}</div>
            </td>
            <td>
              <ul className="reason-list">
                {d.reasons.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
              <Json data={d.scores} label="score breakdown" />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Failures({ failures, recoveries }) {
  if (failures.length === 0 && recoveries.length === 0) {
    return <Empty>No failures occurred. Every operation succeeded first time.</Empty>
  }
  return (
    <>
      <h3>Failures</h3>
      {failures.length === 0 ? <p className="muted">None.</p> : (
        <table>
          <thead><tr>
            <th>When</th><th>Organization</th><th>Reason</th><th>Class</th>
            <th>At state</th><th>Attempt</th><th>Message</th>
          </tr></thead>
          <tbody>
            {failures.map((f) => (
              <tr key={f.id}>
                <td className="muted">{when(f.created_at)}</td>
                <td className="mono">{f.provider_code || '—'}</td>
                <td className="mono">{f.failure_reason}</td>
                <td><Badge value={f.failure_type} /></td>
                <td className="mono muted">{f.state_at_failure}</td>
                <td>{f.attempt_number}</td>
                <td className="muted">{f.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3 style={{ marginTop: 20 }}>Recovery decisions</h3>
      {recoveries.length === 0 ? <p className="muted">None.</p> : (
        <table>
          <thead><tr>
            <th>When</th><th>Strategy</th><th>Rule</th><th>Rationale</th>
            <th>Resumed from</th><th>Result</th>
          </tr></thead>
          <tbody>
            {recoveries.map((r) => (
              <tr key={r.id}>
                <td className="muted">{when(r.created_at)}</td>
                <td><Badge value={r.strategy}>{r.strategy}</Badge></td>
                <td className="mono" style={{ fontSize: 11.5 }}>{r.rule_id}</td>
                <td>{r.rationale}</td>
                <td className="mono muted">{r.resumed_from_state || '—'}</td>
                <td>{r.succeeded === null ? <span className="muted">—</span>
                  : r.succeeded ? <span className="badge ok">applied</span>
                    : <span className="badge err">failed</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

function History({ history }) {
  return (
    <table>
      <thead><tr><th>When</th><th>From</th><th>To</th><th>Actor</th><th>Reason</th></tr></thead>
      <tbody>
        {history.map((h, i) => (
          <tr key={i}>
            <td className="muted">{when(h.created_at)}</td>
            <td className="mono muted">{h.from_state || '—'}</td>
            <td className="mono"><Badge value={h.to_state} /></td>
            <td className="muted">{h.actor}</td>
            <td>{h.reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Events({ events }) {
  if (events.length === 0) return <Empty>No events.</Empty>
  return (
    <table>
      <thead><tr><th>When</th><th>Event</th><th>Source</th><th>Payload</th></tr></thead>
      <tbody>
        {events.map((e) => (
          <tr key={e.event_id}>
            <td className="muted">{when(e.created_at)}</td>
            <td className="mono">{e.event_type}</td>
            <td className="muted">{e.source_service}</td>
            <td><Json data={e.payload} label="payload" /></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Evidence({ txn }) {
  const blocks = [
    ['Marketplace — purchase', txn.purchase_evidence],
    ['Manufacturer — product', txn.product_evidence],
    ['Warranty — contract', txn.warranty_evidence],
    ['Warranty — coverage', txn.coverage_evidence],
    ['Issue extraction', txn.nlp_extraction],
  ].filter(([, v]) => v)

  if (blocks.length === 0) return <Empty>No evidence gathered yet.</Empty>
  return (
    <div className="grid cols-2">
      {blocks.map(([label, data]) => (
        <div key={label}>
          <h3>{label}</h3>
          <pre className="json">{JSON.stringify(data, null, 2)}</pre>
        </div>
      ))}
    </div>
  )
}
