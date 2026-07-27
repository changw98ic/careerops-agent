import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// Section 14.6/14.8 — source + plan CRUD dependency-not-ready states. The
// crawl-plan workspace must surface a missing repository (503) as an
// actionable "dependency not ready" state on both initial load and on
// individual source/plan actions, never as a silent failure.

const mockListSources = vi.fn()
const mockGetHead = vi.fn()
const mockListVersions = vi.fn()
const mockPauseSource = vi.fn()
const mockResumeSource = vi.fn()

vi.mock('../src/api/client.js', () => ({
  api: {
    listCrawlSources: (...a) => mockListSources(...a),
    getCrawlPlanHead: (...a) => mockGetHead(...a),
    listCrawlPlanVersions: (...a) => mockListVersions(...a),
    pauseCrawlSource: (...a) => mockPauseSource(...a),
    resumeCrawlSource: (...a) => mockResumeSource(...a),
    resumeCrawlPlan: vi.fn().mockResolvedValue({ state: 'active' }),
    pauseCrawlPlan: vi.fn().mockResolvedValue({ state: 'paused' }),
    runCrawlPlanNow: vi.fn().mockResolvedValue({ run_id: 'r1' }),
  },
  parseApiError: (err) => ({
    status: err?.status,
    code: err?.code,
    message: err?.messageText || err?.message || '',
    isDependencyNotReady:
      err?.status === 503 || err?.code === 'DEPENDENCY_NOT_READY',
  }),
  setCsrfToken: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useRoute: () => ({ params: {} }),
  RouterLink: { template: '<a><slot /></a>', props: ['to'] },
}))
vi.mock('ant-design-vue', () => ({
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

import CrawlPlans from '../src/views/CrawlPlans.vue'

const stubs = {
  'a-button': {
    template:
      '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
    props: ['disabled', 'loading', 'type', 'size', 'danger', 'ghost'],
    emits: ['click'],
  },
  'a-tag': { template: '<span class="a-tag"><slot /></span>', props: ['color'] },
  'a-alert': {
    template: '<div v-if="message || description" class="a-alert">{{ message }} {{ description }}</div>',
    props: ['type', 'message', 'description', 'showIcon', 'closable'],
  },
  'a-space': { template: '<span><slot /></span>' },
  'a-row': { template: '<div><slot /></div>', props: ['gutter'] },
  'a-col': { template: '<div><slot /></div>', props: ['xs', 'lg', 'span'] },
  'a-card': { template: '<div class="a-card"><slot /></div>', props: ['title'] },
  'a-spin': { template: '<div class="a-spin" />', props: ['size', 'spinning'] },
  'a-empty': { template: '<div class="a-empty">{{ description }}</div>', props: ['description'] },
  'a-list': {
    template: '<div><slot name="renderItem" v-for="item in dataSource" :item="item" :key="item.id" /></div>',
    props: ['dataSource', 'size', 'split'],
  },
  'a-list-item': { template: '<div><slot /></div>' },
  'a-list-item-meta': {
    template: '<div><slot name="title" /><slot name="description" /><slot /></div>',
  },
  'a-descriptions': { template: '<div><slot /></div>', props: ['column', 'size', 'bordered'] },
  'a-descriptions-item': { template: '<div><slot /></div>', props: ['label'] },
  'a-timeline': { template: '<div><slot /></div>' },
  'a-timeline-item': { template: '<div><slot /></div>' },
  'a-modal': { template: '<div v-if="open"><slot /></div>', props: ['open', 'title', 'confirmLoading'] },
  'a-form': { template: '<div><slot /></div>' },
  'a-form-item': { template: '<div><slot /></div>', props: ['label'] },
  'a-input': { template: '<input />', props: ['value'] },
  'a-input-number': { template: '<input />', props: ['value'] },
  'a-textarea': { template: '<textarea />', props: ['value', 'rows'] },
  'a-select': { template: '<select />', props: ['value'] },
  'a-select-option': { template: '<option><slot /></option>', props: ['value'] },
  'a-switch': { template: '<button />', props: ['checked'] },
  'a-divider': { template: '<hr />' },
  'a-result': { template: '<div>{{ title }}</div>', props: ['status', 'title', 'subTitle'] },
}

const HEAD_ACTIVE = { active: { version_number: 1, state: 'active' } }

beforeEach(() => {
  vi.clearAllMocks()
  mockGetHead.mockResolvedValue(HEAD_ACTIVE)
  mockListVersions.mockResolvedValue({ items: [] })
})

describe('CrawlPlans — source/plan CRUD dependency states (task 14.6)', () => {
  it('surfaces 503 on initial load as a dependency-not-ready alert', async () => {
    mockListSources.mockRejectedValue({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
      messageText: 'crawl source repository not available',
    })
    const w = mount(CrawlPlans, { global: { stubs } })
    await flushPromises()

    expect(w.vm.unavailable).toBe(true)
    expect(w.text()).toContain('依赖未就绪')
  })

  it('renders an empty state when there are no sources and no error', async () => {
    mockListSources.mockResolvedValue({ items: [] })
    const w = mount(CrawlPlans, { global: { stubs } })
    await flushPromises()

    expect(w.vm.unavailable).toBe(false)
    expect(w.vm.sources).toEqual([])
  })

  it('propagates a 503 from a source pause action to the unavailable state', async () => {
    mockListSources.mockResolvedValue({
      items: [{ id: 'src-1', name: 'co', enabled: true, state: 'active' }],
    })
    mockPauseSource.mockRejectedValue({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
      messageText: 'repo gone',
    })
    const w = mount(CrawlPlans, { global: { stubs } })
    await flushPromises()

    expect(w.vm.unavailable).toBe(false)
    await w.vm.pauseSource('src-1')
    await flushPromises()

    expect(mockPauseSource).toHaveBeenCalledWith('src-1')
    expect(w.vm.unavailable).toBe(true)
  })

  it('recovers (clears unavailable) on a successful reload', async () => {
    mockListSources.mockRejectedValueOnce({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
    })
    mockListSources.mockResolvedValueOnce({ items: [] })
    const w = mount(CrawlPlans, { global: { stubs } })
    await flushPromises()
    expect(w.vm.unavailable).toBe(true)

    await w.vm.loadAll()
    await flushPromises()
    expect(w.vm.unavailable).toBe(false)
  })
})
