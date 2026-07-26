import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'

const mockBootstrap = vi.fn()
vi.mock('../src/stores/session.js', () => ({
  bootstrap: (...args) => mockBootstrap(...args),
  status: ref('anonymous'),
  user: { username: '', candidate_id: '', csrf_token: '' },
  clearUser: vi.fn(),
}))

const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush }),
  useRoute: () => ({ name: 'bootstrap', query: {} }),
  RouterView: { template: '<div />' },
  RouterLink: { template: '<a><slot /></a>', props: ['to'] },
}))

const stubs = {
  'a-card': { template: '<div class="a-card"><slot /></div>' },
  'a-form': { template: '<form><slot /></form>', props: ['model', 'layout'] },
  'a-form-item': { template: '<div class="a-form-item"><slot /></div>' },
  'a-input': {
    template: '<input :value="modelValue || value" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    props: ['modelValue', 'value', 'disabled', 'placeholder', 'autocomplete'],
    emits: ['update:modelValue', 'update:value'],
  },
  'a-input-password': {
    template: '<input type="password" :value="modelValue || value" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    props: ['modelValue', 'value', 'disabled', 'placeholder', 'autocomplete'],
    emits: ['update:modelValue', 'update:value'],
  },
  'a-button': {
    template: '<button :disabled="disabled"><slot /></button>',
    props: ['disabled', 'loading', 'htmlType', 'type', 'size', 'block'],
  },
  'a-alert': { template: '<div class="a-alert" />', props: ['type', 'message', 'showIcon'] },
  'user-outlined': { template: '<span />' },
  'lock-outlined': { template: '<span />' },
  'key-outlined': { template: '<span />' },
}

import Bootstrap from '../src/views/Bootstrap.vue'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Bootstrap.vue', () => {
  function mountBootstrap() {
    return mount(Bootstrap, { global: { stubs } })
  }

  it('renders the bootstrap form', () => {
    const wrapper = mountBootstrap()
    expect(wrapper.text()).toContain('Set up your account')
    expect(wrapper.text()).toContain('Create account')
  })

  it('formValid is false when fields are empty', () => {
    const wrapper = mountBootstrap()
    expect(wrapper.vm.formValid).toBe(false)
  })

  it('formValid is true when all fields are valid', () => {
    const wrapper = mountBootstrap()
    const vm = wrapper.vm
    vm.formData.bootstrap_token = 'tok123'
    vm.formData.username = 'admin'
    vm.formData.password = 'password1234' // 12+ chars (passwordRules min length)
    vm.formData.confirm_password = 'password1234'
    expect(vm.formValid).toBe(true)
  })

  it('formValid is false when passwords do not match', () => {
    const wrapper = mountBootstrap()
    const vm = wrapper.vm
    vm.formData.bootstrap_token = 'tok123'
    vm.formData.username = 'admin'
    vm.formData.password = 'password123'
    vm.formData.confirm_password = 'different'
    expect(vm.formValid).toBe(false)
  })

  it('formValid is false when username is too short', () => {
    const wrapper = mountBootstrap()
    const vm = wrapper.vm
    vm.formData.bootstrap_token = 'tok123'
    vm.formData.username = 'ab'
    vm.formData.password = 'password123'
    vm.formData.confirm_password = 'password123'
    expect(vm.formValid).toBe(false)
  })

  it('formValid is false when username has invalid chars', () => {
    const wrapper = mountBootstrap()
    const vm = wrapper.vm
    vm.formData.bootstrap_token = 'tok123'
    vm.formData.username = 'admin@home'
    vm.formData.password = 'password123'
    vm.formData.confirm_password = 'password123'
    expect(vm.formValid).toBe(false)
  })

  it('calls bootstrap and redirects on success', async () => {
    mockBootstrap.mockResolvedValueOnce()
    const wrapper = mountBootstrap()
    const vm = wrapper.vm
    vm.formData.bootstrap_token = 'tok123'
    vm.formData.username = 'admin'
    vm.formData.password = 'password123'
    vm.formData.confirm_password = 'password123'

    await vm.doBootstrap()

    expect(mockBootstrap).toHaveBeenCalledWith('tok123', 'admin', 'password123')
    expect(mockPush).toHaveBeenCalledWith({ name: 'dashboard' })
  })

  it('displays mapped error on INVALID_BOOTSTRAP_CREDENTIAL', async () => {
    const err = new Error('bad token')
    err.code = 'INVALID_BOOTSTRAP_CREDENTIAL'
    mockBootstrap.mockRejectedValueOnce(err)

    const wrapper = mountBootstrap()
    const vm = wrapper.vm
    vm.formData.bootstrap_token = 'bad'
    vm.formData.username = 'admin'
    vm.formData.password = 'password123'
    vm.formData.confirm_password = 'password123'

    await vm.doBootstrap()
    expect(vm.error).toBe('Bootstrap token is invalid or expired.')
  })

  it('displays mapped error on BOOTSTRAP_CLOSED', async () => {
    const err = new Error('closed')
    err.code = 'BOOTSTRAP_CLOSED'
    mockBootstrap.mockRejectedValueOnce(err)

    const wrapper = mountBootstrap()
    wrapper.vm.formData.bootstrap_token = 'tok'
    wrapper.vm.formData.username = 'admin'
    wrapper.vm.formData.password = 'password123'
    wrapper.vm.formData.confirm_password = 'password123'

    await wrapper.vm.doBootstrap()
    expect(wrapper.vm.error).toBe('An account already exists. Bootstrap is closed.')
  })

  it('sets loading state during bootstrap', async () => {
    let resolveBootstrap
    mockBootstrap.mockImplementationOnce(() => new Promise((r) => { resolveBootstrap = r }))

    const wrapper = mountBootstrap()
    expect(wrapper.vm.loading).toBe(false)

    const p = wrapper.vm.doBootstrap()
    expect(wrapper.vm.loading).toBe(true)

    resolveBootstrap()
    await p
    expect(wrapper.vm.loading).toBe(false)
  })
})
