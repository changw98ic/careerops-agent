const CSRF_HEADER = 'X-CSRF-Token'

let csrfToken = ''

export function setCsrfToken(token) {
  csrfToken = token
}

function getCsrfToken() {
  return csrfToken
}

async function handleUnauthorized() {
  // Dynamic import to avoid circular dependency with session.js
  try {
    const session = await import('../stores/session.js')
    session.clearUser()
    session.status.value = 'anonymous'
  } catch {
    // Session store not available; proceed with redirect only
  }
  window.location.href = '/login'
}

async function request(path, options = {}) {
  const headers = { ...options.headers }
  const method = (options.method || 'GET').toUpperCase()
  if (method !== 'GET' && method !== 'HEAD') {
    headers[CSRF_HEADER] = getCsrfToken()
  }
  const isForm = options.body instanceof FormData
  if (options.body && typeof options.body === 'object' && !isForm) {
    headers['Content-Type'] = 'application/json'
    options.body = JSON.stringify(options.body)
  }
  // FormData: let the browser set the multipart boundary; do not add Content-Type.
  const res = await fetch(path, { ...options, headers, credentials: 'same-origin' })
  if (res.status === 401) {
    await handleUnauthorized()
    throw new Error('unauthorized')
  }
  if (!res.ok) {
    const text = await res.text()
    // Attach a structured envelope so views can branch on code/status (e.g.
    // 503 dependency-not-ready vs 409 invalid-state) without losing the
    // original message shape existing tests rely on.
    const err = new Error(`${res.status}: ${text}`)
    err.status = res.status
    const parsed = parseEnvelope(text)
    if (parsed) {
      err.code = parsed.code
      err.messageText = parsed.message
      err.details = parsed.details
      err.retryable = parsed.retryable
    }
    throw err
  }
  return res.json()
}

function parseEnvelope(text) {
  if (!text || typeof text !== 'string') return null
  try {
    const body = JSON.parse(text)
    if (body && typeof body === 'object' && body.error) {
      const e = body.error
      return {
        code: e.code,
        message: e.message || '',
        details: e.details ?? null,
        retryable: !!e.retryable,
      }
    }
  } catch {
    // Not JSON — fall through; callers still have the raw status.
  }
  return null
}

/**
 * Extract a structured error view from an Error thrown by `request()`.
 * Returns {status, code, message, details, retryable, isDependencyNotReady}.
 * `isDependencyNotReady` covers both 503 codes the backend emits when a
 * repository/capability is missing (DEPENDENCY_NOT_READY / UNAVAILABLE_DEPENDENCY).
 */
export function parseApiError(err) {
  const status = err?.status
  const code = err?.code
  const message = err?.messageText || err?.message || ''
  const isDependencyNotReady =
    status === 503 ||
    code === 'DEPENDENCY_NOT_READY' ||
    code === 'UNAVAILABLE_DEPENDENCY'
  return {
    status,
    code,
    message,
    details: err?.details ?? null,
    retryable: !!err?.retryable,
    isDependencyNotReady,
  }
}

export const api = {
  getMe: () => request('/api/v1/me'),
  listCompanies: (params) => request('/api/v1/companies?' + new URLSearchParams(params || {})),
  listJobs: (params) => request('/api/v1/jobs?' + new URLSearchParams(params || {})),
  getJob: (id) => request(`/api/v1/jobs/${id}`),
  listApplications: (params) => request('/api/v1/applications?' + new URLSearchParams(params || {})),
  createApplication: (data) => request('/api/v1/applications', { method: 'POST', body: data }),
  submitApplication: (id) => request(`/api/v1/applications/${id}/submit`, { method: 'POST' }),
  getEmailDraft: (id) => request(`/api/v1/applications/${id}/email-draft`),
  sendApplicationEmail: (id, data) => request(`/api/v1/applications/${id}/send-email`, { method: 'POST', body: data }),
  transitionApplication: (id, data) => request(`/api/v1/applications/${id}/transition`, { method: 'POST', body: data }),
  listContacts: (companyId) => request(`/api/v1/companies/${companyId}/contacts`),
  createContact: (data) => request('/api/v1/contacts', { method: 'POST', body: data }),

  // -- Career profile (Section 3) --
  getActiveProfile: () => request('/api/v1/profile'),
  listProfileVersions: (params) =>
    request('/api/v1/profile/versions?' + new URLSearchParams(params || {})),
  getProfileVersion: (id) => request(`/api/v1/profile/versions/${id}`),
  createProfileVersion: (data) => request('/api/v1/profile', { method: 'POST', body: data }),
  activateProfileVersion: (id) =>
    request(`/api/v1/profile/versions/${id}/activate`, { method: 'POST' }),

  // -- Resume versions (Section 3) --
  listResumes: (params) =>
    request('/api/v1/resumes?' + new URLSearchParams(params || {})),
  listEligibleResumes: () => request('/api/v1/resumes/eligible'),
  getResume: (id) => request(`/api/v1/resumes/${id}`),
  registerResume: ({ file, target_type = 'general', source_reference = '' }) => {
    const form = new FormData()
    form.append('file', file)
    form.append('target_type', target_type)
    if (source_reference) form.append('source_reference', source_reference)
    return request('/api/v1/resumes', { method: 'POST', body: form })
  },
  confirmResume: (id) => request(`/api/v1/resumes/${id}/confirm`, { method: 'POST' }),
  listResumeEvidence: (id) => request(`/api/v1/resumes/${id}/evidence`),

  // -- Evidence review (Section 3) --
  listEvidence: (params) =>
    request('/api/v1/evidence?' + new URLSearchParams(params || {})),
  getEvidence: (id) => request(`/api/v1/evidence/${id}`),
  confirmEvidence: (id, body) =>
    request(`/api/v1/evidence/${id}/confirm`, { method: 'POST', body: body || {} }),
  rejectEvidence: (id, body) =>
    request(`/api/v1/evidence/${id}/reject`, { method: 'POST', body: body || {} }),

  // -- Crawl sources (Section 4) --
  listCrawlSources: (params) =>
    request('/api/v1/crawl-sources?' + new URLSearchParams(params || {})),
  getCrawlSource: (id) => request(`/api/v1/crawl-sources/${id}`),
  registerCrawlSource: (data) => request('/api/v1/crawl-sources', { method: 'POST', body: data }),
  updateCrawlSource: (id, data) => request(`/api/v1/crawl-sources/${id}`, { method: 'PATCH', body: data }),
  removeCrawlSource: (id) => request(`/api/v1/crawl-sources/${id}`, { method: 'DELETE' }),
  pauseCrawlSource: (id) => request(`/api/v1/crawl-sources/${id}/pause`, { method: 'POST' }),
  resumeCrawlSource: (id) => request(`/api/v1/crawl-sources/${id}/resume`, { method: 'POST' }),

  // -- Crawl plans (Section 4) --
  getCrawlPlanHead: () => request('/api/v1/crawl-plans'),
  listCrawlPlanVersions: (params) =>
    request('/api/v1/crawl-plans/versions?' + new URLSearchParams(params || {})),
  getCrawlPlanVersion: (id) => request(`/api/v1/crawl-plans/versions/${id}`),
  createCrawlPlanVersion: (data) => request('/api/v1/crawl-plans/versions', { method: 'POST', body: data }),
  activateCrawlPlanVersion: (id) => request(`/api/v1/crawl-plans/versions/${id}/activate`, { method: 'POST' }),
  pauseCrawlPlan: () => request('/api/v1/crawl-plans/pause', { method: 'POST' }),
  resumeCrawlPlan: () => request('/api/v1/crawl-plans/resume', { method: 'POST' }),
  runCrawlPlanNow: () => request('/api/v1/crawl-plans/run-now', { method: 'POST' }),

  // -- Crawl runs (Section 4) --
  listCrawlRuns: (params) =>
    request('/api/v1/crawl-runs?' + new URLSearchParams(params || {})),
  getCrawlRun: (id) => request(`/api/v1/crawl-runs/${id}`),

  // -- Inbox (Section 6) --
  listInbox: (params) =>
    request('/api/v1/inbox?' + new URLSearchParams(params || {})),
  getInboxJobDetail: (jobId) => request(`/api/v1/inbox/${jobId}`),
  getInboxExcludedReasons: (jobId) => request(`/api/v1/inbox/${jobId}/excluded-reasons`),
  favoriteInboxJob: (jobId) => request(`/api/v1/inbox/${jobId}/favorite`, { method: 'POST' }),
  ignoreInboxJob: (jobId) => request(`/api/v1/inbox/${jobId}/ignore`, { method: 'POST' }),
  snoozeInboxJob: (jobId, data) => request(`/api/v1/inbox/${jobId}/snooze`, { method: 'POST', body: data }),

  // -- Application workspace (Section 7) --
  getApplication: (id) => request(`/api/v1/applications/${id}`),
  prepareApplication: (id) => request(`/api/v1/applications/${id}/prepare`, { method: 'POST' }),
  getApplicationChannels: (id) => request(`/api/v1/applications/${id}/channels`),
  selectApplicationChannel: (id, channel) =>
    request(`/api/v1/applications/${id}/channel`, { method: 'POST', body: { channel } }),
  bindApplicationPackage: (id, data) =>
    request(`/api/v1/applications/${id}/package`, { method: 'POST', body: data }),
  getApplicationTimeline: (id) => request(`/api/v1/applications/${id}/timeline`),
  confirmExternalSubmission: (id, data) =>
    request(`/api/v1/applications/${id}/confirm-external-submission`, { method: 'POST', body: data }),
  changeApplicationState: (id, data) =>
    request(`/api/v1/applications/${id}/state`, { method: 'POST', body: data }),

  // -- Application packages (Section 8) --
  createPackageDraft: (applicationId, data) =>
    request(`/api/v1/applications/${applicationId}/packages`, { method: 'POST', body: data }),
  listApplicationPackages: (applicationId) =>
    request(`/api/v1/applications/${applicationId}/packages`),
  getLatestPackage: (applicationId) =>
    request(`/api/v1/applications/${applicationId}/packages/latest`),
  approvePackage: (applicationId, versionId) =>
    request(`/api/v1/applications/${applicationId}/packages/${versionId}/approve`, { method: 'POST' }),
  applyPackageEdits: (applicationId, versionId, data) =>
    request(`/api/v1/applications/${applicationId}/packages/${versionId}/edits`, { method: 'POST', body: data }),
}
