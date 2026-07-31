import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const mockList = vi.fn()
const mockConfirm = vi.fn()
const mockReject = vi.fn()

vi.mock('../src/api/client.js', () => ({
  api: {
    listEvidence: (...a) => mockList(...a),
    confirmEvidence: (...a) => mockConfirm(...a),
    rejectEvidence: (...a) => mockReject(...a),
  },
  parseApiError: (err) => ({
    status: err?.status,
    code: err?.code,
    message: err?.messageText || err?.message || '',
    details: err?.details ?? null,
    retryable: !!err?.retryable,
    isDependencyNotReady: err?.status === 503 || err?.code === 'DEPENDENCY_NOT_READY',
  }),
}))

vi.mock('ant-design-vue', () => ({ message: { success: vi.fn(), info: vi.fn(), warning: vi.fn() } }))

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  RouterLink: { template: '<a><slot /></a>', props: ['to'] },
}))

const stubs = {
  'a-card': { template: '<div class="a-card"><slot /><slot name="title" /></div>', props: ['title'] },
  'a-list': {
    template: '<div class="a-list"><template v-if="dataSource && dataSource.length"><template v-for="item in dataSource" :key="item.id"><slot name="renderItem" :item="item" /></template></template><div v-else class="list-empty"><slot name="emptyText" /></div></div>',
    props: ['dataSource', 'loading'],
  },
  'a-list-item': {
    template: '<div class="a-list-item"><slot /><slot name="actions" /><slot name="extra" /></div>',
  },
  'a-list-item-meta': {
    template: '<div class="a-list-meta"><slot name="title" /><slot name="description" /><slot name="avatar" /></div>',
  },
  'a-select': {
    template: '<select :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\')"><slot /></select>',
    props: ['modelValue', 'value', 'style'],
    emits: ['update:modelValue', 'change'],
  },
  'a-select-option': { template: '<option :value="value"><slot /></option>', props: ['value'] },
  'a-checkbox': {
    template: '<input type="checkbox" :checked="modelValue" @change="$emit(\'update:checked\', $event.target.checked)" />',
    props: ['checked', 'modelValue'],
    emits: ['update:checked'],
  },
  'a-button': {
    template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
    props: ['disabled', 'loading', 'type', 'size', 'danger'],
    emits: ['click'],
  },
  'a-tag': { template: '<span class="a-tag"><slot /></span>', props: ['color'] },
  'a-space': { template: '<span class="a-space"><slot /></span>', props: ['wrap', 'direction', 'size', 'align'] },
  'a-alert': { template: '<div v-if="message" class="a-alert">{{ message }}</div>', props: ['type', 'message', 'description', 'showIcon', 'closable'] },
  'a-spin': { template: '<div><slot /></div>', props: ['spinning', 'tip'] },
  'a-empty': { template: '<div class="a-empty">{{ description }}</div>', props: ['description'] },
  'reload-outlined': { template: '<span />' },
}

import Evidence from '../src/views/Evidence.vue'

function apiErr(status, code, messageText = 'msg') {
  return Object.assign(new Error(`${status}: {}`), { status, code, messageText })
}

const EVIDENCE = {
  items: [
    {
      id: 'e1', kind: 'skill', name: 'Go', extractor_version: 'resume-analysis-v1',
      source_span: 'built services in go', confirmation_status: 'unconfirmed',
      evidence_hash: 'h1', resume_version_id: 'r1',
    },
    {
      id: 'e2', kind: 'skill', name: 'Leadership', extractor_version: 'model-tailor-v2',
      source_span: '', confirmation_status: 'unconfirmed', evidence_hash: 'h2', resume_version_id: null,
    },
    {
      id: 'e3', kind: 'skill', name: 'Python', extractor_version: 'resume-analysis-v1',
      source_span: 'python data pipelines', confirmation_status: 'confirmed',
      evidence_hash: 'h3', resume_version_id: 'r1',
    },
  ],
  total: 3,
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Evidence.vue', () => {
  function mountEvidence() {
    return mount(Evidence, { global: { stubs } })
  }

  it('loads evidence on mount', async () => {
    mockList.mockResolvedValue(EVIDENCE)
    const wrapper = mountEvidence()
    await flushPromises()
    expect(mockList).toHaveBeenCalledWith({})
    expect(wrapper.text()).toContain('3 条')
  })

  it('shows empty state when no evidence', async () => {
    mockList.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountEvidence()
    await flushPromises()
    expect(wrapper.text()).toContain('尚无证据')
  })

  it('marks service unavailable on 503', async () => {
    mockList.mockRejectedValue(apiErr(503, 'DEPENDENCY_NOT_READY'))
    const wrapper = mountEvidence()
    await flushPromises()
    expect(wrapper.vm.unavailable).toBe(true)
  })

  it('isModelProposed distinguishes model output from deterministic extraction', async () => {
    mockList.mockResolvedValue(EVIDENCE)
    const wrapper = mountEvidence()
    await flushPromises()

    expect(wrapper.vm.isModelProposed(EVIDENCE.items[0])).toBe(false) // resume-analysis-v1
    expect(wrapper.vm.isModelProposed(EVIDENCE.items[1])).toBe(true)  // model-tailor-v2
    expect(wrapper.vm.isModelProposed({ extractor_version: '' })).toBe(true)
  })

  it('onlyModel filter restricts to model-proposed items', async () => {
    mockList.mockResolvedValue(EVIDENCE)
    const wrapper = mountEvidence()
    await flushPromises()

    expect(wrapper.vm.filtered).toHaveLength(3)
    wrapper.vm.onlyModel = true
    expect(wrapper.vm.filtered).toHaveLength(1)
    expect(wrapper.vm.filtered[0].name).toBe('Leadership')
  })

  it('confirm updates the row locally and calls the API', async () => {
    mockList.mockResolvedValue(EVIDENCE)
    const wrapper = mountEvidence()
    await flushPromises()

    mockConfirm.mockResolvedValue({ evidence: EVIDENCE.items[0], status: 'confirmed', was_change: true })
    await wrapper.vm.confirm(EVIDENCE.items[0])
    await flushPromises()

    expect(mockConfirm).toHaveBeenCalledWith('e1')
    const row = wrapper.vm.evidence.find((e) => e.id === 'e1')
    expect(row.confirmation_status).toBe('confirmed')
  })

  it('idempotency: was_change=false leaves the row in place, no error', async () => {
    mockList.mockResolvedValue(EVIDENCE)
    const wrapper = mountEvidence()
    await flushPromises()

    const target = EVIDENCE.items[2] // already confirmed
    mockConfirm.mockResolvedValue({ evidence: target, status: 'confirmed', was_change: false })
    await wrapper.vm.confirm(target)
    await flushPromises()

    // row still present, status unchanged, no error
    expect(wrapper.vm.error).toBe('')
    const row = wrapper.vm.evidence.find((e) => e.id === 'e3')
    expect(row).toBeTruthy()
    expect(row.confirmation_status).toBe('confirmed')
  })

  it('confirm under an active status filter drops the row after transition', async () => {
    mockList.mockResolvedValue(EVIDENCE)
    const wrapper = mountEvidence()
    await flushPromises()

    wrapper.vm.statusFilter = 'unconfirmed' // e1, e2 visible; e3 filtered out
    const target = EVIDENCE.items[0]
    mockConfirm.mockResolvedValue({ evidence: target, status: 'confirmed', was_change: true })
    await wrapper.vm.confirm(target)
    await flushPromises()

    // e1 should have been removed from the local list because it no longer matches the filter
    expect(wrapper.vm.evidence.find((e) => e.id === 'e1')).toBeUndefined()
  })

  it('reject calls rejectEvidence API', async () => {
    mockList.mockResolvedValue(EVIDENCE)
    const wrapper = mountEvidence()
    await flushPromises()

    mockReject.mockResolvedValue({ evidence: EVIDENCE.items[0], status: 'rejected', was_change: true })
    await wrapper.vm.reject(EVIDENCE.items[0])
    await flushPromises()

    expect(mockReject).toHaveBeenCalledWith('e1')
  })

  it('passes status filter to API on load', async () => {
    mockList.mockResolvedValue(EVIDENCE)
    const wrapper = mountEvidence()
    await flushPromises()

    wrapper.vm.statusFilter = 'unconfirmed'
    mockList.mockClear()
    mockList.mockResolvedValue(EVIDENCE)
    await wrapper.vm.loadEvidence()
    expect(mockList).toHaveBeenCalledWith({ status: 'unconfirmed' })
  })
})
