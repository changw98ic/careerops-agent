import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// Section 14.8 — comprehensive frontend unit tests covering:
//   - package diff display (Section 8 / 14.8)
//   - confirmation behavior (Section 9/10 / 14.8)
//   - thread review (Section 13 / 14.8)
//   - reminder actions (Section 13 / 14.8)
//   - application timeline rendering with status distinctions (14.5)
//   - "why recommended" evidence display (14.4)
//   - route guards (14.1)
//   - session failure / parseApiError (14.2)
//   - dependency-not-ready states (14.6)

// ---- shared mocks ----

const mockGetApplication = vi.fn()
const mockGetChannels = vi.fn()
const mockGetTimeline = vi.fn()
const mockPrepareApplication = vi.fn()
const mockSelectChannel = vi.fn()
const mockConfirmExternal = vi.fn()
const mockChangeState = vi.fn()
const mockGetInboxJobDetail = vi.fn()
const mockGetExcludedReasons = vi.fn()
const mockFavorite = vi.fn()
const mockListDrafts = vi.fn()
const mockApproveDraft = vi.fn()
const mockRejectDraft = vi.fn()
const mockEditDraft = vi.fn()
const mockSendDraft = vi.fn()
const mockGetAccount = vi.fn()
const mockListThreads = vi.fn()
const mockListUnresolved = vi.fn()
const mockListSyncHistory = vi.fn()

vi.mock('../src/api/client.js', () => ({
  api: {
    getApplication: (...a) => mockGetApplication(...a),
    getApplicationChannels: (...a) => mockGetChannels(...a),
    getApplicationTimeline: (...a) => mockGetTimeline(...a),
    prepareApplication: (...a) => mockPrepareApplication(...a),
    selectApplicationChannel: (...a) => mockSelectChannel(...a),
    confirmExternalSubmission: (...a) => mockConfirmExternal(...a),
    changeApplicationState: (...a) => mockChangeState(...a),
    getInboxJobDetail: (...a) => mockGetInboxJobDetail(...a),
    getInboxExcludedReasons: (...a) => mockGetExcludedReasons(...a),
    favoriteInboxJob: (...a) => mockFavorite(...a),
    ignoreInboxJob: vi.fn(),
    snoozeInboxJob: vi.fn(),
    listReplyDrafts: (...a) => mockListDrafts(...a),
    getReplyDraft: vi.fn(),
    approveReplyDraft: (...a) => mockApproveDraft(...a),
    rejectReplyDraft: (...a) => mockRejectDraft(...a),
    editReplyDraft: (...a) => mockEditDraft(...a),
    sendReplyDraft: (...a) => mockSendDraft(...a),
    listApplicationFollowUps: vi.fn().mockResolvedValue({ items: [] }),
    snoozeFollowUp: vi.fn(),
    completeFollowUp: vi.fn(),
    cancelFollowUp: vi.fn(),
    rescheduleFollowUp: vi.fn(),
    getSystemSendStatus: vi.fn().mockResolvedValue({ phase: 'pending' }),
    escalateSystemSendReconciliation: vi.fn(),
    confirmSystemSend: vi.fn(),
    listRecruitingContacts: vi.fn().mockResolvedValue({ contacts: [] }),
    previewSubmission: vi.fn(),
    getMailAccountStatus: (...a) => mockGetAccount(...a),
    revokeMailAccount: vi.fn(),
    syncMailNow: vi.fn(),
    listMailSyncHistory: (...a) => mockListSyncHistory(...a),
    listMailThreads: (...a) => mockListThreads(...a),
    listMailMessages: vi.fn(),
    listMailUnresolvedLinks: (...a) => mockListUnresolved(...a),
    confirmMailLink: vi.fn(),
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
  setCsrfToken: vi.fn(),
}))

const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush }),
  useRoute: () => ({ params: { id: 'app-1' }, query: {} }),
  RouterLink: { template: '<a><slot /></a>', props: ['to'] },
}))

vi.mock('ant-design-vue', () => ({
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

// ---- shared stubs ----

const stubs = {
  'a-button': {
    template:
      '<button :disabled="disabled" @click="$emit(\'click\')"><slot /><slot name="icon" /></button>',
    props: ['disabled', 'loading', 'type', 'size', 'ghost', 'danger'],
    emits: ['click'],
  },
  'a-tag': { template: '<span class="a-tag"><slot /></span>', props: ['color', 'size'] },
  'a-alert': {
    template: '<div v-if="message || description" class="a-alert">{{ message }} {{ description }}<slot name="message" /><slot name="description" /></div>',
    props: ['type', 'message', 'description', 'showIcon', 'closable', 'banner'],
    emits: ['close'],
  },
  'a-space': { template: '<span><slot /></span>', props: ['wrap', 'size'] },
  'a-row': { template: '<div><slot /></div>', props: ['gutter'] },
  'a-col': { template: '<div><slot /></div>', props: ['xs', 'lg', 'span', 'md'] },
  'a-card': { template: '<div class="a-card"><div v-if="title">{{ title }}</div><slot /></div>', props: ['title'] },
  'a-descriptions': {
    template: '<div><slot /></div>',
    props: ['column', 'size', 'bordered', 'labelStyle'],
  },
  'a-descriptions-item': { template: '<div><slot /></div>', props: ['label'] },
  'a-spin': { template: '<div class="a-spin"><slot /></div>', props: ['size', 'spinning'] },
  'a-result': {
    template: '<div class="a-result">{{ title }} {{ subTitle }}<slot name="extra" /></div>',
    props: ['status', 'title', 'subTitle'],
  },
  'a-empty': { template: '<div class="a-empty">{{ description }}</div>', props: ['description'] },
  'a-timeline': { template: '<div class="a-timeline"><slot /></div>' },
  'a-timeline-item': { template: '<div class="a-timeline-item"><slot /></div>', props: ['color'] },
  'a-table': {
    template: '<div class="a-table"><div v-for="(row, ri) in dataSource" :key="ri"><slot name="bodyCell" v-for="col in columns" :column="col" :record="row" :text="row[col.dataIndex]" :index="ri" /></div></div>',
    props: ['columns', 'dataSource', 'pagination', 'rowKey', 'size', 'scroll'],
  },
  'a-dropdown': { template: '<div><slot /><slot name="overlay" /></div>' },
  'a-menu': { template: '<div><slot /></div>', props: ['selectedKeys'] },
  'a-menu-item': { template: '<div><slot /></div>', props: ['key'] },
  'a-modal': {
    template: '<div v-if="open"><slot /></div>',
    props: ['open', 'title', 'confirmLoading'],
    emits: ['ok'],
  },
  'a-form': { template: '<div><slot /></div>', props: ['layout', 'model'] },
  'a-form-item': { template: '<div><slot /></div>', props: ['label', 'validateStatus', 'help', 'required'] },
  'a-input': { template: '<input />', props: ['value', 'placeholder', 'allowClear'] },
  'a-textarea': { template: '<textarea />', props: ['value', 'rows', 'placeholder'] },
  'a-date-picker': { template: '<input />', props: ['value', 'showTime', 'format', 'placeholder'] },
  'a-input-search': { template: '<input />', props: ['value', 'placeholder', 'allowClear'] },
  'a-select': { template: '<select />', props: ['value', 'placeholder', 'allowClear', 'mode', 'style'] },
  'a-select-option': { template: '<option><slot /></option>', props: ['value'] },
  'a-radio-group': { template: '<div><slot /></div>', props: ['value'] },
  'a-radio-button': { template: '<button><slot /></button>', props: ['value'] },
  'a-tooltip': { template: '<span><slot /></span>', props: ['title'] },
  'a-tabs': { template: '<div><slot /></div>', props: ['activeKey'] },
  'a-tab-pane': { template: '<div><slot /></div>', props: ['key', 'tab'] },
  'a-divider': { template: '<hr />', props: ['orientation'] },
  'a-popconfirm': { template: '<span><slot /></span>', props: ['title', 'okText', 'cancelText'] },
  'a-list': {
    template: '<div class="a-list"><slot name="renderItem" v-for="item in dataSource" :item="item" :key="item.id || item.provider_thread_id || item.link_id || item.subject" /></div>',
    props: ['dataSource', 'size', 'locale'],
  },
  'a-list-item': { template: '<div class="a-list-item"><slot /><slot name="actions" /></div>' },
  'a-list-item-meta': { template: '<div class="a-list-item-meta"><div><slot name="title" /></div><div><slot name="description" /></div></div>' },
  'a-statistic': { template: '<div>{{ title }} {{ value }}</div>', props: ['title', 'value', 'valueStyle'] },
  'a-upload': { template: '<div><slot /></div>', props: ['fileList', 'beforeUpload', 'maxCount', 'accept'] },
  'a-typography-paragraph': { template: '<p><slot /></p>' },
  'a-typography-text': { template: '<span><slot /></span>', props: ['type'] },
  'arrow-left-outlined': { template: '<span />' },
  'arrow-right-outlined': { template: '<span />' },
  'down-outlined': { template: '<span />' },
  'star-outlined': { template: '<span />' },
  'star-filled': { template: '<span />' },
  'close-circle-outlined': { template: '<span />' },
  'clock-circle-outlined': { template: '<span />' },
  'reload-outlined': { template: '<span />' },
  'search-outlined': { template: '<span />' },
  'environment-outlined': { template: '<span />' },
  'inbox-outlined': { template: '<span />' },
  'link-outlined': { template: '<span />' },
  'thunderbolt-outlined': { template: '<span />' },
  'plus-outlined': { template: '<span />' },
  'upload-outlined': { template: '<span />' },
  'package-editor': { template: '<div class="package-editor" />', props: ['applicationId'] },
  'submission-preview': { template: '<div class="submission-preview" />', props: ['applicationId'] },
  'system-send-status-badge': { template: '<div class="send-badge" />', props: ['status'] },
}

// ---- Shared fixtures ----

const APP_FAVORITED = {
  id: 'app-1',
  canonical_job_id: 'job-1',
  state: 'favorited',
  submission_channel: null,
  package_version_id: null,
  payload_hash: null,
  apply_url: 'https://example.com/apply',
  cycle_id: 'cycle-1',
  version: 1,
  submitted_at: null,
}

const APP_PREPARING = {
  ...APP_FAVORITED,
  state: 'preparing',
}

const APP_SUBMITTED = {
  ...APP_FAVORITED,
  state: 'submitted',
  submission_channel: 'manual',
  submitted_at: '2025-06-01T10:00:00Z',
}

beforeEach(() => {
  vi.clearAllMocks()
  mockGetChannels.mockResolvedValue({ channels: [] })
  mockGetTimeline.mockResolvedValue({ items: [] })
  mockGetExcludedReasons.mockResolvedValue({ reasons: [] })
  mockGetAccount.mockResolvedValue({
    connected: false,
    connection_state: 'disconnected',
    sync_available: false,
  })
  mockListThreads.mockResolvedValue({ items: [], next_cursor: null })
  mockListUnresolved.mockResolvedValue({ items: [] })
  mockListSyncHistory.mockResolvedValue({ items: [] })
})

// ============================================================
// 14.5 — Application timeline rendering: status distinctions
// ============================================================

describe('ApplicationWorkspace — timeline status rendering (14.5)', () => {
  const TIMELINE_ITEMS = [
    { id: 't1', kind: 'state_change', title: '收藏', status: 'confirmed', occurred_at: '2025-05-01T10:00:00Z', from_state: 'none', to_state: 'favorited' },
    { id: 't2', kind: 'package', title: '投递包建议', status: 'proposed', occurred_at: '2025-05-02T10:00:00Z' },
    { id: 't3', kind: 'proposal', title: '邮件事件建议', status: 'rejected', occurred_at: '2025-05-03T10:00:00Z' },
    { id: 't4', kind: 'send', title: '系统发送', status: 'pending', occurred_at: '2025-05-04T10:00:00Z' },
    { id: 't5', kind: 'send', title: '发送失败', status: 'failed', occurred_at: '2025-05-05T10:00:00Z' },
    { id: 't6', kind: 'reconciliation', title: '需人工对账', status: 'reconciliation_required', occurred_at: '2025-05-06T10:00:00Z' },
  ]

  it('renders all six timeline status types', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_PREPARING })
    mockGetTimeline.mockResolvedValue({ items: TIMELINE_ITEMS })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    const timelineItems = w.findAll('.a-timeline-item')
    expect(timelineItems.length).toBe(6)

    expect(w.text()).toContain('已确认')
    expect(w.text()).toContain('建议')
    expect(w.text()).toContain('已驳回')
    expect(w.text()).toContain('待处理')
    expect(w.text()).toContain('失败')
    expect(w.text()).toContain('需人工对账')
  })

  it('sorts timeline items chronologically', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_PREPARING })
    mockGetTimeline.mockResolvedValue({
      items: [...TIMELINE_ITEMS].reverse(),
    })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    expect(w.vm.timeline[0].id).toBe('t1')
    expect(w.vm.timeline[5].id).toBe('t6')
  })

  it('handles empty timeline', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_PREPARING })
    mockGetTimeline.mockResolvedValue({ items: [] })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('暂无事件记录')
  })
})

// ============================================================
// 14.4 — "Why recommended" evidence + exclusion reasons
// ============================================================

describe('InboxDetail — recommendation evidence + exclusion reasons (14.4)', () => {
  const RECOMMENDED_JOB = {
    canonical_job_id: 'job-rec-1',
    title: 'Staff Engineer',
    company: 'Acme Corp',
    verdict: 'recommended',
    application_state: 'none',
    application_id: null,
    description: '<p>Build distributed systems</p>',
    match_results: [
      { requirement: 'Distributed systems', level: 'strong', reason: '5 years experience', evidence_refs: ['ev-1'] },
      { requirement: 'Python', level: 'partial', reason: 'Some experience', evidence_refs: ['ev-2'] },
      { requirement: 'Kubernetes', level: 'unsupported', reason: 'No evidence' },
    ],
    source_name: 'Greenhouse',
    source_type: 'official_careers',
    crawl_run_id: 'run-abc12345',
    plan_version_id: 'plan-xyz12345',
    captured_at: '2025-06-01T10:00:00Z',
  }

  it('renders match results with levels and evidence references', async () => {
    mockGetInboxJobDetail.mockResolvedValue({ ...RECOMMENDED_JOB })

    const { default: InboxDetail } = await import('../src/views/InboxDetail.vue')
    const w = mount(InboxDetail, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('要求匹配')
    // Match level labels rendered via bodyCell override
    expect(w.text()).toContain('完全匹配')
    expect(w.text()).toContain('部分匹配')
    expect(w.text()).toContain('不满足')
    // Evidence tags rendered via bodyCell override
    expect(w.text()).toContain('ev-1')
    expect(w.text()).toContain('ev-2')
    // Data is present in the component
    expect(w.vm.matchResults.length).toBe(3)
  })

  it('renders provenance information (source, run, capture time)', async () => {
    mockGetInboxJobDetail.mockResolvedValue({ ...RECOMMENDED_JOB })

    const { default: InboxDetail } = await import('../src/views/InboxDetail.vue')
    const w = mount(InboxDetail, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('Greenhouse')
    expect(w.text()).toContain('run-abc')
  })

  it('renders excluded reasons with evidence for excluded jobs', async () => {
    const excludedJob = {
      ...RECOMMENDED_JOB,
      verdict: 'excluded',
      blocking_reasons: ['Location mismatch', 'Visa requirement'],
    }
    mockGetInboxJobDetail.mockResolvedValue(excludedJob)
    mockGetExcludedReasons.mockResolvedValue({
      reasons: [
        { rule: 'location_filter', message: 'Candidate location does not match job requirement', evidence_refs: ['ev-loc-1'] },
        { rule: 'authorization', message: 'Visa sponsorship required but not available' },
      ],
    })

    const { default: InboxDetail } = await import('../src/views/InboxDetail.vue')
    const w = mount(InboxDetail, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('排除原因')
    expect(w.text()).toContain('location_filter')
    expect(w.text()).toContain('authorization')
  })

  it('handles empty match results gracefully', async () => {
    mockGetInboxJobDetail.mockResolvedValue({
      ...RECOMMENDED_JOB,
      match_results: [],
    })

    const { default: InboxDetail } = await import('../src/views/InboxDetail.vue')
    const w = mount(InboxDetail, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('暂无匹配结果')
  })
})

// ============================================================
// 14.8 — Confirmation behavior (Section 9/10)
// ============================================================

describe('ApplicationWorkspace — confirmation behavior (14.8)', () => {
  it('transitions from favorited to preparing on prepare action', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_FAVORITED })
    mockPrepareApplication.mockResolvedValue({ ...APP_PREPARING })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('开始准备申请材料')

    await w.vm.onPrepare()
    await flushPromises()

    expect(mockPrepareApplication).toHaveBeenCalledWith('app-1')
    expect(w.vm.app.state).toBe('preparing')
  })

  it('surfaces 503 from prepare as dependency-not-ready error', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_FAVORITED })
    mockPrepareApplication.mockRejectedValue({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
      messageText: 'application service not available',
    })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    await w.vm.onPrepare()
    await flushPromises()

    expect(w.vm.actionError).toContain('暂不可用')
  })

  it('validates external_form submission requires apply_url', async () => {
    mockGetApplication.mockResolvedValue({
      ...APP_PREPARING,
      apply_url: '',
      submission_channel: 'external_form',
    })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    w.vm.submissionForm.apply_url = ''
    await w.vm.onConfirmSubmission()
    await flushPromises()

    expect(w.vm.submissionFormError).toContain('申请链接')
  })

  it('confirms external submission with note', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_PREPARING, apply_url: 'https://example.com' })
    mockConfirmExternal.mockResolvedValue({ ...APP_SUBMITTED })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    w.vm.submissionForm.apply_url = 'https://example.com'
    w.vm.submissionForm.note = 'Submitted via official form'

    await w.vm.onConfirmSubmission()
    await flushPromises()

    expect(mockConfirmExternal).toHaveBeenCalledWith('app-1', expect.objectContaining({
      channel: 'external_form',
      apply_url: 'https://example.com',
      note: 'Submitted via official form',
    }))
  })

  it('renders 404 state when application not found', async () => {
    mockGetApplication.mockRejectedValue({ status: 404, message: 'Not found' })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    expect(w.vm.notFound).toBe(true)
    expect(w.text()).toContain('申请不存在')
  })

  it('renders 503 unavailable state', async () => {
    mockGetApplication.mockRejectedValue({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
      messageText: 'application repo missing',
    })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    expect(w.vm.unavailable).toBe(true)
    expect(w.text()).toContain('服务暂不可用')
  })
})

// ============================================================
// 14.8 — Thread review (Section 13: ReplyReviewQueue)
// ============================================================

describe('ReplyReviewQueue — thread review (14.8)', () => {
  const DRAFT_LOW_RISK = {
    id: 'draft-1',
    subject: 'Re: Interview scheduling',
    body: 'Thank you for the opportunity...',
    recipient: 'recruiter@acme.com',
    application_id: 'app-1',
    risk_category: 'low_risk',
    approval_state: 'pending_review',
    intent: 'schedule_interview',
    version_number: 1,
    payload_hash: 'abc123def456',
    validation_issues: [],
    context: {
      thread_excerpt: 'We would like to schedule an interview...',
      evidence_refs: ['ev-1', 'ev-2'],
    },
  }

  const DRAFT_HIGH_RISK = {
    ...DRAFT_LOW_RISK,
    id: 'draft-2',
    risk_category: 'high_risk',
    validation_issues: ['Unsupported salary claim'],
  }

  it('renders draft list with risk and state tags', async () => {
    mockListDrafts.mockResolvedValue({
      items: [DRAFT_LOW_RISK, DRAFT_HIGH_RISK],
      next_cursor: null,
    })

    const { default: ReplyReviewQueue } = await import('../src/views/ReplyReviewQueue.vue')
    const w = mount(ReplyReviewQueue, { global: { stubs } })
    await flushPromises()

    // Draft list is rendered (subjects + recipients visible)
    expect(w.text()).toContain('Re: Interview scheduling')
    expect(w.text()).toContain('recruiter@acme.com')
    // Risk categories are rendered via riskLabel/riskColor
    expect(w.text()).toContain('低风险')
    expect(w.text()).toContain('高风险')
  })

  it('selects a draft and shows detail panel with context', async () => {
    mockListDrafts.mockResolvedValue({
      items: [DRAFT_LOW_RISK],
      next_cursor: null,
    })

    const { default: ReplyReviewQueue } = await import('../src/views/ReplyReviewQueue.vue')
    const w = mount(ReplyReviewQueue, { global: { stubs } })
    await flushPromises()

    w.vm.select(DRAFT_LOW_RISK)
    await w.vm.$nextTick()

    // Detail panel shows context
    expect(w.text()).toContain('recruiter@acme.com')
    expect(w.text()).toContain('schedule_interview')
    expect(w.text()).toContain('We would like to schedule an interview')
    expect(w.text()).toContain('2 项已确认证据')
  })

  it('disables approve when validation issues exist and shows them', async () => {
    mockListDrafts.mockResolvedValue({
      items: [DRAFT_HIGH_RISK],
      next_cursor: null,
    })

    const { default: ReplyReviewQueue } = await import('../src/views/ReplyReviewQueue.vue')
    const w = mount(ReplyReviewQueue, { global: { stubs } })
    await flushPromises()

    w.vm.select(DRAFT_HIGH_RISK)
    await w.vm.$nextTick()

    expect(w.text()).toContain('Unsupported salary claim')
  })

  it('enables send for approved low-risk draft', async () => {
    const approved = { ...DRAFT_LOW_RISK, approval_state: 'approved' }
    mockListDrafts.mockResolvedValue({
      items: [approved],
      next_cursor: null,
    })

    const { default: ReplyReviewQueue } = await import('../src/views/ReplyReviewQueue.vue')
    const w = mount(ReplyReviewQueue, { global: { stubs } })
    await flushPromises()

    w.vm.select(approved)
    await w.vm.$nextTick()

    expect(w.vm.canSend).toBe(true)
  })

  it('calls approve API and refreshes list', async () => {
    mockListDrafts.mockResolvedValue({
      items: [DRAFT_LOW_RISK],
      next_cursor: null,
    })
    mockApproveDraft.mockResolvedValue({ ...DRAFT_LOW_RISK, approval_state: 'approved' })

    const { default: ReplyReviewQueue } = await import('../src/views/ReplyReviewQueue.vue')
    const w = mount(ReplyReviewQueue, { global: { stubs } })
    await flushPromises()

    w.vm.select(DRAFT_LOW_RISK)
    await w.vm.approve()
    await flushPromises()

    expect(mockApproveDraft).toHaveBeenCalledWith('draft-1')
    expect(mockListDrafts).toHaveBeenCalledTimes(2)
  })

  it('calls reject API', async () => {
    mockListDrafts.mockResolvedValue({
      items: [DRAFT_LOW_RISK],
      next_cursor: null,
    })
    mockRejectDraft.mockResolvedValue({ ...DRAFT_LOW_RISK, approval_state: 'rejected' })

    const { default: ReplyReviewQueue } = await import('../src/views/ReplyReviewQueue.vue')
    const w = mount(ReplyReviewQueue, { global: { stubs } })
    await flushPromises()

    w.vm.select(DRAFT_LOW_RISK)
    await w.vm.reject()
    await flushPromises()

    expect(mockRejectDraft).toHaveBeenCalledWith('draft-1')
  })

  it('opens edit modal and submits edit', async () => {
    mockListDrafts.mockResolvedValue({
      items: [DRAFT_LOW_RISK],
      next_cursor: null,
    })
    mockEditDraft.mockResolvedValue({
      ...DRAFT_LOW_RISK,
      subject: 'Updated subject',
      body: 'Updated body',
      version_number: 2,
    })

    const { default: ReplyReviewQueue } = await import('../src/views/ReplyReviewQueue.vue')
    const w = mount(ReplyReviewQueue, { global: { stubs } })
    await flushPromises()

    w.vm.select(DRAFT_LOW_RISK)
    w.vm.editDraft()

    expect(w.vm.editForm.subject).toBe('Re: Interview scheduling')
    expect(w.vm.editForm.body).toBe('Thank you for the opportunity...')
    expect(w.vm.editing).toBe(true)

    w.vm.editForm.subject = 'Updated subject'
    w.vm.editForm.body = 'Updated body'
    await w.vm.submitEdit()
    await flushPromises()

    expect(mockEditDraft).toHaveBeenCalledWith('draft-1', {
      subject: 'Updated subject',
      body: 'Updated body',
    })
  })

  it('surfaces 503 as dependency-not-ready', async () => {
    mockListDrafts.mockRejectedValue({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
      messageText: 'draft service down',
    })

    const { default: ReplyReviewQueue } = await import('../src/views/ReplyReviewQueue.vue')
    const w = mount(ReplyReviewQueue, { global: { stubs } })
    await flushPromises()

    expect(w.vm.unavailable).toBe(true)
    expect(w.text()).toContain('暂不可用')
  })

  it('loads more drafts with cursor pagination', async () => {
    mockListDrafts.mockResolvedValueOnce({
      items: [DRAFT_LOW_RISK],
      next_cursor: 'cursor-2',
    })
    mockListDrafts.mockResolvedValueOnce({
      items: [{ ...DRAFT_LOW_RISK, id: 'draft-3' }],
      next_cursor: null,
    })

    const { default: ReplyReviewQueue } = await import('../src/views/ReplyReviewQueue.vue')
    const w = mount(ReplyReviewQueue, { global: { stubs } })
    await flushPromises()

    expect(w.vm.drafts.length).toBe(1)

    await w.vm.loadMore()
    await flushPromises()

    expect(w.vm.drafts.length).toBe(2)
    expect(w.vm.nextCursor).toBeNull()
  })

  it('filters by state', async () => {
    mockListDrafts.mockResolvedValue({
      items: [DRAFT_LOW_RISK],
      next_cursor: null,
    })

    const { default: ReplyReviewQueue } = await import('../src/views/ReplyReviewQueue.vue')
    const w = mount(ReplyReviewQueue, { global: { stubs } })
    await flushPromises()

    w.vm.stateFilter = 'approved'
    await w.vm.refresh()
    await flushPromises()

    expect(mockListDrafts).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: 'approved', limit: 20 }),
    )
  })
})

// ============================================================
// 14.8 — Mail follow-up: thread summaries + sync status
// ============================================================

describe('MailFollowUp — thread summaries + sync states (14.8)', () => {
  it('renders thread summaries when connected', async () => {
    mockGetAccount.mockResolvedValue({
      connected: true,
      connection_state: 'connected',
      sync_available: true,
      email_address: 'user@example.com',
      last_sync_at: new Date().toISOString(),
    })
    mockListThreads.mockResolvedValue({
      items: [
        { provider_thread_id: 't-1', subject: 'Interview invitation', application_id: 'app-1' },
        { provider_thread_id: 't-2', subject: 'Re: Application status' },
      ],
      next_cursor: null,
    })
    mockListUnresolved.mockResolvedValue({ items: [] })
    mockListSyncHistory.mockResolvedValue({ items: [] })

    const { default: MailFollowUp } = await import('../src/views/MailFollowUp.vue')
    const w = mount(MailFollowUp, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('Interview invitation')
    expect(w.text()).toContain('Re: Application status')
  })

  it('shows sync denied state on 403', async () => {
    mockGetAccount.mockRejectedValue({
      status: 403,
      code: 'DENIED_POLICY',
      messageText: 'GMAIL_READ denied',
    })

    const { default: MailFollowUp } = await import('../src/views/MailFollowUp.vue')
    const w = mount(MailFollowUp, { global: { stubs } })
    await flushPromises()

    expect(w.vm.syncDenied).toBe(true)
    expect(w.text()).toContain('同步尚未启用')
  })

  it('shows unavailable on 503', async () => {
    mockGetAccount.mockRejectedValue({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
    })

    const { default: MailFollowUp } = await import('../src/views/MailFollowUp.vue')
    const w = mount(MailFollowUp, { global: { stubs } })
    await flushPromises()

    expect(w.vm.unavailable).toBe(true)
    expect(w.text()).toContain('暂不可用')
  })

  it('shows stale notice when sync is old', async () => {
    const sixHoursAgo = new Date(Date.now() - 7 * 60 * 60 * 1000).toISOString()
    mockGetAccount.mockResolvedValue({
      connected: true,
      connection_state: 'connected',
      sync_available: true,
      email_address: 'user@example.com',
      last_sync_at: sixHoursAgo,
    })

    const { default: MailFollowUp } = await import('../src/views/MailFollowUp.vue')
    const w = mount(MailFollowUp, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('数据可能已过时')
  })

  it('shows error notice when connection_state is error', async () => {
    mockGetAccount.mockResolvedValue({
      connected: true,
      connection_state: 'error',
      sync_available: false,
      email_address: 'user@example.com',
      last_sync_at: new Date().toISOString(),
      last_error_code: 'TOKEN_REVOKED',
    })

    const { default: MailFollowUp } = await import('../src/views/MailFollowUp.vue')
    const w = mount(MailFollowUp, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('同步出错')
    expect(w.text()).toContain('TOKEN_REVOKED')
  })

  it('renders unresolved links with candidate applications', async () => {
    mockGetAccount.mockResolvedValue({
      connected: true,
      connection_state: 'connected',
      sync_available: true,
    })
    mockListUnresolved.mockResolvedValue({
      items: [{
        link_id: 'link-1',
        subject: 'Fwd: Job offer',
        candidate_application_ids: ['app-1', 'app-2'],
        evidence_refs: { sender: 'hr@acme.com' },
      }],
    })

    const { default: MailFollowUp } = await import('../src/views/MailFollowUp.vue')
    const w = mount(MailFollowUp, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('Fwd: Job offer')
    expect(w.text()).toContain('2 个申请')
  })
})

// ============================================================
// 14.8 — Channel selection behavior
// ============================================================

describe('ApplicationWorkspace — channel selection (14.8)', () => {
  it('selects a channel and updates app state', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_PREPARING })
    mockSelectChannel.mockResolvedValue({ ...APP_PREPARING, submission_channel: 'manual' })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    await w.vm.onSelectChannel('manual')
    await flushPromises()

    expect(mockSelectChannel).toHaveBeenCalledWith('app-1', 'manual')
    expect(w.vm.app.submission_channel).toBe('manual')
  })

  it('surfaces 503 from channel selection', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_PREPARING })
    mockSelectChannel.mockRejectedValue({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
    })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    await w.vm.onSelectChannel('manual')
    await flushPromises()

    expect(w.vm.channelError).toContain('暂不可用')
  })
})

// ============================================================
// 14.8 — State transition behavior
// ============================================================

describe('ApplicationWorkspace — state transitions (14.8)', () => {
  it('changes state to on_hold via menu action', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_PREPARING })
    mockChangeState.mockResolvedValue({ ...APP_PREPARING, state: 'on_hold' })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    await w.vm.onStateMenu({ key: 'on_hold' })
    await flushPromises()

    expect(mockChangeState).toHaveBeenCalledWith('app-1', expect.objectContaining({
      to_state: 'on_hold',
    }))
  })

  it('changes state to withdrawn', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_PREPARING })
    mockChangeState.mockResolvedValue({ ...APP_PREPARING, state: 'withdrawn' })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    await w.vm.onStateMenu({ key: 'withdrawn' })
    await flushPromises()

    expect(mockChangeState).toHaveBeenCalledWith('app-1', expect.objectContaining({
      to_state: 'withdrawn',
    }))
  })

  it('surfaces 503 from state change', async () => {
    mockGetApplication.mockResolvedValue({ ...APP_PREPARING })
    mockChangeState.mockRejectedValue({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
    })

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    await flushPromises()

    await w.vm.onStateMenu({ key: 'on_hold' })
    await flushPromises()

    expect(w.vm.actionError).toContain('暂不可用')
  })
})

// ============================================================
// 14.8 — parseApiError + dependency classification
// ============================================================

describe('parseApiError — dependency-not-ready classification (14.2)', () => {
  it('flags HTTP 503 as dependency-not-ready', async () => {
    const { parseApiError } = await import('../src/api/client.js')
    expect(parseApiError({ status: 503, message: 'boom' }).isDependencyNotReady).toBe(true)
  })

  it('flags DEPENDENCY_NOT_READY code', async () => {
    const { parseApiError } = await import('../src/api/client.js')
    expect(parseApiError({ status: 500, code: 'DEPENDENCY_NOT_READY' }).isDependencyNotReady).toBe(true)
  })

  it('flags UNAVAILABLE_DEPENDENCY code', async () => {
    const { parseApiError } = await import('../src/api/client.js')
    expect(parseApiError({ status: 500, code: 'UNAVAILABLE_DEPENDENCY' }).isDependencyNotReady).toBe(true)
  })

  it('does not flag 404', async () => {
    const { parseApiError } = await import('../src/api/client.js')
    expect(parseApiError({ status: 404 }).isDependencyNotReady).toBe(false)
  })

  it('does not flag 409 INVALID_STATE', async () => {
    const { parseApiError } = await import('../src/api/client.js')
    expect(parseApiError({ status: 409, code: 'INVALID_STATE' }).isDependencyNotReady).toBe(false)
  })

  it('carries retryable and details through', async () => {
    const { parseApiError } = await import('../src/api/client.js')
    const parsed = parseApiError({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
      messageText: 'repo missing',
      details: { which: 'inbox_repository' },
      retryable: true,
    })
    expect(parsed.retryable).toBe(true)
    expect(parsed.details).toEqual({ which: 'inbox_repository' })
    expect(parsed.message).toBe('repo missing')
  })
})

// ============================================================
// 14.6 — Loading/empty states
// ============================================================

describe('ApplicationWorkspace — loading state (14.6)', () => {
  it('shows loading spinner initially', async () => {
    mockGetApplication.mockReturnValue(new Promise(() => {}))

    const { default: ApplicationWorkspace } = await import('../src/views/ApplicationWorkspace.vue')
    const w = mount(ApplicationWorkspace, { global: { stubs } })
    expect(w.vm.loading).toBe(true)
  })
})

describe('InboxDetail — states (14.6)', () => {
  it('shows loading state when fetching', async () => {
    mockGetInboxJobDetail.mockReturnValue(new Promise(() => {}))

    const { default: InboxDetail } = await import('../src/views/InboxDetail.vue')
    const w = mount(InboxDetail, { global: { stubs } })
    expect(w.vm.loading).toBe(true)
  })

  it('shows 404 when job not found', async () => {
    mockGetInboxJobDetail.mockRejectedValue({ status: 404, message: 'Not found' })

    const { default: InboxDetail } = await import('../src/views/InboxDetail.vue')
    const w = mount(InboxDetail, { global: { stubs } })
    await flushPromises()

    expect(w.text()).toContain('职位未找到')
  })

  it('shows dependency-not-ready on 503', async () => {
    mockGetInboxJobDetail.mockRejectedValue({
      status: 503,
      code: 'DEPENDENCY_NOT_READY',
      messageText: 'inbox service not ready',
    })

    const { default: InboxDetail } = await import('../src/views/InboxDetail.vue')
    const w = mount(InboxDetail, { global: { stubs } })
    await flushPromises()

    expect(w.vm.error).toContain('未就绪')
  })
})
