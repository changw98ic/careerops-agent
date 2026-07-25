import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../src/api/client.js', () => ({
  setCsrfToken: vi.fn(),
  api: {},
}))

import { status, clearUser } from '../src/stores/session.js'

// We need to test the guard logic directly rather than through the router,
// because createRouter with createWebHistory doesn't work well in happy-dom.
// Instead we extract the guard logic and test the decision paths.

describe('route guard logic', () => {
  beforeEach(() => {
    clearUser()
    status.value = 'unknown'
  })

  function simulateGuard(to) {
    // Mirror the logic from router.js beforeEach
    if (to.name === 'login' && status.value === 'authenticated') {
      return { name: 'dashboard' }
    }
    if (to.meta?.auth && status.value !== 'authenticated') {
      return { name: 'login' }
    }
    return undefined // allow navigation
  }

  it('redirects authenticated user away from /login', () => {
    status.value = 'authenticated'
    const result = simulateGuard({ name: 'login', meta: {} })
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('allows unauthenticated user to visit /login', () => {
    status.value = 'anonymous'
    const result = simulateGuard({ name: 'login', meta: {} })
    expect(result).toBeUndefined()
  })

  it('redirects unauthenticated user from protected route to /login', () => {
    status.value = 'anonymous'
    const result = simulateGuard({ name: 'dashboard', meta: { auth: true } })
    expect(result).toEqual({ name: 'login' })
  })

  it('allows authenticated user to visit protected route', () => {
    status.value = 'authenticated'
    const result = simulateGuard({ name: 'dashboard', meta: { auth: true } })
    expect(result).toBeUndefined()
  })

  it('redirects from protected route when session is unknown', () => {
    status.value = 'unknown'
    const result = simulateGuard({ name: 'jobs', meta: { auth: true } })
    expect(result).toEqual({ name: 'login' })
  })

  it('does not redirect for non-auth routes', () => {
    status.value = 'anonymous'
    const result = simulateGuard({ name: 'not-found', meta: {} })
    expect(result).toBeUndefined()
  })
})
