// The API runs separately (Railway). VITE_API_URL is its public origin, e.g.
// https://credit-sniper-api.up.railway.app — baked in at build time. It is
// not a secret: no keys ever go in frontend env vars. Empty = same origin
// (local dev, where Vite proxies /api to the backend).
const API_ORIGIN = (import.meta.env.VITE_API_URL || '').replace(/\/+$/, '')
const BASE = `${API_ORIGIN}/api`

// The app sets this to a function returning the current session token (see
// auth.jsx). It stays null when auth is disabled (local dev), and requests
// simply go without an Authorization header. The token is never persisted
// here — it's fetched fresh per request, so it can't go stale.
let tokenGetter = null
export function setTokenGetter(fn) {
  tokenGetter = fn
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

async function request(path, { method = 'GET', body, form } = {}) {
  const init = { method, headers: {} }
  if (tokenGetter) {
    const token = await tokenGetter()
    if (token) init.headers['Authorization'] = `Bearer ${token}`
  }
  if (form) {
    init.body = form
  } else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }
  const res = await fetch(`${BASE}${path}`, init)
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    const detail = Array.isArray(err.detail) ? err.detail.map(d => d.msg).join('; ') : err.detail
    throw new ApiError(detail || `Request failed (${res.status})`, res.status)
  }
  return res.json()
}

// Fetch a binary file with the bearer token attached (a plain <a href> can't
// send it), then hand the browser a blob to open/save. Used for PDFs that
// live behind the authenticated API.
async function openFile(path) {
  const headers = {}
  if (tokenGetter) {
    const token = await tokenGetter()
    if (token) headers['Authorization'] = `Bearer ${token}`
  }
  const res = await fetch(`${BASE}${path}`, { headers })
  if (!res.ok) throw new ApiError(`Request failed (${res.status})`, res.status)
  const url = URL.createObjectURL(await res.blob())
  window.open(url, '_blank', 'noopener')
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

export const api = {
  dashboard: () => request('/dashboard'),
  activity: () => request('/activity'),

  uploadReport: (file, bureau) => {
    const form = new FormData()
    form.append('file', file)
    form.append('bureau', bureau)
    return request('/reports/upload', { method: 'POST', form })
  },
  listReports: () => request('/reports/'),
  getReport: (id) => request(`/reports/${id}`),
  reportStatus: (id) => request(`/reports/${id}/status`),
  retryExtraction: (id) => request(`/reports/${id}/retry-extraction`, { method: 'POST' }),

  listAccounts: () => request('/accounts/'),
  getAccount: (id) => request(`/accounts/${id}`),
  evaluateAccount: (id) => request(`/accounts/${id}/evaluate`, { method: 'POST' }),

  // ── Operator control plane (admin only) ───────────────────────────
  // Authenticated as the signed-in user; the machine credential belongs to
  // the review agent and is never present in a browser build.
  operatorOperations: () => request('/operator/operations'),
  operatorReports: (limit = 20) => request(`/operator/reports?limit=${limit}`),
  operatorBatchPlan: (id) => request(`/operator/reports/${id}/batch-plan`),
  operatorCheckpoint: (id) => request(`/operator/reports/${id}/checkpoint`),
  operatorDiagnosis: (id) => request(`/operator/reports/${id}/diagnosis`),
  operatorJobs: (reportId) =>
    request(`/operator/jobs${reportId ? `?report_id=${reportId}` : ''}`),
  operatorJob: (jobId) => request(`/operator/jobs/${jobId}`),
  queueBenchmark: (body) =>
    request('/operator/jobs/benchmark-batch', { method: 'POST', body }),

  // Benchmark truth lives server-side: entered or corrected once, then named
  // by (batch, label). The listing carries counts and status only, never the
  // account values, so a report overview holds none of the data it describes.
  operatorTruthList: (reportId) => request(`/operator/reports/${reportId}/truth`),
  operatorTruth: (reportId, batchId, label = 'current') =>
    request(`/operator/reports/${reportId}/batches/${batchId}/truth?label=${encodeURIComponent(label)}`),
  saveOperatorTruth: (reportId, batchId, body) =>
    request(`/operator/reports/${reportId}/batches/${batchId}/truth`, { method: 'PUT', body }),
  verifyOperatorTruth: (reportId, batchId, verified = true, label = 'current') =>
    request(`/operator/reports/${reportId}/batches/${batchId}/truth/verify`
      + `?label=${encodeURIComponent(label)}&verified=${verified}`, { method: 'POST' }),
  draftOperatorTruth: (reportId, batchId, label = 'current') =>
    request(`/operator/reports/${reportId}/batches/${batchId}/truth/draft`
      + `?label=${encodeURIComponent(label)}`, { method: 'POST' }),

  listCases: () => request('/cases/'),
  getCase: (id) => request(`/cases/${id}`),
  openCase: (claimIds, recipient, furnisher = {}) =>
    request('/cases/', {
      method: 'POST',
      body: { claim_ids: claimIds, recipient, furnisher_name: furnisher.name, furnisher_address: furnisher.address },
    }),
  setFurnisherAddress: (id, address) => request(`/cases/${id}/furnisher`, { method: 'PATCH', body: { furnisher_address: address } }),
  generatePackage: (id) => request(`/cases/${id}/package`, { method: 'POST' }),
  openPackagePdf: (id) => openFile(`/cases/${id}/package.pdf`),
  openReportFile: (id) => openFile(`/reports/${id}/file`),
  approveCase: (id) => request(`/cases/${id}/approve`, { method: 'POST' }),
  markSubmitted: (id, body) => request(`/cases/${id}/submitted`, { method: 'POST', body }),
  markDelivered: (id, body) => request(`/cases/${id}/delivered`, { method: 'POST', body }),
  recordResponse: (id, body) => request(`/cases/${id}/response`, { method: 'POST', body }),
  moveCase: (id, target, detail = '') => request(`/cases/${id}/transition`, { method: 'POST', body: { target, detail } }),

  getMe: () => request('/users/me'),
  updateMe: (body) => request('/users/me', { method: 'PATCH', body }),
}
