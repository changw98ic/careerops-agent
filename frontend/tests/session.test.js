import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the api client before importing session
vi.mock('../src/api/client.js', () => ({
  setCsrfToken: vi.fn(),
  api: {},
}))

import { status, user, checkSession, login, logout, clearUser, bootstrap } from '../src/stores/session.js'
import { setCsrfToken } from '../src/api/client.js'

function mockFetch(responses) {
  globalThis.fetch = vi.fn((url, opts) => {
    const key = `${(opts?.method || 'GET').toUpperCase()} ${url}`
    const handler = responses[key]
    if (!handler) return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve('not found'), json: () => Promise.resolve({}) })
    const result = typeof handler === 'function' ? handler(opts) : handler
    return Promise.resolve({ ok: true, json: () => Promise.resolve(result), ...result._meta })
  })
}

beforeEach(() => {
  clearUser()
  status.value = 'unknown'
  vi.restoreAllMocks()
  globalThis.fetch = vi.fn()
})

describe('checkSession', () => {
  it('sets authenticated when server returns authenticated user', async () => {
    mockFetch({
      'GET /api/v1/auth/session': {
        authenticated: true,
        user: { username: 'alice', candidate_id: 'c1' },
        csrf_token: 'tok123',
        expires_at: '2099-01-01',
      },
    })

    const result = await checkSession()
    expect(result).toBe('authenticated')
    expect(status.value).toBe('authenticated')
    expect(user.username).toBe('alice')
    expect(user.candidate_id).toBe('c1')
    expect(user.csrf_token).toBe('tok123')
    expect(setCsrfToken).toHaveBeenCalledWith('tok123')
  })

  it('sets anonymous when server returns unauthenticated', async () => {
    mockFetch({
      'GET /api/v1/auth/session': { authenticated: false },
    })

    const result = await checkSession()
    expect(result).toBe('anonymous')
    expect(status.value).toBe('anonymous')
    expect(user.username).toBe('')
  })

  it('sets anonymous on network error', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('network'))

    const result = await checkSession()
    expect(result).toBe('anonymous')
    expect(status.value).toBe('anonymous')
  })
})

describe('login', () => {
  it('logs in successfully and updates session state', async () => {
    mockFetch({
      'POST /api/v1/auth/preauth': { ok: true, csrf_token: 'pre-csrf' },
      'POST /api/v1/auth/login': { ok: true, csrf_token: 'sess-csrf', expires_at: '2099-01-01' },
    })

    await login('bob', 'pass1234')
    expect(status.value).toBe('authenticated')
    expect(user.username).toBe('bob')
    expect(user.csrf_token).toBe('sess-csrf')
  })

  it('throws on invalid credentials', async () => {
    mockFetch({
      'POST /api/v1/auth/preauth': { ok: true, csrf_token: 'pre-csrf' },
      'POST /api/v1/auth/login': { ok: false, error: 'Username or password is invalid' },
    })

    await expect(login('bob', 'wrong')).rejects.toThrow('Username or password is invalid')
    expect(status.value).not.toBe('authenticated')
  })
})

describe('bootstrap', () => {
  it('bootstraps successfully', async () => {
    mockFetch({
      'POST /api/v1/auth/preauth': { ok: true, csrf_token: 'pre-csrf' },
      'POST /api/v1/auth/bootstrap': { ok: true, csrf_token: 'boot-csrf', expires_at: '2099-01-01' },
    })

    await bootstrap('token-abc', 'admin', 'password123')
    expect(status.value).toBe('authenticated')
    expect(user.username).toBe('admin')
  })

  it('throws with error code on failure', async () => {
    mockFetch({
      'POST /api/v1/auth/preauth': { ok: true, csrf_token: 'pre-csrf' },
      'POST /api/v1/auth/bootstrap': { ok: false, error: 'Bootstrap closed', code: 'BOOTSTRAP_CLOSED' },
    })

    await expect(bootstrap('bad', 'admin', 'password123')).rejects.toThrow('Bootstrap closed')
  })
})

describe('logout', () => {
  it('clears user and sets anonymous', async () => {
    user.username = 'bob'
    user.csrf_token = 'tok'
    status.value = 'authenticated'

    globalThis.fetch = vi.fn().mockResolvedValue({ ok: true })

    // Mock window.location
    const origLocation = window.location
    delete window.location
    window.location = { href: '' }

    await logout()

    expect(status.value).toBe('anonymous')
    expect(user.username).toBe('')
    expect(user.csrf_token).toBe('')
    expect(window.location.href).toBe('/login')

    window.location = origLocation
  })

  it('clears state even if server request fails', async () => {
    user.username = 'bob'
    status.value = 'authenticated'

    globalThis.fetch = vi.fn().mockRejectedValue(new Error('network'))

    const origLocation = window.location
    delete window.location
    window.location = { href: '' }

    await logout()

    expect(status.value).toBe('anonymous')
    expect(user.username).toBe('')

    window.location = origLocation
  })
})

describe('clearUser', () => {
  it('resets all user fields', () => {
    user.username = 'alice'
    user.candidate_id = 'c1'
    user.csrf_token = 'tok'
    user.expires_at = '2099-01-01'

    clearUser()

    expect(user.username).toBe('')
    expect(user.candidate_id).toBe('')
    expect(user.csrf_token).toBe('')
    expect(user.expires_at).toBeNull()
  })
})
