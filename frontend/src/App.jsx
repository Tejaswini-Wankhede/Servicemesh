import React, { useState } from 'react'
import { Navigate, NavLink, Route, Routes, useNavigate } from 'react-router-dom'
import { clearSession, getUser } from './lib/api.js'

import Login from './pages/Login.jsx'
import CustomerTransactions from './pages/CustomerTransactions.jsx'
import NewRequest from './pages/NewRequest.jsx'
import TransactionDetail from './pages/TransactionDetail.jsx'
import ProviderJobs from './pages/ProviderJobs.jsx'
import RepairPersonJobs from './pages/RepairPersonJobs.jsx'
import AdminDashboard from './pages/AdminDashboard.jsx'
import AdminTransactions from './pages/AdminTransactions.jsx'
import AdminProviders from './pages/AdminProviders.jsx'
import AdminSimulation from './pages/AdminSimulation.jsx'

// Route guarding is a UX affordance only. The backend enforces authorization
// independently on every request, so a user who forces a URL still gets 401/403
// (or 404 for another tenant's transaction) from the API.
function Guard({ user, roles, children }) {
  if (!user) return <Navigate to="/login" replace />
  if (roles && !roles.includes(user.role)) return <Navigate to="/" replace />
  return children
}

function Shell({ user, children }) {
  const navigate = useNavigate()
  const isCustomer = user.role === 'CUSTOMER'
  const isProvider = user.role === 'PROVIDER'
  const isRepairPerson = user.role === 'REPAIR_PERSON'
  const isAdmin = user.role === 'ADMIN'

  function logout() {
    clearSession()
    navigate('/login')
  }

  return (
    <div className="app">
      <div className="demo-banner" role="note"><strong>Demo environment</strong><span>Data and provider decisions are simulated for this workspace.</span></div>
      <aside className="sidebar">
        <div className="brand">Service<span>Mesh</span></div>
        <nav className="nav">
          {isCustomer && <>
            <NavLink to="/transactions">My requests</NavLink>
            <NavLink to="/new">New request</NavLink>
          </>}
          {isProvider && <>
            <NavLink to="/provider/jobs">{user.provider_code?.startsWith('SUP-') ? 'Supplier portal' : 'Service centre'}</NavLink>
            {!user.provider_code?.startsWith('SUP-') && <NavLink to="/repair-person/jobs">Repair person portal</NavLink>}
          </>}
          {isRepairPerson && <NavLink to="/repair-person/jobs">Repair person portal</NavLink>}
          {isAdmin && <>
            <NavLink to="/admin">Dashboard</NavLink>
            <NavLink to="/admin/transactions">Transactions</NavLink>
            <NavLink to="/admin/providers">Organizations</NavLink>
            <NavLink to="/admin/simulation">Failure simulation</NavLink>
          </>}
        </nav>
        <div className="session">
          <strong>{user.full_name}</strong>
          <small>{user.role}{user.provider_code ? ` · ${user.provider_code}` : ''}</small>
          <button className="secondary" style={{ marginTop: 10, width: '100%' }}
                  onClick={logout}>Sign out</button>
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  )
}

export default function App() {
  const [user, setUser] = useState(getUser())

  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<Login onLogin={setUser} />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }

  const home = user.role === 'ADMIN' ? '/admin'
    : user.role === 'PROVIDER' ? '/provider/jobs'
      : user.role === 'REPAIR_PERSON' ? '/repair-person/jobs' : '/transactions'

  return (
    <Shell user={user}>
      <Routes>
        <Route path="/" element={<Navigate to={home} replace />} />
        <Route path="/login" element={<Navigate to={home} replace />} />

        <Route path="/transactions" element={
          <Guard user={user} roles={['CUSTOMER']}><CustomerTransactions /></Guard>} />
        <Route path="/new" element={
          <Guard user={user} roles={['CUSTOMER']}><NewRequest /></Guard>} />

        <Route path="/provider/jobs" element={
          <Guard user={user} roles={['PROVIDER', 'ADMIN']}><ProviderJobs /></Guard>} />
        <Route path="/repair-person/jobs" element={
          <Guard user={user} roles={['PROVIDER', 'REPAIR_PERSON']}><RepairPersonJobs /></Guard>} />

        <Route path="/admin" element={
          <Guard user={user} roles={['ADMIN']}><AdminDashboard /></Guard>} />
        <Route path="/admin/transactions" element={
          <Guard user={user} roles={['ADMIN']}><AdminTransactions /></Guard>} />
        <Route path="/admin/providers" element={
          <Guard user={user} roles={['ADMIN']}><AdminProviders /></Guard>} />
        <Route path="/admin/simulation" element={
          <Guard user={user} roles={['ADMIN']}><AdminSimulation /></Guard>} />

        <Route path="/transactions/:id" element={
          <Guard user={user}><TransactionDetail user={user} /></Guard>} />

        <Route path="*" element={<Navigate to={home} replace />} />
      </Routes>
    </Shell>
  )
}
