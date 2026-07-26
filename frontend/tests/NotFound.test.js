import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'

vi.mock('../src/stores/session.js', () => ({
  status: ref('anonymous'),
}))

const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush }),
  RouterView: { template: '<div />' },
  RouterLink: { template: '<a><slot /></a>', props: ['to'] },
}))

const stubs = {
  'a-result': {
    template: '<div class="a-result">{{ title }} {{ subTitle }}<slot name="extra" /></div>',
    props: ['status', 'title', 'subTitle'],
  },
  'a-button': {
    template: '<button @click="$emit(\'click\')"><slot /></button>',
    props: ['type'],
    emits: ['click'],
  },
}

import NotFound from '../src/views/NotFound.vue'
import { status } from '../src/stores/session.js'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('NotFound.vue', () => {
  it('renders 404 result', () => {
    const wrapper = mount(NotFound, { global: { stubs } })
    expect(wrapper.text()).toContain('404')
  })

  it('navigates to /dashboard when authenticated', async () => {
    status.value = 'authenticated'
    const wrapper = mount(NotFound, { global: { stubs } })

    await wrapper.find('button').trigger('click')
    expect(mockPush).toHaveBeenCalledWith('/dashboard')
  })

  it('navigates to /login when anonymous', async () => {
    status.value = 'anonymous'
    const wrapper = mount(NotFound, { global: { stubs } })

    await wrapper.find('button').trigger('click')
    expect(mockPush).toHaveBeenCalledWith('/login')
  })
})
