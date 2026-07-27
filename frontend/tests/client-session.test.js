import { describe, it, expect, vi, beforeEach } from 'vitest'

// Section 14.2/14.8 — current-session API resolution + dependency-not-ready
// error parsing. These exercise the shared client surface that every Gate C/D
// view relies on: parseApiError classification and the 401 session-failure
// path that clears the session and redirects.

// Track the dynamic import the client uses to clear the session on 401.
const sessionClear = vi.fn()
const sessionStatusSet = vi.fn()
vi.mock('../src/stores/session.js', () => ({
  clearUser: (...args) => sessionClear(...args),
  status: { value: 'unknown', _set: (v) => sessionStatusSet(v) },
}))

// Stub fetch so we can drive request() through realistic responses. We do NOT
// import client.js until after the mock is registered; the module under test
// reads the fetch global lazily.
const originalLocation = window.location
beforeEach(() => {
  vi.clearAllMocks()
  delete window.location
  // jsdom/happy-dom throw on assigning window.location; use a mutable stub.
  Object.defineProperty(window, 'location', {
    value: { href: '', ...originalLocation },
    writable: true,
    configurable: true,
  })
})

const { parseApiError, setCsrfToken } = await import('../src/api/client.js')

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

describe('request — 401 clears the session and redirects (task 14.2)', () => {
  it('treats 401 as a session failure: clears user and bounces to /login', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      text: () => Promise.resolve('unauthorized'),
    })
    globalThis.fetch = fetchMock

    const { api } = await import('../src/api/client.js')
    setCsrfToken('csrf-1')

    // drive any api method; the 401 handler runs before .json() parsing.
    await expect(api.getMe()).rejects.toThrow('unauthorized')

    // give the dynamic session import a tick to resolve
    await Promise.resolve()
    await Promise.resolve()

    expect(sessionClear).toHaveBeenCalled()
    expect(window.location.href).toBe('/login')
  })
})
