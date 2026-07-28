import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import DashboardChart from '../src/components/DashboardChart.vue'

describe('DashboardChart', () => {
  it('emits a selection from a status mark click', async () => {
    const data = [{ state: 'active', count: 2 }]
    const wrapper = mount(DashboardChart, { props: { type: 'status', data } })

    await wrapper.find('[role="button"]').trigger('click')

    expect(wrapper.emitted('select')).toEqual([[expect.objectContaining({ state: 'active', count: 2 })]])
  })

  it('supports keyboard activation for timeline marks', async () => {
    const data = [{ date: '2026-07-28', state: '已投递', id: 'a1' }]
    const wrapper = mount(DashboardChart, { props: { type: 'timeline', data } })

    await wrapper.find('[role="button"]').trigger('keydown', { key: 'Enter' })

    expect(wrapper.emitted('select')).toEqual([[expect.objectContaining({ id: 'a1' })]])
  })
})
