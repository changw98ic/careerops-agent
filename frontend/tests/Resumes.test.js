import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const mockList = vi.fn()
const mockRegister = vi.fn()
const mockConfirm = vi.fn()

vi.mock('../src/api/client.js', () => ({
  api: {
    listResumes: (...a) => mockList(...a),
    registerResume: (...a) => mockRegister(...a),
    confirmResume: (...a) => mockConfirm(...a),
  },
  parseApiError: (err) => ({
    status: err?.status,
    code: err?.code,
    message: err?.messageText || err?.message || '',
    details: err?.details ?? null,
    retryable: !!err?.retryable,
    isDependencyNotReady: err?.status === 503 || err?.code === 'DEPENDENCY_NOT_READY',
  }),
  formatApiError: (err, fallback) => err?.messageText || err?.message || fallback,
}))

vi.mock('ant-design-vue', () => ({ message: { success: vi.fn(), info: vi.fn(), warning: vi.fn() } }))

vi.mock('vue-router', () => ({ RouterLink: { template: '<a><slot /></a>', props: ['to'] } }))

const stubs = {
  'a-card': { template: '<div class="a-card"><slot /><slot name="title" /></div>', props: ['title'] },
  'a-upload': { template: '<div class="a-upload"><slot /><slot name="default" /></div>', props: ['fileList', 'maxCount', 'accept'] },
  'a-button': {
    template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /><template v-if="$slots.icon"><slot name="icon" /></template></button>',
    props: ['disabled', 'loading', 'type', 'size', 'danger'],
    emits: ['click'],
  },
  'a-input': { template: '<input />', props: ['value', 'modelValue', 'placeholder', 'style'] },
  'a-space': { template: '<span class="a-space"><slot /></span>', props: ['wrap', 'direction', 'size'] },
  'a-tag': { template: '<span class="a-tag"><slot /></span>', props: ['color'] },
  'a-alert': { template: '<div v-if="message" class="a-alert">{{ message }}</div>', props: ['type', 'message', 'description', 'showIcon', 'closable'] },
  'a-spin': { template: '<div><slot /></div>', props: ['spinning', 'tip'] },
  'a-empty': { template: '<div class="a-empty">{{ description }}</div>', props: ['description'] },
  'a-table': {
    template: '<div class="a-table"><template v-if="dataSource && dataSource.length"><template v-for="r in dataSource" :key="r[rowKey || \'id\']"><slot name="bodyCell" v-for="col in columns" :column="col" :record="r" /></template></template><div v-else class="table-empty"><slot name="emptyText" /></div></div>',
    props: ['columns', 'dataSource', 'loading', 'pagination', 'rowKey', 'scroll'],
  },
  'upload-outlined': { template: '<span />' },
  'reload-outlined': { template: '<span />' },
}

import Resumes from '../src/views/Resumes.vue'

function apiErr(status, code, messageText = 'msg') {
  return Object.assign(new Error(`${status}: {}`), { status, code, messageText })
}

const RESUMES = {
  items: [
    {
      id: 'r1', version_number: 1, content_hash: 'a'.repeat(64), target_type: 'general',
      parse_status: 'parsed', confirmation_status: 'confirmed', created_at: '2026-01-01T00:00:00Z',
    },
    {
      id: 'r2', version_number: 2, content_hash: 'b'.repeat(64), target_type: 'backend',
      parse_status: 'failed', confirmation_status: 'unconfirmed', source_reference: 'bad pdf', created_at: '2026-01-02T00:00:00Z',
    },
    {
      id: 'r3', version_number: 3, content_hash: 'c'.repeat(64), target_type: 'general',
      parse_status: 'parsed', confirmation_status: 'unconfirmed', created_at: '2026-01-03T00:00:00Z',
    },
  ],
  total: 3,
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Resumes.vue', () => {
  function mountResumes() {
    return mount(Resumes, { global: { stubs } })
  }

  it('loads resume versions on mount', async () => {
    mockList.mockResolvedValue(RESUMES)
    const wrapper = mountResumes()
    await flushPromises()
    expect(mockList).toHaveBeenCalledWith({ limit: 50 })
    expect(wrapper.text()).toContain('3 个版本')
  })

  it('shows empty state when no resumes', async () => {
    mockList.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountResumes()
    await flushPromises()
    expect(wrapper.text()).toContain('尚无简历版本')
  })

  it('marks service unavailable on 503', async () => {
    mockList.mockRejectedValue(apiErr(503, 'DEPENDENCY_NOT_READY'))
    const wrapper = mountResumes()
    await flushPromises()
    expect(wrapper.vm.unavailable).toBe(true)
  })

  it('isEligible requires parsed + confirmed', () => {
    const wrapper = mountResumes()
    expect(wrapper.vm.isEligible({ parse_status: 'parsed', confirmation_status: 'confirmed' })).toBe(true)
    expect(wrapper.vm.isEligible({ parse_status: 'parsed', confirmation_status: 'unconfirmed' })).toBe(false)
    expect(wrapper.vm.isEligible({ parse_status: 'failed', confirmation_status: 'confirmed' })).toBe(false)
  })

  it('beforeUpload rejects unsupported media type', async () => {
    mockList.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountResumes()
    await flushPromises()

    const bad = new File(['x'], 'r.exe', { type: 'application/octet-stream' })
    const result = wrapper.vm.beforeUpload(bad)
    expect(result).toBe(false)
    expect(wrapper.vm.pendingFile).toBeNull()
    expect(wrapper.vm.uploadError).toContain('不支持的类型')
  })

  it('beforeUpload rejects oversize files', async () => {
    mockList.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountResumes()
    await flushPromises()

    const big = new File(['x'], 'r.pdf', { type: 'application/pdf' })
    Object.defineProperty(big, 'size', { value: 11 * 1024 * 1024 })
    const result = wrapper.vm.beforeUpload(big)
    expect(result).toBe(false)
    expect(wrapper.vm.uploadError).toContain('文件过大')
  })

  it('beforeUpload infers text/markdown from extension when type missing', async () => {
    mockList.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountResumes()
    await flushPromises()

    const md = new File(['# cv'], 'cv.md', { type: '' })
    const result = wrapper.vm.beforeUpload(md)
    expect(result).toBe(false) // we always block auto-upload
    expect(wrapper.vm.pendingFile.name).toBe('cv.md')
    expect(wrapper.vm.uploadError).toBe('')
  })

  it('upload calls registerResume with FormData-bearing file and refreshes list', async () => {
    mockList.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountResumes()
    await flushPromises()

    const file = new File(['skills: go, python'], 'cv.txt', { type: 'text/plain' })
    wrapper.vm.beforeUpload(file)
    mockRegister.mockResolvedValue({
      resume: { id: 'r9', version_number: 1 }, deduplicated: false,
      parse_status: 'parsed', extracted_evidence_count: 2,
    })
    mockList.mockResolvedValue({ items: [{ id: 'r9', version_number: 1, parse_status: 'parsed', confirmation_status: 'unconfirmed' }], total: 1 })

    await wrapper.vm.upload()
    await flushPromises()

    expect(mockRegister).toHaveBeenCalledTimes(1)
    expect(mockRegister.mock.calls[0][0].file.name).toBe('cv.txt')
    expect(mockRegister.mock.calls[0][0].target_type).toBe('general')
    expect(wrapper.vm.pendingFile).toBeNull()
  })

  it('surfaces dedupe hit without error', async () => {
    mockList.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountResumes()
    await flushPromises()

    const file = new File(['x'], 'cv.txt', { type: 'text/plain' })
    wrapper.vm.beforeUpload(file)
    mockRegister.mockResolvedValue({
      resume: { id: 'r1', version_number: 1 }, deduplicated: true,
      parse_status: 'parsed', extracted_evidence_count: 0,
    })
    await wrapper.vm.upload()
    await flushPromises()
    expect(wrapper.vm.uploadError).toBe('')
  })

  it('confirm calls confirmResume and reloads', async () => {
    mockList.mockResolvedValue(RESUMES)
    const wrapper = mountResumes()
    await flushPromises()

    mockConfirm.mockResolvedValue({ ...RESUMES.items[2], confirmation_status: 'confirmed' })
    await wrapper.vm.confirm('r3')
    await flushPromises()

    expect(mockConfirm).toHaveBeenCalledWith('r3')
  })

  it('confirm surfaces 409 when resume not parseable', async () => {
    mockList.mockResolvedValue(RESUMES)
    const wrapper = mountResumes()
    await flushPromises()

    mockConfirm.mockRejectedValue(apiErr(409, 'INVALID_STATE', 'resume cannot be confirmed'))
    await wrapper.vm.confirm('r2')
    await flushPromises()

    expect(wrapper.vm.error).toContain('resume cannot be confirmed')
  })
})
