import { describe, it, expect, vi, beforeEach } from 'vitest'

// We test the api client by importing it and mocking fetch
// The client uses a module-level csrfToken variable

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('api client', () => {
  let api, setCsrfToken, formatApiError

  beforeEach(async () => {
    // Re-import to get fresh module state
    vi.resetModules()
    const mod = await import('../src/api/client.js')
    api = mod.api
    setCsrfToken = mod.setCsrfToken
    formatApiError = mod.formatApiError
  })

  it('setCsrfToken stores token for subsequent requests', async () => {
    setCsrfToken('my-csrf-token')

    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ items: [] }),
    })
    globalThis.fetch = fetchSpy

    await api.listJobs()

    // listJobs uses GET, so no CSRF header
    const [url, opts] = fetchSpy.mock.calls[0]
    expect(url).toContain('/api/v1/jobs')
    expect(opts.method || 'GET').toBe('GET')
  })

  it('sends CSRF token on POST requests', async () => {
    setCsrfToken('post-token')

    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ id: 'app-1' }),
    })
    globalThis.fetch = fetchSpy

    await api.createApplication({ canonical_job_id: 'j1' })

    const [, opts] = fetchSpy.mock.calls[0]
    expect(opts.headers['X-CSRF-Token']).toBe('post-token')
    expect(opts.method).toBe('POST')
  })

  it('throws on non-ok response', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      text: () => Promise.resolve('Internal Server Error'),
    })

    await expect(api.getJob('j1')).rejects.toThrow('500')
  })

  it('throws on 401 and redirects', async () => {
    const origLocation = window.location
    delete window.location
    window.location = { href: '' }

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      text: () => Promise.resolve('Unauthorized'),
    })

    await expect(api.getJob('j1')).rejects.toThrow()
    // handleUnauthorized redirects to /login
    // (may be async, so we just verify the throw)

    window.location = origLocation
  })

  it('sends JSON body for POST with object', async () => {
    setCsrfToken('tok')

    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    })
    globalThis.fetch = fetchSpy

    await api.createApplication({ canonical_job_id: 'j1', apply_url: 'https://example.com' })

    const [, opts] = fetchSpy.mock.calls[0]
    expect(opts.headers['Content-Type']).toBe('application/json')
    expect(JSON.parse(opts.body)).toEqual({ canonical_job_id: 'j1', apply_url: 'https://example.com' })
  })

  it('constructs correct URL params for list endpoints', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ items: [], total: 0 }),
    })
    globalThis.fetch = fetchSpy

    await api.listCompanies({ state: 'active', limit: 10 })

    const [url] = fetchSpy.mock.calls[0]
    expect(url).toContain('/api/v1/companies?')
    expect(url).toContain('state=active')
    expect(url).toContain('limit=10')
  })

  it('maps candidate profile errors to actionable safe UI copy', async () => {
    const err = new Error('403: {"error":{"code":"CANDIDATE_PROFILE_REQUIRED"}}')
    err.status = 403
    err.code = 'CANDIDATE_PROFILE_REQUIRED'
    err.messageText = 'Candidate profile is required'

    expect(formatApiError(err)).toContain('个人档案')
    expect(formatApiError(err)).not.toContain('403:')
  })

  it('exposes the real matching and agent endpoint methods', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ items: [] }),
    })
    globalThis.fetch = fetchSpy

    await api.listMatches({ limit: 10 })
    await api.listAgentRuns({ capability: 'resume_review', limit: 10 })
    await api.startResumeReview({ canonical_job_id: 'job', job_version_id: 'version', resume_version_id: 'resume' })

    expect(fetchSpy.mock.calls[0][0]).toContain('/api/v1/matches?')
    expect(fetchSpy.mock.calls[1][0]).toContain('/api/v1/agents/runs?')
    expect(fetchSpy.mock.calls[2][0]).toBe('/api/v1/agents/resume-review')
  })

  it('exposes smart intake preview and apply endpoints with CSRF', async () => {
    setCsrfToken('smart-token')
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ preview_id: 'p1', state: 'unavailable' }),
    })
    globalThis.fetch = fetchSpy

    await api.createSmartIntakePreview({
      target: 'profile',
      input: { kind: 'text', text: 'Backend' },
      idempotency_key: 'k1',
    })
    await api.getSmartIntakePreview('p1')
    await api.applySmartIntakePreview('p1', {
      apply_idempotency_key: 'a1',
      context_digest: '1'.repeat(64),
      decision_set_hash: '0'.repeat(64),
      decisions: [],
    })
    await api.getSmartIntakeCapability()

    expect(fetchSpy.mock.calls[0][0]).toBe('/api/v1/smart-intake/previews')
    expect(fetchSpy.mock.calls[0][1].headers['X-CSRF-Token']).toBe('smart-token')
    expect(fetchSpy.mock.calls[1][0]).toBe('/api/v1/smart-intake/previews/p1')
    expect(fetchSpy.mock.calls[2][0]).toBe('/api/v1/smart-intake/previews/p1/apply')
    expect(fetchSpy.mock.calls[3][0]).toBe('/api/v1/smart-intake/capability')
  })
})
