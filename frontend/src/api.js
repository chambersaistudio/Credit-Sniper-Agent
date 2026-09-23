const BASE = '/api'

async function request(path, { method = 'GET', body, form } = {}) {
  const init = { method, headers: {} }
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
    throw new Error(detail || `Request failed (${res.status})`)
  }
  return res.json()
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

  listAccounts: () => request('/accounts/'),
  getAccount: (id) => request(`/accounts/${id}`),
  evaluateAccount: (id) => request(`/accounts/${id}/evaluate`, { method: 'POST' }),

  listCases: () => request('/cases/'),
  getCase: (id) => request(`/cases/${id}`),
  openCase: (claimIds, recipient, furnisher = {}) =>
    request('/cases/', {
      method: 'POST',
      body: { claim_ids: claimIds, recipient, furnisher_name: furnisher.name, furnisher_address: furnisher.address },
    }),
  setFurnisherAddress: (id, address) => request(`/cases/${id}/furnisher`, { method: 'PATCH', body: { furnisher_address: address } }),
  generatePackage: (id) => request(`/cases/${id}/package`, { method: 'POST' }),
  packagePdfUrl: (id) => `${BASE}/cases/${id}/package.pdf`,
  approveCase: (id) => request(`/cases/${id}/approve`, { method: 'POST' }),
  markSubmitted: (id, body) => request(`/cases/${id}/submitted`, { method: 'POST', body }),
  markDelivered: (id, body) => request(`/cases/${id}/delivered`, { method: 'POST', body }),
  recordResponse: (id, body) => request(`/cases/${id}/response`, { method: 'POST', body }),
  moveCase: (id, target, detail = '') => request(`/cases/${id}/transition`, { method: 'POST', body: { target, detail } }),

  getMe: () => request('/users/me'),
  updateMe: (body) => request('/users/me', { method: 'PATCH', body }),
}
