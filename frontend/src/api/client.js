const CSRF_HEADER = 'X-CSRF-Token'

let csrfToken = ''

export function setCsrfToken(token) {
  csrfToken = token
}

function getCsrfToken() {
  return csrfToken
}

async function request(path, options = {}) {
  const headers = { ...options.headers }
  const method = (options.method || 'GET').toUpperCase()
  if (method !== 'GET' && method !== 'HEAD') {
    headers[CSRF_HEADER] = getCsrfToken()
  }
  if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
    options.body = JSON.stringify(options.body)
  }
  const res = await fetch(path, { ...options, headers, credentials: 'same-origin' })
  if (res.status === 401) {
    window.location.href = '/login'
    throw new Error('unauthorized')
  }
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status}: ${text}`)
  }
  return res.json()
}

export const api = {
  listCompanies: (params) => request('/api/v1/companies?' + new URLSearchParams(params || {})),
  listJobs: (params) => request('/api/v1/jobs?' + new URLSearchParams(params || {})),
  getJob: (id) => request(`/api/v1/jobs/${id}`),
  listApplications: (params) => request('/api/v1/applications?' + new URLSearchParams(params || {})),
  createApplication: (data) => request('/api/v1/applications', { method: 'POST', body: data }),
  submitApplication: (id) => request(`/api/v1/applications/${id}/submit`, { method: 'POST' }),
  transitionApplication: (id, data) => request(`/api/v1/applications/${id}/transition`, { method: 'POST', body: data }),
  listContacts: (companyId) => request(`/api/v1/companies/${companyId}/contacts`),
  createContact: (data) => request('/api/v1/contacts', { method: 'POST', body: data }),
}
