import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// Section 14.3 — job-detail action path (recommendation -> favorite -> prepare
// -> workspace). Verifies the inbox favorite captures the application_id and
// the CTA routes the user into the application workspace, plus the 14.2/14.6
// dependency-not-ready state.

const mockGetInboxJobDetail = vi.fn()
const mockGetExcludedReasons = vi.fn()
const mockFavorite = vi.fn()

vi.mock('../src/api/client.js', () => ({
  api: {
    getInboxJobDetail: (...a) => mockGetInboxJobDetail(...a),
    getInboxExcludedReasons: (...a) => mockGetExcludedReasons(...a),
    favoriteInboxJob: (...a) => mockFavorite(...a),
  },
  parseApiError: (err) => ({
    status: err?.status,
    code: err?.code,
    message: err?.messageText || err?.message || '',
    isDependencyNotReady:
      err?.status === 503 || err?.code === 'DEPENDENCY_NOT_READY',
  }),
}))

const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush }),
  useRoute: () => ({ params: { id: 'job-1' } }),
  RouterLink: { template: '<a><slot /></a>', props: ['to'] },
}))

vi.mock('ant-design-vue', () => ({
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

import InboxDetail from '../src/views/InboxDetail.vue'

const stubs = {
  'a-button': {
    template:
      '<button :disabled="disabled" @click="$emit(\'click\')"><slot /><slot name="icon" /></button>',
    props: ['disabled', 'loading', 'type', 'size', 'ghost'],
    emits: ['click'],
  },
  'a-tag': { template: '<span class="a-tag"><slot /></span>', props: ['color'] },
  'a-alert': { template: '<div v-if="message" class="a-alert">{{ message }}</div>', props: ['type', 'message', 'showIcon', 'closable'] },
  'a-space': { template: '<span class="a-space"><slot /></span>', props: ['wrap'] },
  'a-row': { template: '<div><slot /></div>', props: ['gutter'] },
  'a-col': { template: '<div><slot /></div>', props: ['xs', 'lg'] },
  'a-card': { template: '<div class="a-card"><slot /></div>', props: ['title'] },
  'a-descriptions': { template: '<div><slot /></div>', props: ['column', 'size'] },
  'a-descriptions-item': { template: '<div><slot /></div>', props: ['label'] },
  'a-spin': { template: '<div class="a-spin" />', props: ['size', 'spinning'] },
  'a-result': { template: '<div class="a-result">{{ title }}<slot name="extra" /></div>', props: ['status', 'title', 'subTitle'] },
  'a-empty': { template: '<div class="a-empty" />', props: ['description'] },
  'a-table': { template: '<div class="a-table" />', props: ['columns', 'dataSource'] },
  'arrow-left-outlined': { template: '<span />' },
  'arrow-right-outlined': { template: '<span />' },
}

const RECOMMENDED = {
  canonical_job_id: 'job-1',
  title: 'Staff Engineer',
  verdict: 'recommended',
  application_state: 'none',
  application_id: null,
  description: '<p>job body</p>',
  match_results: [],
}

beforeEach(() => {
  vi.clearAllMocks()
  mockGetExcludedReasons.mockResolvedValue({ reasons: [] })
})

describe('InboxDetail — action path (task 14.3)', () => {
  it('has no workspace CTA before favoriting', async () => {
    mockGetInboxJobDetail.mockResolvedValue({ ...RECOMMENDED })
    const w = mount(InboxDetail, { global: { stubs } })
    await flushPromises()
    expect(w.text()).not.toContain('准备申请材料')
  })

  it('captures application_id on favorite and routes into the workspace', async () => {
    mockGetInboxJobDetail.mockResolvedValue({ ...RECOMMENDED })
    mockFavorite.mockResolvedValue({
      application_id: 'app-9',
      state: 'favorited',
      canonical_job_id: 'job-1',
    })
    const w = mount(InboxDetail, { global: { stubs } })
    await flushPromises()

    await w.vm.onFavorite()
    await flushPromises()

    expect(mockFavorite).toHaveBeenCalledWith('job-1')
    expect(w.vm.job.application_id).toBe('app-9')
    expect(w.vm.job.application_state).toBe('favorited')

    // CTA now present and routes to the application workspace
    expect(w.text()).toContain('准备申请材料')
    w.vm.goToWorkspace()
    expect(mockPush).toHaveBeenCalledWith('/applications/app-9')
  })

  it('does not navigate when application_id is missing', async () => {
    mockGetInboxJobDetail.mockResolvedValue({ ...RECOMMENDED })
    const w = mount(InboxDetail, { global: { stubs } })
    await flushPromises()
    // Force the favorited state without an application id (server returned none)
    w.vm.job = { ...RECOMMENDED, application_state: 'favorited', application_id: '' }
    await w.vm.$nextTick()
    w.vm.goToWorkspace()
    expect(mockPush).not.toHaveBeenCalled()
    // The CTA explains the record is not ready instead of silently doing nothing
    expect(w.text()).toContain('申请记录尚未就绪')
  })
})

describe('InboxDetail — dependency-not-ready state (task 14.2/14.6)', () => {
  it('surfaces a 503 as an actionable dependency-not-ready message', async () => {
    mockGetInboxJobDetail.mockRejectedValue({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
      messageText: 'inbox repository not available',
    })
    const w = mount(InboxDetail, { global: { stubs } })
    await flushPromises()

    expect(w.vm.error).toContain('未就绪')
    expect(w.text()).toContain('加载失败')
  })
})
