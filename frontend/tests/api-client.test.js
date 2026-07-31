import { describe, it, expect, vi, beforeEach } from 'vitest'

// We test the api client by importing it and mocking fetch.
// The CSRF-token machinery was removed with the console login UI (task 14),
// so state-changing requests no longer attach an X-CSRF-Token header.

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('api client', () => {
  let api, formatApiError

  beforeEach(async () => {
    // Re-import to get fresh module state
    vi.resetModules()
    const mod = await import('../src/api/client.js')
    api = mod.api
    formatApiError = mod.formatApiError
  })

  it('lists jobs via GET', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ items: [] }),
    })
    globalThis.fetch = fetchSpy

    await api.listJobs()

    const [url, opts] = fetchSpy.mock.calls[0]
    expect(url).toContain('/api/v1/jobs')
    expect(opts.method || 'GET').toBe('GET')
  })

  it('throws on non-ok response', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      text: () => Promise.resolve('Internal Server Error'),
    })

    await expect(api.getJob('j1')).rejects.toThrow('500')
  })

  it('throws on 401 without redirecting', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      text: () => Promise.resolve('Unauthorized'),
    })

    await expect(api.getJob('j1')).rejects.toThrow()
  })

  it('sends JSON body for POST with object', async () => {
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

  it('exposes smart intake preview and apply endpoints', async () => {
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
    expect(fetchSpy.mock.calls[1][0]).toBe('/api/v1/smart-intake/previews/p1')
    expect(fetchSpy.mock.calls[2][0]).toBe('/api/v1/smart-intake/previews/p1/apply')
    expect(fetchSpy.mock.calls[3][0]).toBe('/api/v1/smart-intake/capability')
  })
})
