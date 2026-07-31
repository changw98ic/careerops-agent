import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'

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

beforeEach(() => {
  vi.clearAllMocks()
})

describe('NotFound.vue', () => {
  it('renders 404 result', () => {
    const wrapper = mount(NotFound, { global: { stubs } })
    expect(wrapper.text()).toContain('404')
  })

  it('navigates to /dashboard from the 404 page', async () => {
    const wrapper = mount(NotFound, { global: { stubs } })

    await wrapper.find('button').trigger('click')
    expect(mockPush).toHaveBeenCalledWith('/dashboard')
  })
})
