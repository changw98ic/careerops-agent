import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const mockCreate = vi.fn()
const mockApply = vi.fn()
const mockCapability = vi.fn()

vi.mock('../src/api/client.js', () => ({
  api: {
    getSmartIntakeCapability: (...args) => mockCapability(...args),
    createSmartIntakePreview: (...args) => mockCreate(...args),
    applySmartIntakePreview: (...args) => mockApply(...args),
  },
  formatApiError: (_err, fallback) => fallback,
}))

const stubs = {
  'a-card': { template: '<div><slot /></div>' },
  'a-textarea': {
    template: '<textarea :value="value" :disabled="disabled" @input="$emit(\'update:value\', $event.target.value)" />',
    props: ['value', 'disabled', 'rows', 'maxlength', 'showCount', 'placeholder'],
    emits: ['update:value'],
  },
  'a-button': {
    template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
    props: ['disabled', 'loading', 'type', 'size'],
    emits: ['click'],
  },
  'a-alert': { template: '<div v-if="message" class="alert">{{ message }} {{ description }}</div>', props: ['message', 'description', 'type', 'showIcon', 'closable'] },
  'a-divider': { template: '<hr />' },
  'a-tag': { template: '<span class="tag"><slot /></span>', props: ['color'] },
  'a-checkbox': {
    template: '<input type="checkbox" :checked="checked" :disabled="disabled" @change="$emit(\'update:checked\', $event.target.checked)" />',
    props: ['checked', 'disabled'],
    emits: ['update:checked'],
  },
  'a-input': {
    template: '<input :value="value" :disabled="disabled" :aria-label="ariaLabel" @input="$emit(\'update:value\', $event.target.value)" />',
    props: ['value', 'disabled', 'ariaLabel', 'size'],
    emits: ['update:value'],
  },
  'a-input-number': {
    template: '<input :value="value" :disabled="disabled" @input="$emit(\'update:value\', Number($event.target.value))" />',
    props: ['value', 'disabled', 'min', 'max', 'size', 'ariaLabel'],
    emits: ['update:value'],
  },
  'robot-outlined': { template: '<span />' },
}

const READY = {
  preview_id: 'p1',
  candidate_id: 'c1',
  target: 'profile',
  state: 'ready',
  input_digest: 'a'.repeat(64),
  context_digest: 'b'.repeat(64),
  model_id: 'disabled',
  prompt_version: 'smart-intake-v1',
  expires_at: '2026-07-28T12:00:00Z',
  fields: [
    {
      path: 'target_roles[0].title',
      value: 'Backend Engineer',
      value_type: 'string',
      confidence: 0.91,
      status: 'proposed',
      reason: '来源明确',
      source_refs: [{ input_digest: 'a'.repeat(64), start_offset: 0, end_offset: 16 }],
    },
    {
      path: 'remote_rules.remote_allowed',
      value: null,
      value_type: 'boolean',
      confidence: 0,
      status: 'blocked',
      reason: '受保护字段',
      source_refs: [],
    },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  mockCapability.mockResolvedValue({ released: true, provider_enabled: false })
  mockCreate.mockResolvedValue(READY)
  mockApply.mockResolvedValue({ ...READY, draft_patch: { fields: { 'target_roles[0].title': 'Backend Engineer' } } })
  Object.defineProperty(globalThis.crypto, 'randomUUID', {
    value: () => 'fixed-key',
    configurable: true,
  })
  vi.spyOn(globalThis.crypto.subtle, 'digest').mockResolvedValue(new Uint8Array(32).buffer)
})

describe('SmartIntakePanel.vue', () => {
  it('shows status, confidence, source spans, records unknown fields, and applies once', async () => {
    const wrapper = mount((await import('../src/components/SmartIntakePanel.vue')).default, {
      props: {
        target: 'profile',
        featureEnabled: true,
        baseSnapshot: { target_roles: [{ title: '', seniority: '', notes: '' }] },
      },
      global: { stubs },
    })
    await flushPromises()

    await wrapper.find('textarea').setValue('Backend Engineer')
    await wrapper.findAll('button')[0].trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('可审查')
    expect(wrapper.text()).toContain('已拦截')
    expect(wrapper.text()).toContain('置信度 91%')
    expect(wrapper.text()).toContain('“Backend Engineer” (0-16)')

    await wrapper.findAll('button').find((button) => button.text().includes('采纳')).trigger('click')
    await flushPromises()
    expect(mockApply).toHaveBeenCalledTimes(1)
    const payload = mockApply.mock.calls[0][1]
    expect(payload.decisions).toEqual(expect.arrayContaining([
      expect.objectContaining({ path: 'target_roles[0].title', decision: 'accept', reason: '用户确认智能预填建议' }),
      expect.objectContaining({ path: 'remote_rules.remote_allowed', decision: 'unknown' }),
    ]))
    expect(payload.decision_set_hash).toHaveLength(64)
    expect(payload.context_digest).toBe('b'.repeat(64))
    expect(wrapper.text()).toContain('本次选择已记录')

    await wrapper.findAll('button').find((button) => button.text().includes('采纳')).trigger('click')
    expect(mockApply).toHaveBeenCalledTimes(1)
  })

  it('preserves a manually changed value and reports the conflict', async () => {
    const current = { target_roles: [{ title: '', seniority: '', notes: '' }] }
    const wrapper = mount((await import('../src/components/SmartIntakePanel.vue')).default, {
      props: {
        target: 'profile',
        featureEnabled: true,
        baseSnapshot: { target_roles: [{ title: '', seniority: '', notes: '' }] },
        currentSnapshot: current,
      },
      global: { stubs },
    })
    await flushPromises()
    await wrapper.find('textarea').setValue('Backend Engineer')
    await wrapper.findAll('button')[0].trigger('click')
    await flushPromises()
    current.target_roles[0].title = 'Manual title'
    await wrapper.findAll('button').find((button) => button.text().includes('采纳')).trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('有 1 项因表单已修改而保留原值')
    expect(wrapper.emitted('applied')[0][0].conflictCount).toBe(1)
  })

  it('keeps the manual path explicit when the feature is disabled or context is incomplete', async () => {
    const disabled = mount((await import('../src/components/SmartIntakePanel.vue')).default, {
      props: { target: 'profile', featureEnabled: false },
      global: { stubs },
    })
    expect(disabled.text()).toContain('智能预填当前关闭')
    expect(disabled.find('textarea').element.disabled).toBe(true)
    expect(disabled.findAll('button')[0].element.disabled).toBe(true)

    const incomplete = mount((await import('../src/components/SmartIntakePanel.vue')).default, {
      props: { target: 'interview_context', featureEnabled: true },
      global: { stubs },
    })
    await flushPromises()
    expect(incomplete.text()).toContain('目标职位、职位版本、已确认简历')
    expect(incomplete.findAll('button')[0].element.disabled).toBe(true)
  })

  it('locks the review after a stale apply response and keeps regeneration manual', async () => {
    mockApply.mockRejectedValueOnce({ code: 'STALE_SMART_INTAKE_PREVIEW' })
    const wrapper = mount((await import('../src/components/SmartIntakePanel.vue')).default, {
      props: {
        target: 'profile',
        featureEnabled: true,
        baseSnapshot: { target_roles: [{ title: '', seniority: '', notes: '' }] },
      },
      global: { stubs },
    })
    await flushPromises()
    await wrapper.find('textarea').setValue('Backend Engineer')
    await wrapper.findAll('button')[0].trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('采纳')).trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('上下文已变化，需要重新生成')
    expect(wrapper.findAll('button').some((button) => button.text().includes('采纳'))).toBe(false)
  })
})
