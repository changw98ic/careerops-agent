import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'

// Mock session store
const mockLogin = vi.fn()
vi.mock('../src/stores/session.js', () => ({
  login: (...args) => mockLogin(...args),
  status: ref('anonymous'),
  user: { username: '', candidate_id: '', csrf_token: '' },
  clearUser: vi.fn(),
}))

// Mock router
const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush }),
  useRoute: () => ({ name: 'login', query: {} }),
  RouterView: { template: '<div />' },
  RouterLink: { template: '<a><slot /></a>', props: ['to'] },
}))

// Stub ant-design-vue components used by Login.vue
const stubs = {
  'a-card': { template: '<div class="a-card"><slot /></div>' },
  'a-form': { template: '<form @submit.prevent="$attrs.onFinish && $attrs.onFinish()"><slot /></form>' },
  'a-form-item': { template: '<div class="a-form-item"><slot /></div>' },
  'a-input': {
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    props: ['modelValue', 'value', 'disabled', 'placeholder', 'autocomplete'],
    emits: ['update:modelValue', 'update:value'],
  },
  'a-input-password': {
    template: '<input type="password" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    props: ['modelValue', 'value', 'disabled', 'placeholder', 'autocomplete'],
    emits: ['update:modelValue', 'update:value'],
  },
  'a-button': {
    template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
    props: ['disabled', 'loading', 'htmlType', 'type', 'size', 'block'],
    emits: ['click'],
  },
  'a-alert': { template: '<div class="a-alert" />', props: ['type', 'message', 'showIcon'] },
  'user-outlined': { template: '<span />' },
  'lock-outlined': { template: '<span />' },
}

import Login from '../src/views/Login.vue'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Login.vue', () => {
  function mountLogin() {
    return mount(Login, { global: { stubs } })
  }

  it('renders the login form', () => {
    const wrapper = mountLogin()
    expect(wrapper.text()).toContain('欢迎回来')
    expect(wrapper.text()).toContain('登录')
  })

  it('renders username and password inputs', () => {
    const wrapper = mountLogin()
    const inputs = wrapper.findAll('input')
    expect(inputs.length).toBeGreaterThanOrEqual(2)
  })

  it('calls login() and redirects on success', async () => {
    mockLogin.mockResolvedValueOnce()
    const wrapper = mountLogin()

    // Set values via the formData reactive object (what doLogin actually reads)
    const vm = wrapper.vm
    vm.formData.username = 'alice'
    vm.formData.password = 'secret123'

    await vm.doLogin()

    expect(mockLogin).toHaveBeenCalledWith('alice', 'secret123')
    expect(mockPush).toHaveBeenCalledWith({ name: 'dashboard' })
  })

  it('displays error message on login failure', async () => {
    mockLogin.mockRejectedValueOnce(new Error('Username or password is invalid'))
    const wrapper = mountLogin()

    const vm = wrapper.vm
    vm.formData.username = 'alice'
    vm.formData.password = 'wrong'

    await vm.doLogin()

    expect(wrapper.vm.error).toBe('用户名或密码错误')
  })

  it('maps rate limit error correctly', async () => {
    mockLogin.mockRejectedValueOnce(new Error('Too many attempts; try again later'))
    const wrapper = mountLogin()
    wrapper.vm.formData.username = 'alice'
    wrapper.vm.formData.password = 'pass'

    await wrapper.vm.doLogin()

    expect(wrapper.vm.error).toBe('登录尝试过于频繁，请稍后再试')
  })

  it('sets loading state during login', async () => {
    let resolveLogin
    mockLogin.mockImplementationOnce(() => new Promise((r) => { resolveLogin = r }))
    const wrapper = mountLogin()

    expect(wrapper.vm.loading).toBe(false)

    const loginPromise = wrapper.vm.doLogin()
    expect(wrapper.vm.loading).toBe(true)

    resolveLogin()
    await loginPromise

    expect(wrapper.vm.loading).toBe(false)
  })

  it('shows fallback error for unknown errors', async () => {
    mockLogin.mockRejectedValueOnce(new Error('something weird'))
    const wrapper = mountLogin()
    wrapper.vm.formData.username = 'a'
    wrapper.vm.formData.password = 'b'

    await wrapper.vm.doLogin()

    expect(wrapper.vm.error).toBe('登录失败，请重试')
  })
})
