import { describe, it, expect, vi } from 'vitest'

// Avoid triggering createWebHistory by importing only the route table. The
// named `routes` export is additive (task 14.1/14.8) and lets us assert guard
// coverage without a real browser history.
vi.mock('vue-router', () => ({
  createRouter: () => ({ beforeEach: () => () => {} }),
  createWebHistory: () => ({}),
}))

import { routes } from '../src/router.js'

// Section 14.1 — every workspace route the unified frontend exposes must be
// behind the authentication guard. Names mirror the router registrations.
const SECTION_14_PROTECTED = [
  'profile',
  'resumes',
  'evidence',
  'crawl-plans',
  'crawl-runs',
  'crawl-run-detail',
  'inbox',
  'inbox-detail',
  'application-workspace',
  'mail-follow-up',
  'reply-queue',
  'applications',
  'jobs',
  'job-detail',
  'dashboard',
]

describe('Section 14.1 — authenticated route coverage', () => {
  const byName = new Map(routes.map((r) => [r.name, r]))

  it('registers every required workspace route', () => {
    for (const name of SECTION_14_PROTECTED) {
      expect(byName.has(name), `route ${name} should be registered`).toBe(true)
    }
  })

  it('marks every Section 14 route with meta.auth = true', () => {
    for (const name of SECTION_14_PROTECTED) {
      const route = byName.get(name)
      expect(route.meta?.auth, `${name} must require auth`).toBe(true)
    }
  })

  it('no longer registers login or bootstrap routes (auth UI removed)', () => {
    expect(byName.has('login')).toBe(false)
    expect(byName.has('bootstrap')).toBe(false)
  })
})
