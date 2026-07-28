import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const mockGetActive = vi.fn()
const mockListVersions = vi.fn()
const mockCreate = vi.fn()
const mockActivate = vi.fn()

vi.mock('../src/api/client.js', () => ({
  api: {
    getActiveProfile: (...a) => mockGetActive(...a),
    listProfileVersions: (...a) => mockListVersions(...a),
    createProfileVersion: (...a) => mockCreate(...a),
    activateProfileVersion: (...a) => mockActivate(...a),
  },
  parseApiError: (err) => ({
    status: err?.status,
    code: err?.code,
    message: err?.messageText || err?.message || '',
    details: err?.details ?? null,
    retryable: !!err?.retryable,
    isDependencyNotReady:
      err?.status === 503 ||
      err?.code === 'DEPENDENCY_NOT_READY' ||
      err?.code === 'UNAVAILABLE_DEPENDENCY',
  }),
  formatApiError: (err, fallback) => err?.messageText || err?.message || fallback,
  smartIntakeUiEnabled: false,
  setCsrfToken: vi.fn(),
}))

vi.mock('ant-design-vue', () => ({ message: { success: vi.fn(), info: vi.fn(), warning: vi.fn() } }))

vi.mock('vue-router', () => ({ RouterLink: { template: '<a><slot /></a>', props: ['to'] } }))

const stubs = {
  'a-card': { template: '<div class="a-card"><slot /><slot name="title" /><slot name="emptyText" /></div>', props: ['title'] },
  'a-form': { template: '<div class="a-form"><slot /></div>', props: ['model', 'layout'] },
  'a-form-item': { template: '<div><slot /></div>' },
  'a-input': { template: '<input v-model="modelValue" />', props: ['modelValue', 'value', 'placeholder', 'style'] },
  'a-input-number': { template: '<input />', props: ['modelValue', 'value', 'placeholder', 'min', 'style'] },
  'a-select': {
    template: '<select><slot /></select>',
    props: ['modelValue', 'value', 'style', 'mode', 'placeholder'],
  },
  'a-select-option': { template: '<option :value="value"><slot /></option>', props: ['value'] },
  'a-checkbox': {
    template: '<input type="checkbox" :checked="modelValue" @change="$emit(\'update:checked\', $event.target.checked)" />',
    props: ['checked', 'modelValue'],
    emits: ['update:checked'],
  },
  'a-divider': { template: '<div class="a-divider"><slot /></div>', props: ['orientation'] },
  'a-button': {
    template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /><template v-if="$slots.icon"><slot name="icon" /></template></button>',
    props: ['disabled', 'loading', 'type', 'size', 'danger'],
    emits: ['click'],
  },
  'a-tag': { template: '<span class="a-tag"><slot /></span>', props: ['color'] },
  'a-alert': { template: '<div v-if="message" class="a-alert">{{ message }}</div>', props: ['type', 'message', 'description', 'showIcon', 'closable', 'banner'] },
  'a-space': { template: '<span class="a-space"><slot /></span>', props: ['wrap', 'align', 'direction', 'size'] },
  'a-spin': { template: '<div class="a-spin"><slot /></div>', props: ['spinning', 'size', 'tip'] },
  'a-empty': { template: '<div class="a-empty">{{ description }}</div>', props: ['description'] },
  'a-table': {
    template: '<div class="a-table"><template v-if="dataSource && dataSource.length"><template v-for="r in dataSource" :key="r[rowKey || \'id\']"><slot name="bodyCell" v-for="col in columns" :column="col" :record="r" /></template></template><div v-else class="table-empty"><slot name="emptyText" /></div></div>',
    props: ['columns', 'dataSource', 'loading', 'pagination', 'rowKey', 'scroll'],
  },
  'a-popconfirm': { template: '<span><slot /><slot name="title" /></span>', props: ['title'], emits: ['confirm'] },
  'a-result': { template: '<div class="a-result"><slot /><slot name="extra" /></div>', props: ['status', 'title', 'subTitle'] },
  'plus-outlined': { template: '<span />' },
}

import Profile from '../src/views/Profile.vue'

function apiErr(status, code, messageText = 'msg') {
  return Object.assign(new Error(`${status}: {}`), { status, code, messageText })
}

const ACTIVE_PROFILE = {
  id: 'p1', candidate_id: 'c1', version: 3, is_active: true, rules_version: 'rules-v2',
  target_roles: [{ title: 'Backend Engineer', seniority: 'Senior', notes: '' }],
  locations: [{ name: 'Shanghai', kind: 'preferred', radius_km: null }],
  remote_rules: { remote_allowed: true, hybrid_allowed: false, onsite_required: false, timezone: '' },
  compensation: { currency: 'CNY', amount_min: 100, amount_max: 200, period: 'annual', equity: false },
  seniority: 'Senior',
  authorization: { work_authorization: 'citizen', visa_sponsorship_required: false, locale_restrictions: [] },
  include_keywords: ['go'], exclude_keywords: [], hard_exclusions: { companies: [], titles: [], keywords: [] },
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Profile.vue', () => {
  function mountProfile() {
    return mount(Profile, { global: { stubs } })
  }

  it('loads active profile + history on mount', async () => {
    mockGetActive.mockResolvedValue(ACTIVE_PROFILE)
    mockListVersions.mockResolvedValue({ items: [ACTIVE_PROFILE], total: 1 })
    const wrapper = mountProfile()
    await flushPromises()
    expect(mockGetActive).toHaveBeenCalled()
    expect(mockListVersions).toHaveBeenCalledWith({ limit: 50 })
    expect(wrapper.text()).toContain('激活版本 v3')
  })

  it('falls back to empty form when no active profile (404)', async () => {
    mockGetActive.mockRejectedValue(apiErr(404))
    mockListVersions.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountProfile()
    await flushPromises()
    expect(wrapper.text()).toContain('尚无激活版本')
    // form empty, save disabled (no target roles)
    expect(wrapper.vm.canSave).toBe(false)
  })

  it('marks service unavailable on 503', async () => {
    mockGetActive.mockRejectedValue(apiErr(503, 'DEPENDENCY_NOT_READY'))
    mockListVersions.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountProfile()
    await flushPromises()
    expect(wrapper.vm.unavailable).toBe(true)
    // shows unavailable banner when nothing loaded
    expect(wrapper.text()).toContain('服务暂不可用')
  })

  it('saves a new version and refreshes history', async () => {
    mockGetActive.mockResolvedValue(ACTIVE_PROFILE)
    mockListVersions.mockResolvedValue({ items: [ACTIVE_PROFILE], total: 1 })
    const wrapper = mountProfile()
    await flushPromises()

    mockCreate.mockResolvedValue({ ...ACTIVE_PROFILE, version: 4 })
    mockListVersions.mockClear()
    mockListVersions.mockResolvedValue({ items: [{ ...ACTIVE_PROFILE, version: 4 }], total: 2 })

    await wrapper.vm.save()
    await flushPromises()

    expect(mockCreate).toHaveBeenCalledWith(expect.objectContaining({ activate: true }))
    expect(mockListVersions).toHaveBeenCalled()
    expect(wrapper.vm.activeVersion.version).toBe(4)
  })

  it('surfaces 409 INVALID_STATE as validationError (no generic error)', async () => {
    mockGetActive.mockResolvedValue(ACTIVE_PROFILE)
    mockListVersions.mockResolvedValue({ items: [ACTIVE_PROFILE], total: 1 })
    const wrapper = mountProfile()
    await flushPromises()

    mockCreate.mockRejectedValue(apiErr(409, 'INVALID_STATE', 'excluded location conflicts with required'))
    await wrapper.vm.save()
    await flushPromises()

    expect(wrapper.vm.validationError).toContain('excluded location conflicts with required')
    expect(wrapper.vm.error).toBe('')
  })

  it('activates a historical version via the API', async () => {
    mockGetActive.mockResolvedValue(ACTIVE_PROFILE)
    mockListVersions.mockResolvedValue({ items: [ACTIVE_PROFILE], total: 1 })
    const wrapper = mountProfile()
    await flushPromises()

    const activated = { ...ACTIVE_PROFILE, version: 2, is_active: true }
    mockActivate.mockResolvedValue(activated)
    await wrapper.vm.activate('v2-id')
    await flushPromises()

    expect(mockActivate).toHaveBeenCalledWith('v2-id')
    expect(wrapper.vm.activeVersion.version).toBe(2)
  })

  it('canSave requires at least one non-empty target role', async () => {
    mockGetActive.mockRejectedValue(apiErr(404))
    mockListVersions.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountProfile()
    await flushPromises()

    expect(wrapper.vm.canSave).toBe(false)
    wrapper.vm.addRole()
    expect(wrapper.vm.canSave).toBe(false)
    wrapper.vm.form.target_roles[0].title = 'Engineer'
    expect(wrapper.vm.canSave).toBe(true)
  })

  it('buildPayload filters empty role/location rows and sets activate:true', async () => {
    mockGetActive.mockRejectedValue(apiErr(404))
    mockListVersions.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountProfile()
    await flushPromises()

    wrapper.vm.addRole()
    wrapper.vm.form.target_roles[0].title = 'Engineer'
    wrapper.vm.addRole() // empty role -> filtered
    wrapper.vm.addLocation()
    wrapper.vm.form.locations[0].name = 'Beijing'
    wrapper.vm.addLocation() // empty -> filtered

    const payload = wrapper.vm.buildPayload()
    expect(payload.target_roles).toHaveLength(1)
    expect(payload.locations).toHaveLength(1)
    expect(payload.activate).toBe(true)
  })
})
