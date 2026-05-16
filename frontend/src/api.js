const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

export const api = {
  // Reports
  uploadReport: (formData) =>
    fetch(`${BASE}/reports/upload`, { method: 'POST', body: formData }).then(r => {
      if (!r.ok) return r.json().then(e => { throw new Error(e.detail || 'Upload failed') })
      return r.json()
    }),

  getReport: (id) => request(`/reports/${id}`),
  listReports: () => request('/reports/'),

  // Disputes
  createDispute: (data) => request('/disputes/', { method: 'POST', body: JSON.stringify(data) }),
  listDisputes: () => request('/disputes/'),
  getDispute: (id) => request(`/disputes/${id}`),
  approveDispute: (id) => request(`/disputes/${id}/approve`, { method: 'POST', body: JSON.stringify({}) }),
  submitDispute: (id) => request(`/disputes/${id}/submit`, { method: 'POST' }),
  recordResponse: (id, data) => request(`/disputes/${id}/response`, { method: 'POST', body: JSON.stringify(data) }),
  getPendingAction: () => request('/disputes/pending-action'),

  // Letters
  getLetter: (id) => request(`/letters/${id}`),
  regenerateLetter: (id, data) => request(`/letters/${id}/regenerate`, { method: 'POST', body: JSON.stringify(data) }),
  createFurnisherLetter: (data) => request('/letters/furnisher', { method: 'POST', body: JSON.stringify(data) }),
  listLetterTypes: () => request('/letters/types/list'),

  // Users
  createUser: (data) => request('/users/', { method: 'POST', body: JSON.stringify(data) }),
  getUser: (id) => request(`/users/${id}`),
  updateUser: (id, data) => request(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),

  // Dashboard
  getStats: () => request('/dashboard/stats'),
  health: () => request('/health'),
}
