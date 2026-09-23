import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { Badge, ErrorBox, Loading } from '../components/ui.jsx'

// Reliability figures are ServiceMesh's own observations of each organization,
// accumulated from real operation outcomes - not numbers a provider reports
// about itself.
export default function AdminProviders() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => { api.providers().then(setRows).catch(setError) }, [])

  if (error) return <ErrorBox error={error} />
  if (!rows) return <Loading what="organizations" />

  const groups = rows.reduce((acc, p) => {
    (acc[p.kind] ||= []).push(p)
    return acc
  }, {})

  return (
    <>
      <div className="page-head">
        <h1>Participating organizations</h1>
        <p>Observed reliability drives provider selection scoring.</p>
      </div>

      {Object.entries(groups).map(([kind, list]) => (
        <div className="card" key={kind}>
          <h2>{kind.replace('_', ' ')}</h2>
          <table>
            <thead><tr>
              <th>Code</th><th>Name</th><th>Region</th><th>Ops</th>
              <th>Success rate</th><th>Avg latency</th><th>Capacity</th>
              <th>SLA</th><th>Cost index</th><th>SLA violations</th>
            </tr></thead>
            <tbody>
              {list.map((p) => (
                <tr key={p.id}>
                  <td className="mono">{p.code}</td>
                  <td>{p.name}</td>
                  <td>{p.region}</td>
                  <td>{p.total_operations}</td>
                  <td>
                    {p.total_operations === 0
                      ? <span className="muted">no data</span>
                      : <Badge value={p.success_rate >= 0.9 ? 'OK'
                        : p.success_rate >= 0.6 ? 'WARN' : 'ERR'}>
                          {(p.success_rate * 100).toFixed(1)}%
                        </Badge>}
                    <div className="muted" style={{ fontSize: 11.5 }}>
                      {p.successful_operations} ok / {p.failed_operations} failed
                    </div>
                  </td>
                  <td className="muted">{p.avg_latency_ms.toFixed(0)} ms</td>
                  <td>{p.available_capacity}/{p.capacity_total}</td>
                  <td>{p.sla_hours}h</td>
                  <td>{p.cost_index}</td>
                  <td>{p.sla_violations}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  )
}
