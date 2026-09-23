import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { ErrorBox, Loading } from '../components/ui.jsx'

const SERVICES = ['marketplace', 'manufacturer', 'warranty',
                  'service_centre', 'parts_supplier']

const EXPLAIN = {
  NORMAL: 'Behave correctly.',
  TIMEOUT: 'Hang past the client deadline. Never commits.',
  TEMPORARILY_UNAVAILABLE: '503 with Retry-After — classified TRANSIENT, so it is retried.',
  PERMANENT_FAILURE: '500 that will never succeed — classified PERMANENT, so it is NOT retried.',
  INVALID_RESPONSE: '200 with a body that violates the published contract.',
  HIGH_LATENCY: 'Slow but correct — exercises SLA logic rather than failure logic.',
  TIMEOUT_AFTER_COMMIT:
    'Commits, then withholds the response. Produces a genuinely UNKNOWN outcome, ' +
    'which forces reconciliation via the idempotency key.',
}

export default function AdminSimulation() {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)
  const [form, setForm] = useState({
    service: 'parts_supplier', mode: 'TEMPORARILY_UNAVAILABLE',
    operation: '', count: 2, latency_seconds: 0,
  })
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.simulationStatus().then(setStatus).catch(setError)
  }, [])
  useEffect(() => { load() }, [load])

  function set(k, v) { setForm((f) => ({ ...f, [k]: v })) }

  async function apply(e) {
    e.preventDefault()
    setBusy(true); setError(null); setNote(null)
    try {
      const payload = {
        service: form.service, mode: form.mode,
        operation: form.operation || null,
        count: form.count ? Number(form.count) : null,
        latency_seconds: Number(form.latency_seconds) || 0,
      }
      const res = await api.setSimulation(payload)
      setNote(`Applied to ${form.service} via the "${res.channel}" channel.`)
      load()
    } catch (err) { setError(err) } finally { setBusy(false) }
  }

  async function reset() {
    setBusy(true); setError(null); setNote(null)
    try { await api.resetSimulation(); setNote('All organizations returned to NORMAL.'); load() }
    catch (err) { setError(err) } finally { setBusy(false) }
  }

  if (!status) return <Loading what="simulation state" />

  const modes = status.available_modes || Object.keys(EXPLAIN)

  return (
    <>
      <div className="page-head">
        <h1>Failure simulation</h1>
        <p>
          Push an organization into a specific misbehaviour to demonstrate recovery
          against real running services. Failures are injected before the
          organization&apos;s business logic runs, so its database is never left
          half-written.
        </p>
      </div>

      <ErrorBox error={error} />
      {note && <div className="ok-box">{note}</div>}

      <div className="card">
        <h2>Apply a failure mode</h2>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 0 }}>
          Control channel: <strong>{status.channel}</strong>
          {status.channel === 'http'
            ? ' — each organization runs as a separate service and is configured over its own admin API.'
            : ' — organizations are running inside this process.'}
        </p>
        <form onSubmit={apply}>
          <div className="grid cols-3">
            <label><span>Organization</span>
              <select value={form.service} onChange={(e) => set('service', e.target.value)}>
                {SERVICES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select></label>
            <label><span>Mode</span>
              <select value={form.mode} onChange={(e) => set('mode', e.target.value)}>
                {modes.map((m) => <option key={m} value={m}>{m}</option>)}
              </select></label>
            <label><span>Affected calls (blank = until reset)</span>
              <input type="number" min="1" value={form.count}
                     onChange={(e) => set('count', e.target.value)} /></label>
            <label><span>Operation key (blank = all)</span>
              <input value={form.operation} placeholder="e.g. inventory.check"
                     onChange={(e) => set('operation', e.target.value)} /></label>
            <label><span>Latency seconds (HIGH_LATENCY)</span>
              <input type="number" min="0" step="0.5" value={form.latency_seconds}
                     onChange={(e) => set('latency_seconds', e.target.value)} /></label>
          </div>
          <p className="muted" style={{ fontSize: 12.5 }}>{EXPLAIN[form.mode]}</p>
          <div className="btn-row">
            <button type="submit" disabled={busy}>Apply</button>
            <button type="button" className="secondary" disabled={busy} onClick={reset}>
              Reset all to NORMAL
            </button>
          </div>
        </form>
      </div>

      <div className="card">
        <h2>Current state</h2>
        <table>
          <thead><tr><th>Organization</th><th>Active rules</th></tr></thead>
          <tbody>
            {Object.entries(status.services).map(([name, s]) => (
              <tr key={name}>
                <td className="mono">{name}</td>
                <td>
                  {s.error ? <span className="badge err">{s.error}</span>
                    : !s.rules || s.rules.length === 0
                      ? <span className="badge ok">NORMAL</span>
                      : s.rules.map((r, i) => (
                        <div key={i}>
                          <span className="badge warn">{r.mode}</span>{' '}
                          <span className="mono muted" style={{ fontSize: 11.5 }}>
                            {r.operation || 'all operations'}
                            {r.remaining !== null && r.remaining !== undefined
                              ? ` · ${r.remaining} calls left` : ' · until reset'}
                          </span>
                        </div>
                      ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
