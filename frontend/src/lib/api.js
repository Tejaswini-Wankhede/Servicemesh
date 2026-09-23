// Thin API client.
//
// Every value the UI renders comes through here from the backend. There is no
// fixture data, no seeded component state pretending to be a transaction, and
// no hard-coded dashboard figures anywhere in this app - if the backend is
// down, the UI shows an error rather than a plausible-looking screen.

const BASE = import.meta.env.VITE_API_BASE_URL || ''

const TOKEN_KEY = 'servicemesh.token'
const USER_KEY = 'servicemesh.user'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function getUser() {
  const raw = localStorage.getItem(USER_KEY)
  return raw ? JSON.parse(raw) : null
}

export function setSession(token, user) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

export class ApiError extends Error {
  constructor(status, body) {
    const detail = body && body.detail
    const message =
      (detail && (detail.message || detail)) ||
      (body && body.message) ||
      `Request failed (HTTP ${status})`
    super(typeof message === 'string' ? message : JSON.stringify(message))
    this.status = status
    this.body = body
  }
}

async function request(path, { method = 'GET', body, auth = true } = {}) {
  const headers = { 'Content-Type': 'application/json' }
  if (auth) {
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }

  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (response.status === 204) return null

  let payload = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    // An expired or invalid token should drop the session rather than leave
    // the user staring at repeated errors.
    if (response.status === 401 && auth) {
      clearSession()
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
    }
    throw new ApiError(response.status, payload)
  }
  return payload
}

export const api = {
  login: (email, password) =>
    request('/api/v1/auth/login', {
      method: 'POST',
      body: { email, password },
      auth: false,
    }),
  me: () => request('/api/v1/auth/me'),

  listTransactions: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== '')
    ).toString()
    return request(`/api/v1/transactions${qs ? `?${qs}` : ''}`)
  },
  getTransaction: (id) => request(`/api/v1/transactions/${id}`),
  createTransaction: (payload) =>
    request('/api/v1/transactions', { method: 'POST', body: payload }),
  createFromText: (payload) =>
    request('/api/v1/transactions/natural-language', {
      method: 'POST',
      body: payload,
    }),
  resumeTransaction: (id) =>
    request(`/api/v1/transactions/${id}/resume`, { method: 'POST' }),
  cancelTransaction: (id) =>
    request(`/api/v1/transactions/${id}/cancel`, { method: 'POST' }),

  myProvider: () => request('/api/v1/providers/me'),
  myJobs: () => request('/api/v1/providers/me/jobs'),
  repairPersons: () => request('/api/v1/providers/me/repair-persons'),
  serviceDecision: (id, action) =>
    request(`/api/v1/providers/me/transactions/${id}/service-decision?action=${action}`, { method: 'POST' }),
  supplierDispatch: (id, payload = {}) =>
    request(`/api/v1/providers/me/transactions/${id}/supplier-dispatch`, { method: 'POST', body: payload }),
  updateRepair: (id, payload) =>
    request(`/api/v1/providers/me/transactions/${id}/repair`, {
      method: 'POST',
      body: payload,
    }),
  // Technician assignment is recorded through the provider repair workflow.
  // The backend treats the note as the audit trail until a provider-specific
  // technician directory is available.
  assignTechnician: (id, technician) =>
    request(`/api/v1/providers/me/transactions/${id}/repair`, {
      method: 'POST',
      body: { status: 'CHECKED_IN', notes: `Technician assigned: ${technician}` },
    }),

  metrics: () => request('/api/v1/admin/metrics'),
  providers: () => request('/api/v1/providers'),
  customers: () => request('/api/v1/admin/customers'),
  recoveryMatrix: () => request('/api/v1/admin/recovery-matrix'),
  policies: () => request('/api/v1/admin/policies'),
  simulationStatus: () => request('/api/v1/admin/simulation'),
  setSimulation: (payload) =>
    request('/api/v1/admin/simulation', { method: 'POST', body: payload }),
  resetSimulation: () =>
    request('/api/v1/admin/simulation/reset', { method: 'POST' }),
}
