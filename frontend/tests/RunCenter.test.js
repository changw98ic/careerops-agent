import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('ant-design-vue', () => ({
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

import RunCenter from '../src/components/RunCenter.vue'

const stubs = {
  'a-button': {
    template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
    props: ['disabled', 'loading', 'type', 'size', 'danger', 'ghost'],
    emits: ['click'],
  },
  'a-tag': { template: '<span class="a-tag"><slot /></span>', props: ['color'] },
  'a-card': { template: '<div class="a-card"><slot /></div>', props: ['title'] },
  'a-empty': { template: '<div class="a-empty">{{ description }}</div>', props: ['description'] },
  'a-alert': {
    template: '<div v-if="message || description" class="a-alert">{{ message }} {{ description }}</div>',
    props: ['type', 'message', 'description', 'showIcon', 'closable'],
  },
  'a-descriptions': { template: '<div><slot /></div>', props: ['column', 'size', 'bordered'] },
  'a-descriptions-item': { template: '<div><slot /></div>', props: ['label'] },
  'a-spin': { template: '<div class="a-spin" />', props: ['tip', 'size'] },
}

// StageTimeline renders a-tag for each stage; stub it directly so stage
// entries are visible as DOM text.
const StageTimelineStub = {
  template: '<div class="stage-timeline"><div v-for="s in stages" :key="s.id" class="stage-item">{{ s.stage }}</div></div>',
  props: ['stages'],
}

function mountRunCenter({ state = 'succeeded', stages = [] } = {}) {
  return mount(RunCenter, {
    props: {
      run: {
        id: 'run-1',
        capability: 'resume_review',
        state,
        input_hash: 'abc',
        schema_version: 'v1',
        prompt_version: 'p1',
        evidence_ids: [],
        input_identities: { resume_version_id: 'r1' },
        trace_id: 'trace-1',
        result: { status: 'completed' },
        model_id: 'm1',
        input_tokens: 1,
        output_tokens: 1,
        error_category: '',
      },
      stages,
      loading: false,
      actionLoading: { retry: false, stop: false, review: false },
    },
    global: {
      stubs: { ...stubs, StageTimeline: StageTimelineStub },
    },
  })
}

function buttonByText(wrapper, text) {
  return wrapper.findAll('button').find((button) => button.text().includes(text))
}

describe('RunCenter — run-control visibility (qa7)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it.each(['failed', 'abstained', 'unavailable', 'cancelled'])(
    'offers retry for %s runs',
    (state) => {
      const w = mountRunCenter({ state })
      expect(buttonByText(w, '重试')).toBeTruthy()
    },
  )

  it.each(['succeeded', 'pending', 'running', 'reviewed', 'stale'])(
    'hides retry for %s runs',
    (state) => {
      const w = mountRunCenter({ state })
      expect(buttonByText(w, '重试')).toBeFalsy()
    },
  )

  it('offers stop only for pending runs', () => {
    const pending = mountRunCenter({ state: 'pending' })
    expect(buttonByText(pending, '停止')).toBeTruthy()

    for (const state of ['running', 'succeeded', 'failed', 'cancelled', 'abstained']) {
      const w = mountRunCenter({ state })
      expect(buttonByText(w, '停止')).toBeFalsy()
    }
  })

  it('emits retry and stop with the run payload', async () => {
    const retry = mountRunCenter({ state: 'failed' })
    await retry.findAll('button').find((b) => b.text().includes('重试')).trigger('click')
    expect(retry.emitted('retry')).toHaveLength(1)
    expect(retry.emitted('retry')[0][0].state).toBe('failed')

    const stop = mountRunCenter({ state: 'pending' })
    await stop.findAll('button').find((b) => b.text().includes('停止')).trigger('click')
    expect(stop.emitted('stop')).toHaveLength(1)
    expect(stop.emitted('stop')[0][0].state).toBe('pending')
  })

  it('shows the synchronous-execution note when stages are empty', () => {
    const w = mountRunCenter({ state: 'succeeded', stages: [] })
    expect(w.text()).toContain('同步执行')
    expect(w.find('.stage-item').exists()).toBe(false)
  })

  it('renders the stage timeline when stage events exist', () => {
    const w = mountRunCenter({
      state: 'succeeded',
      stages: [{ id: 'e1', stage: 'invoke_model_stage', status: 'succeeded' }],
    })
    expect(w.text()).not.toContain('同步执行')
    expect(w.findAll('.stage-item')).toHaveLength(1)
  })
})
