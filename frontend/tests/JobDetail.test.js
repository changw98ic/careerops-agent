import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { ref } from 'vue'

const mockGetJob = vi.fn()
const mockCreateApplication = vi.fn()
vi.mock('../src/api/client.js', () => ({
  api: {
    getJob: (...args) => mockGetJob(...args),
    createApplication: (...args) => mockCreateApplication(...args),
  },
  setCsrfToken: vi.fn(),
}))

const mockBack = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ back: mockBack, push: vi.fn() }),
  useRoute: () => ({ params: { id: 'job-123' } }),
  RouterView: { template: '<div />' },
  RouterLink: { template: '<a><slot /></a>', props: ['to'] },
}))

vi.mock('ant-design-vue/es/message', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}))

const stubs = {
  'a-button': {
    template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
    props: ['disabled', 'loading', 'type', 'size', 'htmlType'],
    emits: ['click'],
  },
  'a-tag': { template: '<span class="a-tag"><slot /></span>', props: ['color'] },
  'a-alert': { template: '<div v-if="message" class="a-alert" />', props: ['type', 'message', 'showIcon', 'closable'] },
  'a-row': { template: '<div><slot /></div>', props: ['gutter'] },
  'a-col': { template: '<div><slot /></div>', props: ['xs', 'lg', 'sm'] },
  'a-card': { template: '<div class="a-card"><slot /></div>', props: ['title'] },
  'a-descriptions': { template: '<div><slot /></div>', props: ['column', 'size'] },
  'a-descriptions-item': { template: '<div><slot /></div>', props: ['label'] },
  'a-spin': { template: '<div class="a-spin" />', props: ['size'] },
  'a-result': { template: '<div class="a-result">{{ title }} {{ subTitle }}<slot name="extra" /></div>', props: ['status', 'title', 'subTitle'] },
  'a-empty': { template: '<div class="a-empty" />', props: ['description'] },
  'a-timeline': { template: '<div><slot /></div>' },
  'a-timeline-item': { template: '<div><slot /></div>' },
  'a-list': { template: '<div><slot name="renderItem" v-for="item in dataSource" :item="item" :key="item.id" /></div>', props: ['dataSource', 'size', 'split'] },
  'a-list-item': { template: '<div><slot /></div>' },
  'a-list-item-meta': { template: '<div><slot name="title" /><slot name="description" /></div>' },
  'arrow-left-outlined': { template: '<span />' },
}

import JobDetail from '../src/views/JobDetail.vue'

const MOCK_JOB = {
  canonical_job: {
    id: 'job-123',
    canonical_title: 'Frontend Engineer',
    company_id: 'comp-abc',
    aggregate_state: 'active',
    current_apply_url: 'https://example.com/apply',
    postings: [
      { canonical_url: 'https://example.com/posting/1', source_state: 'active', first_seen_at: '2025-01-01' },
    ],
  },
  versions: [
    {
      id: 'v1',
      parser_version: 'v2.0',
      captured_at: '2025-06-01',
      structured_data: { location: 'Remote', description: 'Build amazing UIs with Vue.js and TypeScript.' },
      content_hash: 'abc123def456ghi789',
    },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('JobDetail.vue', () => {
  function mountDetail() {
    mockGetJob.mockResolvedValue(MOCK_JOB)
    return mount(JobDetail, { global: { stubs } })
  }

  it('fetches and renders job details on mount', async () => {
    const wrapper = mountDetail()
    await flushPromises()

    expect(mockGetJob).toHaveBeenCalledWith('job-123')
    expect(wrapper.text()).toContain('Frontend Engineer')
    expect(wrapper.text()).toContain('active')
  })

  it('shows loading state initially', () => {
    mockGetJob.mockReturnValue(new Promise(() => {})) // never resolves
    const wrapper = mount(JobDetail, { global: { stubs } })
    expect(wrapper.text()).toContain('加载职位详情中...')
  })

  it('shows error state when fetch fails', async () => {
    mockGetJob.mockRejectedValue(new Error('500: Internal Server Error'))
    const wrapper = mount(JobDetail, { global: { stubs } })
    await flushPromises()

    expect(wrapper.text()).toContain('加载失败')
  })

  it('renders with fallback fields when canonical_job is null', async () => {
    mockGetJob.mockResolvedValue({ canonical_job: null, versions: [] })
    const wrapper = mount(JobDetail, { global: { stubs } })
    await flushPromises()

    // canonical_job is null, so detail itself becomes the job object
    // The template renders with fallback values (未知)
    expect(wrapper.text()).toContain('未知')
    expect(wrapper.vm.job).toBeTruthy()
  })

  it('displays version timeline', async () => {
    const wrapper = mountDetail()
    await flushPromises()

    expect(wrapper.text()).toContain('v2.0')
    expect(wrapper.text()).toContain('Remote')
  })

  it('displays job description', async () => {
    const wrapper = mountDetail()
    await flushPromises()

    expect(wrapper.text()).toContain('Build amazing UIs')
  })

  it('calls createApplication when apply button clicked', async () => {
    mockCreateApplication.mockResolvedValue({ id: 'app-1' })
    const wrapper = mountDetail()
    await flushPromises()

    const vm = wrapper.vm
    expect(vm.applied).toBe(false)

    await vm.apply()
    await flushPromises()

    expect(mockCreateApplication).toHaveBeenCalledWith({
      canonical_job_id: 'job-123',
      apply_url: 'https://example.com/apply',
    })
    expect(vm.applied).toBe(true)
  })

  it('sets error on apply failure (409 duplicate)', async () => {
    mockCreateApplication.mockRejectedValue(new Error('409: Conflict'))
    const wrapper = mountDetail()
    await flushPromises()

    await wrapper.vm.apply()
    await flushPromises()

    expect(wrapper.vm.error).toBe('该职位已投递，请勿重复操作。')
  })

  it('sets error on apply failure (401 unauthorized)', async () => {
    mockCreateApplication.mockRejectedValue(new Error('unauthorized'))
    const wrapper = mountDetail()
    await flushPromises()

    await wrapper.vm.apply()
    await flushPromises()

    expect(wrapper.vm.error).toBe('登录已过期，请重新登录。')
  })

  it('back button calls router.back()', async () => {
    const wrapper = mountDetail()
    await flushPromises()

    const backBtn = wrapper.find('button')
    await backBtn.trigger('click')
    expect(mockBack).toHaveBeenCalled()
  })
})
