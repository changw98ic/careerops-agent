async function request(path, options = {}) {
  const headers = { ...options.headers }
  const isForm = options.body instanceof FormData
  if (options.body && typeof options.body === 'object' && !isForm) {
    headers['Content-Type'] = 'application/json'
    options.body = JSON.stringify(options.body)
  }
  // FormData: let the browser set the multipart boundary; do not add Content-Type.
  const res = await fetch(path, { ...options, headers, credentials: 'same-origin' })
  // 401 has no login page to bounce to anymore; views surface the failure.
  if (res.status === 401) {
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
    isCandidateProfileRequired: code === 'CANDIDATE_PROFILE_REQUIRED',
  }
}

/**
 * Convert an API error into copy that is safe and useful in the UI.
 * Backend envelopes may contain trace-oriented details; views should never
 * render the raw `status: {json}` string directly.
 */
export function formatApiError(err, fallback = '操作失败，请稍后重试。') {
  const info = parseApiError(err)
  if (info.isCandidateProfileRequired) return '请先完成职业画像配置，可从左侧「个人档案」开始。'
  if (info.code === 'DENIED_POLICY') return '当前能力未启用，暂时无法访问。'
  if (info.code === 'SMART_INTAKE_DISABLED') return '智能预填尚未启用，手工表单仍然可用。'
  if (info.code === 'SMART_PREVIEW_IN_PROGRESS') return '已有相同预览正在生成，请稍候或刷新。'
  if (info.code === 'SMART_PREVIEW_NOT_FOUND') return '智能预览不存在或已被清理，请重新生成。'
  if (info.code === 'SMART_PREVIEW_EXPIRED') return '智能预览已过期，请重新生成。'
  if (info.code === 'STALE_SMART_INTAKE_PREVIEW') return '表单或上下文已变化，请重新生成预览。'
  if (info.code === 'IDEMPOTENCY_KEY_REUSED') return '请求内容或审查选择已变化，请重新生成当前预览。'
  if (info.code === 'RATE_LIMITED') return '智能预填请求过于频繁，请稍后再试。'
  if (info.code === 'AGENT_RUN_NOT_FOUND') return '指定的运行记录不存在或已被清理。'
  if (info.code === 'AGENT_INVALID_STATE') return '运行状态不允许此操作，请刷新后重试。'
  if (info.code === 'AGENT_CAPABILITY_BLOCKED') return '当前能力未启用，无法执行此操作。'
  if (info.code === 'AGENT_RETRY_LIMIT') return '已达到最大重试次数，请稍后再试。'
  if (info.isDependencyNotReady) return fallback
  if (!info.message || /^\d+\s*:\s*\{/.test(info.message)) return fallback
  return info.message
}

// Both the backend capability flag and this explicit build-time flag are
// required before the AI entry point becomes interactive. The safe default is
// false, so a disabled backend never presents a misleading generation CTA.
export const smartIntakeUiEnabled = import.meta.env.VITE_SMART_INTAKE_ENABLED === 'true'

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

  // -- Crawl permissions (Phase 6) --
  listCrawlPermissions: (params) =>
    request('/api/v1/crawl-permissions?' + new URLSearchParams(params || {})),
  getCrawlPermission: (id) => request(`/api/v1/crawl-permissions/${id}`),
  grantCrawlPermission: (id) =>
    request(`/api/v1/crawl-permissions/${id}/grant`, { method: 'POST', body: {} }),
  denyCrawlPermission: (id) =>
    request(`/api/v1/crawl-permissions/${id}/deny`, { method: 'POST', body: {} }),
  revokeCrawlPermission: (id) =>
    request(`/api/v1/crawl-permissions/${id}/revoke`, { method: 'POST', body: {} }),
  openCrawlLoginSession: (id) =>
    request(`/api/v1/crawl-permissions/${id}/open-login`, { method: 'POST' }),
  requestCrawlPermission: (sourceId, data) =>
    request(`/api/v1/crawl-sources/${sourceId}/permissions`, { method: 'POST', body: data || {} }),

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

  // -- Trusted contacts + submission preview (Section 9) --
  // Preview performs NO provider side effects; it returns the exact sendable
  // representation + validation errors so the user can review before final
  // confirmation (the actual send is Section 10).
  listRecruitingContacts: (applicationId) =>
    request(`/api/v1/applications/${applicationId}/recruiting-contacts`),
  previewSubmission: (applicationId, data) =>
    request(`/api/v1/applications/${applicationId}/submission-preview`, { method: 'POST', body: data }),

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

  // -- System-managed send (Section 10) --
  // CareerOps final confirmation -> durable pending intent. The provider is
  // NOT called synchronously; an isolated worker drives execution. The UI must
  // never display a queued/pending send as "sent" (task 10.11).
  confirmSystemSend: (applicationId, data) =>
    request(`/api/v1/applications/${applicationId}/system-send`, { method: 'POST', body: data }),
  getSystemSendStatus: (applicationId, intentId) =>
    request(`/api/v1/applications/${applicationId}/system-send/${intentId}`),
  escalateSystemSendReconciliation: (applicationId, intentId) =>
    request(`/api/v1/applications/${applicationId}/system-send/${intentId}/reconcile`, { method: 'POST' }),

  // -- Gmail read sync (Section 11) --
  // The GMAIL_READ capability stays DENIED at the contract layer (Iron Rule 7);
  // every method below will surface a 403 DENIED_POLICY until a separate
  // qualification change releases the capability. The view renders that 403 as
  // the "sync unavailable / not connected" stale state (task 11.8) rather than
  // presenting old data as current.
  getMailAccountStatus: () => request('/api/v1/mail/account'),
  revokeMailAccount: () =>
    request('/api/v1/mail/account/revoke', { method: 'POST' }),
  syncMailNow: (params) =>
    request('/api/v1/mail/sync-now?' + new URLSearchParams(params || {}), { method: 'POST' }),
  listMailSyncHistory: (params) =>
    request('/api/v1/mail/sync-history?' + new URLSearchParams(params || {})),
  listMailThreads: (params) =>
    request('/api/v1/mail/threads?' + new URLSearchParams(params || {})),
  listMailMessages: (threadId, params) =>
    request(`/api/v1/mail/threads/${threadId}/messages?` + new URLSearchParams(params || {})),
  listMailUnresolvedLinks: (params) =>
    request('/api/v1/mail/unresolved-links?' + new URLSearchParams(params || {})),
  confirmMailLink: (linkId, data) =>
    request(`/api/v1/mail/unresolved-links/${linkId}/confirm`, { method: 'POST', body: data }),

  // -- Reply drafts + follow-up (Section 13) --
  // Drafts are review-only; only an approved low-risk reply may be sent via
  // the reused Section 10 chain. High-risk categories are permanently denied
  // system send; auto-send is permanently denied (Iron Rule 7).
  listReplyDrafts: (params) =>
    request('/api/v1/reply/drafts?' + new URLSearchParams(params || {})),
  getReplyDraft: (draftId) => request(`/api/v1/reply/drafts/${draftId}`),
  createReplyDraft: (data) =>
    request('/api/v1/reply/drafts', { method: 'POST', body: data }),
  editReplyDraft: (draftId, data) =>
    request(`/api/v1/reply/drafts/${draftId}/edit`, { method: 'POST', body: data }),
  approveReplyDraft: (draftId) =>
    request(`/api/v1/reply/drafts/${draftId}/approve`, { method: 'POST' }),
  rejectReplyDraft: (draftId) =>
    request(`/api/v1/reply/drafts/${draftId}/reject`, { method: 'POST' }),
  sendReplyDraft: (draftId, data) =>
    request(`/api/v1/reply/drafts/${draftId}/send`, { method: 'POST', body: data }),
  getReplyDraftSendStatus: (draftId) =>
    request(`/api/v1/reply/drafts/${draftId}/send-status`),
  getFollowUpRules: () => request('/api/v1/reply/follow-up-rules'),
  listApplicationFollowUps: (applicationId) =>
    request(`/api/v1/applications/${applicationId}/follow-ups`),
  scheduleFollowUp: (applicationId, data) =>
    request(`/api/v1/applications/${applicationId}/follow-ups`, {
      method: 'POST',
      body: data,
    }),
  snoozeFollowUp: (reminderId, data) =>
    request(`/api/v1/follow-ups/${reminderId}/snooze`, { method: 'POST', body: data }),
  rescheduleFollowUp: (reminderId, data) =>
    request(`/api/v1/follow-ups/${reminderId}/reschedule`, {
      method: 'POST',
      body: data,
    }),
  cancelFollowUp: (reminderId, data) =>
    request(`/api/v1/follow-ups/${reminderId}/cancel`, { method: 'POST', body: data }),
  completeFollowUp: (reminderId) =>
    request(`/api/v1/follow-ups/${reminderId}/complete`, { method: 'POST' }),
  // -- Matching + review-only agent workbench --
  listMatches: (params) =>
    request('/api/v1/matches?' + new URLSearchParams(params || {})),
  runMatch: (data) => request('/api/v1/matches/run', { method: 'POST', body: data }),
  startResumeReview: (data) =>
    request('/api/v1/agents/resume-review', { method: 'POST', body: data }),
  startInterviewPreparation: (data) =>
    request('/api/v1/agents/interview-preparation', { method: 'POST', body: data }),
  listAgentRuns: (params) =>
    request('/api/v1/agents/runs?' + new URLSearchParams(params || {})),
  getAgentRun: (runId) => request(`/api/v1/agents/runs/${runId}`),
  getAgentStages: (runId, params) =>
    request(`/api/v1/agents/runs/${runId}/stages?` + new URLSearchParams(params || {})),
  retryAgentRun: (runId, data) =>
    request(`/api/v1/agents/runs/${runId}/retry`, { method: 'POST', body: data || {} }),
  stopAgentRun: (runId, data) =>
    request(`/api/v1/agents/runs/${runId}/stop`, { method: 'POST', body: data || {} }),
  reviewAgentRun: (runId, data) =>
    request(`/api/v1/agents/runs/${runId}/review`, { method: 'POST', body: data }),
  listAgentReviews: (runId) => request(`/api/v1/agents/runs/${runId}/reviews`),

  // -- Smart form intake (review-only draft proposals) --
  getSmartIntakeCapability: () => request('/api/v1/smart-intake/capability'),
  createSmartIntakePreview: (data) =>
    request('/api/v1/smart-intake/previews', { method: 'POST', body: data }),
  getSmartIntakePreview: (previewId) =>
    request(`/api/v1/smart-intake/previews/${previewId}`),
  applySmartIntakePreview: (previewId, data) =>
    request(`/api/v1/smart-intake/previews/${previewId}/apply`, {
      method: 'POST',
      body: data,
    }),

  // -- Agent console: action queue (Section 14) --
  getActionQueue: (params) =>
    request('/api/v1/agent-console/actions?' + new URLSearchParams(params || {})),
  acceptAction: (actionKey, data) =>
    request(`/api/v1/agent-console/actions/${actionKey}/accept`, {
      method: 'POST',
      body: data || {},
    }),
  snoozeAction: (actionKey, data) =>
    request(`/api/v1/agent-console/actions/${actionKey}/snooze`, {
      method: 'POST',
      body: data || {},
    }),
  dismissAction: (actionKey, data) =>
    request(`/api/v1/agent-console/actions/${actionKey}/dismiss`, {
      method: 'POST',
      body: data || {},
    }),
  completeAction: (actionKey, data) =>
    request(`/api/v1/agent-console/actions/${actionKey}/complete`, {
      method: 'POST',
      body: data || {},
    }),

  // -- Agent console: shared context --
  createContext: (data) =>
    request('/api/v1/agent-console/context', { method: 'POST', body: data }),
  getContext: (contextId) =>
    request(`/api/v1/agent-console/context/${contextId}`),

  // -- Agent console: capability and preflight --
  getCapability: () => request('/api/v1/agent-console/capability'),
  createPreflight: (data) =>
    request('/api/v1/agent-console/preflight', { method: 'POST', body: data }),
}
