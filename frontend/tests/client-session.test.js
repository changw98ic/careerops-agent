import { describe, it, expect, vi, beforeEach } from 'vitest'

// Section 14.2/14.8 — shared client surface that every Gate C/D view relies
// on: parseApiError classification and the 401 path. Since the console login
// UI was removed (task 14), a 401 no longer clears a session or redirects to
// /login — it just throws and views surface the failure.

const { parseApiError } = await import('../src/api/client.js')

beforeEach(() => {
  vi.clearAllMocks()
})

describe('parseApiError — dependency-not-ready classification (task 14.2)', () => {
  it('flags HTTP 503 as dependency-not-ready regardless of code', () => {
    const parsed = parseApiError({ status: 503, message: 'boom' })
    expect(parsed.isDependencyNotReady).toBe(true)
    expect(parsed.status).toBe(503)
  })

  it('flags DEPENDENCY_NOT_READY / UNAVAILABLE_DEPENDENCY codes', () => {
    for (const code of ['DEPENDENCY_NOT_READY', 'UNAVAILABLE_DEPENDENCY']) {
      expect(parseApiError({ status: 500, code }).isDependencyNotReady).toBe(true)
    }
  })

  it('does not flag ordinary validation / not-found errors', () => {
    expect(parseApiError({ status: 404 }).isDependencyNotReady).toBe(false)
    expect(parseApiError({ status: 422, code: 'INVALID_STATE' }).isDependencyNotReady).toBe(false)
  })

  it('carries retryable + details through from the envelope', () => {
    const parsed = parseApiError({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
      messageText: 'repo missing',
      details: { which: 'inbox_repository' },
      retryable: true,
    })
    expect(parsed.retryable).toBe(true)
    expect(parsed.details).toEqual({ which: 'inbox_repository' })
    expect(parsed.message).toBe('repo missing')
  })
})

describe('request — 401 (task 14)', () => {
  it('throws without redirecting (login page no longer exists)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      text: () => Promise.resolve('unauthorized'),
    })
    globalThis.fetch = fetchMock

    const { api } = await import('../src/api/client.js')

    // getMe was removed with the auth surface (auth-rm Task 15); listJobs is a
    // global endpoint that still exercises the shared request() path.
    await expect(api.listJobs()).rejects.toThrow('unauthorized')
  })
})
