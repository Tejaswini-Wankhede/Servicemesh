import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, setSession } from '../lib/api.js'
import { ErrorBox } from '../components/ui.jsx'

const DEMO = [
  ['Customer (Pune)', 'aarti@example.com', 'customer123'],
  ['Customer (Nagpur)', 'rohit@example.com', 'customer123'],
  ['Service centre', 'svc-pune-01@partners.servicemesh.io', 'provider123'],
  ['Repair person', 'repair-person@svc-pune.servicemesh.io', 'repair123'],
  ['Parts supplier', 'sup-a@partners.servicemesh.io', 'provider123'],
  ['Operations admin', 'admin@servicemesh.io', 'admin123'],
]

export default function Login({ onLogin }) {
  const [email, setEmail] = useState('aarti@example.com')
  const [password, setPassword] = useState('customer123')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()

  async function submit(e) {
    e.preventDefault()
    setBusy(true); setError(null)
    try {
      const res = await api.login(email, password)
      const user = {
        id: res.user_id, full_name: res.full_name, role: res.role,
        customer_id: res.customer_id, provider_id: res.provider_id,
        provider_code: res.provider_code,
      }
      setSession(res.access_token, user)
      onLogin(user)
      navigate(res.role === 'ADMIN' ? '/admin'
        : res.role === 'PROVIDER' ? '/provider/jobs'
          : res.role === 'REPAIR_PERSON' ? '/repair-person/jobs' : '/transactions')
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="card login-card">
        <h1>Service<span style={{ color: 'var(--accent)' }}>Mesh</span></h1>
        <p className="muted" style={{ marginTop: 0 }}>
          Cross-company after-sales coordination
        </p>
        <ErrorBox error={error} />
        <form onSubmit={submit}>
          <label>
            <span>Email</span>
            <input value={email} onChange={(e) => setEmail(e.target.value)}
                   type="email" autoComplete="username" required />
          </label>
          <label>
            <span>Password</span>
            <input value={password} onChange={(e) => setPassword(e.target.value)}
                   type="password" autoComplete="current-password" required />
          </label>
          <button type="submit" disabled={busy} style={{ width: '100%' }}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        <div className="hint">
          <strong>Demo accounts</strong> (synthetic data)
          {DEMO.map(([label, mail, pw]) => (
            <div key={mail} style={{ marginTop: 5 }}>
              <a href="#" onClick={(e) => { e.preventDefault(); setEmail(mail); setPassword(pw) }}>
                {label}
              </a>{' '}
              <code>{mail}</code>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
