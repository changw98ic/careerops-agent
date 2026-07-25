import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const mockListJobs = vi.fn()
const mockListApplications = vi.fn()
vi.mock('../src/api/client.js', () => ({
  api: {
    listJobs: (...args) => mockListJobs(...args),
    listApplications: (...args) => mockListApplications(...args),
  },
  setCsrfToken: vi.fn(),
}))

vi.mock('@antv/g2', () => ({
  Chart: vi.fn().mockImplementation(() => ({
    interval: vi.fn().mockReturnThis(),
    area: vi.fn().mockReturnThis(),
    line: vi.fn().mockReturnThis(),
    point: vi.fn().mockReturnThis(),
    data: vi.fn().mockReturnThis(),
    encode: vi.fn().mockReturnThis(),
    scale: vi.fn().mockReturnThis(),
    axis: vi.fn().mockReturnThis(),
    legend: vi.fn().mockReturnThis(),
    tooltip: vi.fn().mockReturnThis(),
    interaction: vi.fn().mockReturnThis(),
    style: vi.fn().mockReturnThis(),
    coordinate: vi.fn().mockReturnThis(),
    on: vi.fn().mockReturnThis(),
    render: vi.fn().mockResolvedValue(undefined),
    destroy: vi.fn(),
  })),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
  RouterView: { template: '<div />' },
  RouterLink: { template: '<a><slot /></a>', props: ['to'] },
}))

const stubs = {
  'a-row': { template: '<div><slot /></div>', props: ['gutter'] },
  'a-col': { template: '<div><slot /></div>', props: ['xs', 'sm', 'lg'] },
  'a-card': { template: '<div class="a-card"><slot /><slot name="title" /></div>', props: ['title', 'hoverable'] },
  'a-button': {
    template: '<button @click="$emit(\'click\')"><slot /></button>',
    props: ['loading', 'type', 'size'],
    emits: ['click'],
  },
  'a-tag': { template: '<span><slot /></span>', props: ['color'] },
  'a-alert': { template: '<div v-if="message" />', props: ['type', 'message', 'showIcon', 'closable'] },
  'a-statistic': { template: '<div><slot name="prefix" /><slot /></div>', props: ['title', 'value'] },
  'a-spin': { template: '<div />', props: ['tip'] },
  'a-empty': { template: '<div class="a-empty" />', props: ['description'] },
  'a-result': { template: '<div />', props: ['status', 'title', 'subTitle'] },
  'a-list': { template: '<div><slot name="renderItem" v-for="item in dataSource" :item="item" :key="item.id || item.canonical_job_id" /></div>', props: ['dataSource', 'size'] },
  'a-list-item': { template: '<div><slot /></div>' },
  'a-list-item-meta': { template: '<div><slot name="title" /><slot name="description" /></div>' },
  'a-steps': { template: '<div />', props: ['current', 'items', 'size', 'progressDot'] },
  'inbox-outlined': { template: '<span />' },
  'thunderbolt-outlined': { template: '<span />' },
  'file-text-outlined': { template: '<span />' },
  'check-circle-outlined': { template: '<span />' },
  'reload-outlined': { template: '<span />' },
}

import Dashboard from '../src/views/Dashboard.vue'

const MOCK_JOBS = {
  items: [
    { id: 'j1', aggregate_state: 'active', created_at: '2025-07-01' },
    { id: 'j2', aggregate_state: 'active', created_at: '2025-07-15' },
    { id: 'j3', aggregate_state: 'closed', created_at: '2025-06-20' },
  ],
  total: 3,
}

const MOCK_APPS = {
  items: [
    { id: 'a1', state: 'submitted', submitted_at: '2025-07-10', canonical_job_id: 'j1' },
    { id: 'a2', state: 'preparing', submitted_at: null, canonical_job_id: 'j2' },
  ],
  total: 2,
}

beforeEach(() => {
  vi.clearAllMocks()
  mockListJobs.mockResolvedValue(MOCK_JOBS)
  mockListApplications.mockResolvedValue(MOCK_APPS)
})

describe('Dashboard.vue', () => {
  function mountDashboard() {
    return mount(Dashboard, { global: { stubs } })
  }

  it('renders page header', () => {
    const wrapper = mountDashboard()
    expect(wrapper.text()).toContain('工作台总览')
  })

  it('fetches jobs and applications on mount', async () => {
    mountDashboard()
    await flushPromises()
    expect(mockListJobs).toHaveBeenCalledWith({ limit: 200 })
    expect(mockListApplications).toHaveBeenCalledWith({ limit: 200 })
  })

  it('computes metrics correctly', async () => {
    const wrapper = mountDashboard()
    await flushPromises()

    const vm = wrapper.vm
    expect(vm.metrics.jobs).toBe(3)
    expect(vm.metrics.activeJobs).toBe(2)
    expect(vm.metrics.applications).toBe(2)
    expect(vm.metrics.submitted).toBe(1)
  })

  it('shows error on fetch failure', async () => {
    mockListJobs.mockRejectedValue(new Error('Network error'))
    const wrapper = mountDashboard()
    await flushPromises()

    expect(wrapper.vm.error).toBe('Network error')
  })

  it('computes status distribution chart data', async () => {
    const wrapper = mountDashboard()
    await flushPromises()

    const statusDist = wrapper.vm.chartData.statusDist
    expect(statusDist).toEqual(
      expect.arrayContaining([
        { state: 'active', count: 2 },
        { state: 'closed', count: 1 },
      ])
    )
  })

  it('computes empty chart data when no jobs', async () => {
    mockListJobs.mockResolvedValue({ items: [], total: 0 })
    mockListApplications.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountDashboard()
    await flushPromises()

    expect(wrapper.vm.chartData.statusDist).toEqual([])
    expect(wrapper.vm.chartData.funnel).toEqual([])
    expect(wrapper.vm.chartData.timeline).toEqual([])
  })

  it('computes funnel data from applications', async () => {
    const wrapper = mountDashboard()
    await flushPromises()

    const funnel = wrapper.vm.chartData.funnel
    expect(funnel).toEqual(
      expect.arrayContaining([
        { state: '准备中', count: 1 },
        { state: '已投递', count: 1 },
      ])
    )
  })

  it('computes recent applications sorted by date', async () => {
    const wrapper = mountDashboard()
    await flushPromises()

    const recent = wrapper.vm.recentApplications
    expect(recent.length).toBe(2)
    // submitted_at: '2025-07-10' should come first (most recent)
    expect(recent[0].id).toBe('a1')
  })

  it('stateLabel returns correct labels', async () => {
    const wrapper = mountDashboard()
    await flushPromises()

    expect(wrapper.vm.stateLabel('active')).toBe('活跃')
    expect(wrapper.vm.stateLabel('closed')).toBe('已关闭')
    expect(wrapper.vm.stateLabel('submitted')).toBe('已投递')
    expect(wrapper.vm.stateLabel('unknown')).toBe('未知')
  })

  it('applicationColor returns correct colors', async () => {
    const wrapper = mountDashboard()
    await flushPromises()

    expect(wrapper.vm.applicationColor('submitted')).toBe('green')
    expect(wrapper.vm.applicationColor('rejected')).toBe('red')
    expect(wrapper.vm.applicationColor('unknown')).toBe('default')
  })
})
