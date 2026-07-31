import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const mockListJobs = vi.fn()
vi.mock('../src/api/client.js', () => ({
  api: {
    listJobs: (...args) => mockListJobs(...args),
  },
  formatApiError: (err, fallback) => err?.message || fallback,
}))

const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush }),
  useRoute: () => ({ query: {} }),
  RouterView: { template: '<div />' },
  RouterLink: { template: '<a><slot /></a>', props: ['to'] },
}))

const stubs = {
  'a-card': { template: '<div class="a-card"><slot /></div>' },
  'a-table': {
    template: `
      <div class="a-table">
        <template v-if="dataSource && dataSource.length">
          <template v-for="r in dataSource" :key="r[rowKey || 'id']">
            <slot name="bodyCell" v-for="col in columns" :column="col" :record="r" />
          </template>
        </template>
        <div v-else class="table-empty"><slot name="emptyText" /></div>
      </div>
    `,
    props: ['columns', 'dataSource', 'loading', 'pagination', 'rowKey', 'scroll', 'customRow'],
  },
  'a-tag': { template: '<span class="a-tag"><slot /></span>', props: ['color'] },
  'a-alert': { template: '<div v-if="message" class="a-alert" />', props: ['type', 'message', 'showIcon', 'closable'] },
  'a-button': {
    template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
    props: ['disabled', 'loading', 'type', 'size'],
    emits: ['click'],
  },
  'a-spin': { template: '<div><slot /></div>', props: ['spinning', 'tip'] },
  'a-empty': { template: '<div class="a-empty">{{ description }}</div>', props: ['description'] },
  'a-input-search': {
    template: '<input @input="$emit(\'search\', $event.target.value)" />',
    props: ['value', 'allowClear', 'placeholder', 'style'],
    emits: ['search', 'change'],
  },
  'a-select': {
    template: '<select :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\')"><slot /></select>',
    props: ['modelValue', 'value', 'style'],
    emits: ['update:modelValue', 'change'],
  },
  'a-select-option': { template: '<option :value="value"><slot /></option>', props: ['value'] },
  'search-outlined': { template: '<span />' },
  'filter-outlined': { template: '<span />' },
  'reload-outlined': { template: '<span />' },
}

import Jobs from '../src/views/Jobs.vue'

const MOCK_JOBS_PAGE = {
  items: [
    { id: 'j1', canonical_title: 'Frontend Dev', company_id: 'comp-11111111-aaaa', aggregate_state: 'active' },
    { id: 'j2', canonical_title: 'Backend Dev', company_id: 'comp-22222222-bbbb', aggregate_state: 'closed' },
  ],
  total: 2,
  next_cursor: null,
}

const MOCK_JOBS_PAGINATED = {
  items: [
    { id: 'j1', canonical_title: 'Frontend Dev', company_id: 'comp-11111111-aaaa', aggregate_state: 'active' },
  ],
  total: 50,
  next_cursor: 'cursor-abc',
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('Jobs.vue', () => {
  function mountJobs() {
    mockListJobs.mockResolvedValue(MOCK_JOBS_PAGE)
    return mount(Jobs, { global: { stubs } })
  }

  it('renders the page header', async () => {
    const wrapper = mountJobs()
    await flushPromises()
    expect(wrapper.text()).toContain('职位管理')
    expect(wrapper.text()).toContain('2 条记录')
  })

  it('fetches jobs on mount', async () => {
    mountJobs()
    await flushPromises()
    expect(mockListJobs).toHaveBeenCalledWith({ limit: 50 })
  })

  it('displays job rows after fetch', async () => {
    const wrapper = mountJobs()
    await flushPromises()
    expect(wrapper.text()).toContain('Frontend Dev')
    expect(wrapper.text()).toContain('Backend Dev')
  })

  it('shows empty state when no jobs', async () => {
    mockListJobs.mockResolvedValue({ items: [], total: 0, next_cursor: null })
    const wrapper = mount(Jobs, { global: { stubs } })
    await flushPromises()
    expect(wrapper.text()).toContain('暂无职位数据')
  })

  it('shows error message on fetch failure', async () => {
    mockListJobs.mockRejectedValue(new Error('Server error'))
    const wrapper = mount(Jobs, { global: { stubs } })
    await flushPromises()
    expect(wrapper.vm.error).toBe('Server error')
  })

  it('shows "load more" button when hasMore is true', async () => {
    mockListJobs.mockResolvedValue(MOCK_JOBS_PAGINATED)
    const wrapper = mount(Jobs, { global: { stubs } })
    await flushPromises()
    expect(wrapper.text()).toContain('加载更多')
  })

  it('loads more jobs and appends to list', async () => {
    mockListJobs
      .mockResolvedValueOnce(MOCK_JOBS_PAGINATED)
      .mockResolvedValueOnce({
        items: [{ id: 'j2', canonical_title: 'Backend Dev', company_id: 'comp-22222222-bbbb', aggregate_state: 'closed' }],
        total: 50,
        next_cursor: null,
      })

    const wrapper = mount(Jobs, { global: { stubs } })
    await flushPromises()

    await wrapper.vm.loadMore()
    await flushPromises()

    expect(wrapper.vm.jobs).toHaveLength(2)
    expect(wrapper.vm.hasMore).toBe(false)
  })

  it('passes search query to API on search', async () => {
    const wrapper = mountJobs()
    await flushPromises()

    wrapper.vm.searchQuery = 'engineer'
    wrapper.vm.resetAndFetch()
    await flushPromises()

    expect(mockListJobs).toHaveBeenCalledWith(expect.objectContaining({ q: 'engineer' }))
  })

  it('passes state filter to API', async () => {
    const wrapper = mountJobs()
    await flushPromises()

    wrapper.vm.stateFilter = 'active'
    wrapper.vm.resetAndFetch()
    await flushPromises()

    expect(mockListJobs).toHaveBeenCalledWith(expect.objectContaining({ state: 'active' }))
  })

  it('stateColor returns correct colors', () => {
    const wrapper = mountJobs()
    expect(wrapper.vm.stateColor('active')).toBe('green')
    expect(wrapper.vm.stateColor('closed')).toBe('default')
    expect(wrapper.vm.stateColor('unknown')).toBe('gold')
  })

  it('stateLabel returns correct labels', () => {
    const wrapper = mountJobs()
    expect(wrapper.vm.stateLabel('active')).toBe('进行中')
    expect(wrapper.vm.stateLabel('closed')).toBe('已关闭')
    expect(wrapper.vm.stateLabel('')).toBe('未知')
  })

  it('emptyText changes based on search/filter', () => {
    const wrapper = mountJobs()

    wrapper.vm.searchQuery = 'engineer'
    expect(wrapper.vm.emptyText).toContain('engineer')

    wrapper.vm.searchQuery = ''
    wrapper.vm.stateFilter = 'active'
    expect(wrapper.vm.emptyText).toContain('该状态下暂无职位')

    wrapper.vm.stateFilter = ''
    expect(wrapper.vm.emptyText).toBe('暂无职位数据')
  })
})
