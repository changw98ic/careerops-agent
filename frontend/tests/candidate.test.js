import { describe, it, expect, vi, beforeEach } from 'vitest'

// auth-rm Task 15: the candidate selector store + path-param API client.
// The console login is gone; the store owns which candidate id every
// per-candidate API call addresses (`/api/v1/candidates/{cid}/...`).

const STORAGE_KEY = 'careerops.current_candidate_id'

const TWO_CANDIDATES = {
  items: [
    { id: 'c1', display_name: '张三' },
    { id: 'c2', display_name: '李四' },
  ],
  total: 2,
}

function jsonResponse(body, status = 200) {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  }
}

function mockFetchOnce(body, status) {
  const spy = vi.fn().mockResolvedValue(jsonResponse(body, status))
  globalThis.fetch = spy
  return spy
}

beforeEach(() => {
  window.localStorage.clear()
  vi.restoreAllMocks()
})

describe('candidate store', () => {
  beforeEach(() => {
    // Fresh module per test: the store reads localStorage at import time.
    vi.resetModules()
  })

  it('load() GETs /api/v1/candidates and defaults to the first candidate (persisted)', async () => {
    const fetchSpy = mockFetchOnce(TWO_CANDIDATES)
    const store = await import('../src/stores/candidate.js')

    const list = await store.load()

    expect(fetchSpy).toHaveBeenCalledWith('/api/v1/candidates', expect.anything())
    expect(list).toHaveLength(2)
    expect(store.candidates.value).toHaveLength(2)
    expect(store.currentCandidateId.value).toBe('c1')
    expect(store.status.value).toBe('ready')
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('c1')
  })

  it('load() keeps the persisted selection when it still exists', async () => {
    window.localStorage.setItem(STORAGE_KEY, 'c2')
    mockFetchOnce(TWO_CANDIDATES)
    // Import AFTER seeding localStorage so the module initializes from it.
    const store = await import('../src/stores/candidate.js')

    await store.load()

    expect(store.currentCandidateId.value).toBe('c2')
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('c2')
  })

  it('load() falls back to the first candidate when the stored id is stale', async () => {
    window.localStorage.setItem(STORAGE_KEY, 'gone')
    mockFetchOnce(TWO_CANDIDATES)
    const store = await import('../src/stores/candidate.js')

    await store.load()

    expect(store.currentCandidateId.value).toBe('c1')
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('c1')
  })

  it('load() with zero candidates enters the empty (create-prompt) state', async () => {
    mockFetchOnce({ items: [], total: 0 })
    const store = await import('../src/stores/candidate.js')

    await store.load()

    expect(store.candidates.value).toHaveLength(0)
    expect(store.currentCandidateId.value).toBe('')
    expect(store.status.value).toBe('empty')
  })

  it('switchCandidate(id) updates the selection and persists it', async () => {
    mockFetchOnce(TWO_CANDIDATES)
    const store = await import('../src/stores/candidate.js')
    await store.load()

    store.switchCandidate('c2')

    expect(store.currentCandidateId.value).toBe('c2')
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('c2')
  })

  it('create(name) POSTs {display_name}, then reloads the list and selects the new candidate', async () => {
    const fetchSpy = vi.fn().mockImplementation((url, opts = {}) => {
      if (opts.method === 'POST') {
        return Promise.resolve(jsonResponse({ id: 'c3', display_name: '王五' }, 201))
      }
      return Promise.resolve(
        jsonResponse({
          items: [...TWO_CANDIDATES.items, { id: 'c3', display_name: '王五' }],
          total: 3,
        }),
      )
    })
    globalThis.fetch = fetchSpy
    const store = await import('../src/stores/candidate.js')

    await store.create('王五')

    const postCall = fetchSpy.mock.calls.find(([, opts]) => opts.method === 'POST')
    expect(postCall[0]).toBe('/api/v1/candidates')
    expect(JSON.parse(postCall[1].body)).toEqual({ display_name: '王五' })
    expect(store.candidates.value).toHaveLength(3)
    expect(store.currentCandidateId.value).toBe('c3')
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('c3')
    expect(store.status.value).toBe('ready')
  })
})

describe('api client — per-candidate URL prefixing', () => {
  let api, store

  beforeEach(async () => {
    vi.resetModules()
    store = await import('../src/stores/candidate.js')
    const client = await import('../src/api/client.js')
    api = client.api
    store.switchCandidate('c1')
  })

  function mockOk() {
    const spy = vi.fn().mockResolvedValue(jsonResponse({ items: [], total: 0 }))
    globalThis.fetch = spy
    return spy
  }

  it('listApplications() hits /api/v1/candidates/{cid}/applications', async () => {
    const fetchSpy = mockOk()

    await api.listApplications({ state: 'active' })

    const [url] = fetchSpy.mock.calls[0]
    expect(url).toContain('/api/v1/candidates/c1/applications?')
    expect(url).toContain('state=active')
  })

  it('switching the candidate changes the URL prefix', async () => {
    const fetchSpy = mockOk()
    store.switchCandidate('c2')

    await api.listInbox({ limit: 10 })

    expect(fetchSpy.mock.calls[0][0]).toContain('/api/v1/candidates/c2/inbox?')
  })

  it('nested per-candidate methods (matches, agents, smart-intake) get the prefix', async () => {
    const fetchSpy = mockOk()

    await api.listMatches({ limit: 10 })
    await api.startResumeReview({ canonical_job_id: 'j1' })
    await api.getSmartIntakePreview('p1')
    await api.getAgentRun('r1')
    await api.listCrawlRuns({ limit: 5 })

    const urls = fetchSpy.mock.calls.map(([u]) => u)
    expect(urls[0]).toContain('/api/v1/candidates/c1/matches?')
    expect(urls[1]).toBe('/api/v1/candidates/c1/agents/resume-review')
    expect(urls[2]).toBe('/api/v1/candidates/c1/smart-intake/previews/p1')
    expect(urls[3]).toBe('/api/v1/candidates/c1/agents/runs/r1')
    expect(urls[4]).toContain('/api/v1/candidates/c1/crawl-runs?')
  })

  it('global endpoints (jobs / companies / contacts) stay unprefixed', async () => {
    const fetchSpy = mockOk()

    await api.listJobs({ limit: 10 })
    await api.getJob('j1')
    await api.listCompanies({})
    await api.listContacts('comp1')

    const urls = fetchSpy.mock.calls.map(([u]) => u)
    expect(urls[0]).toContain('/api/v1/jobs?')
    expect(urls[1]).toBe('/api/v1/jobs/j1')
    expect(urls[2]).toContain('/api/v1/companies?')
    expect(urls[3]).toBe('/api/v1/companies/comp1/contacts')
  })
})
