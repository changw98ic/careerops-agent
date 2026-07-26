<template>
  <div v-if="app" class="page-shell">
    <!-- Header -->
    <div class="detail-heading">
      <div class="detail-title">
        <a-button type="text" @click="router.push('/applications')">
          <template #icon><ArrowLeftOutlined /></template>
          返回投递列表
        </a-button>
        <h1>申请工作区</h1>
        <a-tag :color="stateColor(app.state)">{{ stateLabel(app.state) }}</a-tag>
        <span class="mono muted">job {{ shortId(app.canonical_job_id) }}</span>
      </div>
      <div class="detail-actions">
        <a-dropdown>
          <a-button>
            更多操作
            <template #icon><DownOutlined /></template>
          </a-button>
          <template #overlay>
            <a-menu @click="onStateMenu">
              <a-menu-item key="on_hold">设为暂停 (On hold)</a-menu-item>
              <a-menu-item key="withdrawn">撤回申请 (Withdraw)</a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
      </div>
    </div>

    <a-alert v-if="error" type="error" show-icon :message="error" closable @close="error = ''" />

    <a-row :gutter="[24, 24]">
      <!-- Left column: job evidence + channel + package -->
      <a-col :xs="24" :lg="10">
        <!-- Job evidence -->
        <a-card title="职位证据" class="detail-card">
          <a-descriptions :column="1" size="small" :label-style="{ color: '#667085', width: '90px' }">
            <a-descriptions-item label="Canonical Job">
              <span class="mono">{{ shortId(app.canonical_job_id) }}</span>
            </a-descriptions-item>
            <a-descriptions-item label="申请链接">
              <a v-if="app.apply_url" :href="app.apply_url" target="_blank" rel="noreferrer" class="table-link">
                打开官方链接 ↗
              </a>
              <span v-else class="muted">未提供</span>
            </a-descriptions-item>
            <a-descriptions-item v-if="app.cycle_id" label="Cycle">
              <span class="mono">{{ shortId(app.cycle_id) }}</span>
            </a-descriptions-item>
            <a-descriptions-item v-if="app.version != null" label="版本">
              {{ app.version }}
            </a-descriptions-item>
          </a-descriptions>
          <a-alert
            v-if="app.apply_url"
            type="info"
            show-icon
            banner
            message="官方链接由来源采集进入。请仅在此域名内完成外部表单。"
            style="margin-top: 12px"
          />
        </a-card>

        <!-- Channel selection -->
        <a-card title="投递渠道" class="detail-card" style="margin-top: 16px">
          <a-alert
            v-if="channelError"
            type="error"
            show-icon
            :message="channelError"
            closable
            @close="channelError = ''"
            style="margin-bottom: 12px"
          />
          <a-spin :spinning="channelsLoading">
            <div v-if="channels.length > 0" class="channel-list">
              <div
                v-for="ch in channels"
                :key="ch.channel"
                class="channel-card"
                :class="{
                  'channel-card--active': app.submission_channel === ch.channel,
                  'channel-card--disabled': !ch.eligible,
                }"
              >
                <div class="channel-card__head">
                  <span class="channel-card__name">{{ channelLabel(ch.channel) }}</span>
                  <a-tag :color="ch.eligible ? 'green' : 'default'" size="small">
                    {{ ch.eligible ? '可用' : '不可用' }}
                  </a-tag>
                  <a-tag
                    v-if="app.submission_channel === ch.channel"
                    color="blue"
                    size="small"
                  >
                    已选
                  </a-tag>
                </div>
                <p class="channel-card__reason muted">
                  {{ ch.reason || (ch.eligible ? '符合投递条件' : '不满足条件') }}
                </p>
                <div v-if="ch.channel === 'external_form' && app.apply_url" class="channel-card__url">
                  <a :href="app.apply_url" target="_blank" rel="noreferrer" class="table-link">
                    {{ truncateUrl(app.apply_url) }}
                  </a>
                </div>
                <div class="channel-card__action">
                  <a-button
                    v-if="ch.eligible && app.submission_channel !== ch.channel"
                    size="small"
                    type="primary"
                    ghost
                    :disabled="!canSelectChannel"
                    :loading="actionLoading === 'channel-' + ch.channel"
                    @click="onSelectChannel(ch.channel)"
                  >
                    选择此渠道
                  </a-button>
                  <span v-else-if="ch.channel === 'email'" class="muted" style="font-size: 12px">
                    延后开放 — 将在后续版本提供可信联系人解析。
                  </span>
                  <span v-else-if="!ch.eligible" class="muted" style="font-size: 12px">
                    {{ ch.reason === 'contact_resolution_deferred' ? '延后开放' : '当前不可用' }}
                  </span>
                </div>
              </div>
            </div>
            <a-empty v-else description="暂无渠道信息" />
          </a-spin>
        </a-card>

        <!-- Package binding summary (the full editor lives below the row) -->
        <a-card title="投递包状态" class="detail-card" style="margin-top: 16px">
          <template v-if="app.package_version_id || app.payload_hash">
            <a-descriptions :column="1" size="small" :label-style="{ color: '#667085', width: '110px' }">
              <a-descriptions-item v-if="app.package_version_id" label="Package 版本">
                <span class="mono">{{ shortId(app.package_version_id) }}</span>
              </a-descriptions-item>
              <a-descriptions-item v-if="app.payload_hash" label="Payload Hash">
                <span class="mono">{{ truncateHash(app.payload_hash) }}</span>
              </a-descriptions-item>
              <a-descriptions-item v-if="app.submission_channel" label="投递渠道">
                {{ channelLabel(app.submission_channel) }}
              </a-descriptions-item>
            </a-descriptions>
            <a-button size="small" type="link" @click="scrollToPackageEditor">
              打开包编辑器 ↓
            </a-button>
          </template>
          <a-empty v-else description="尚未绑定投递包 — 在下方包编辑器中创建首版草稿。" />
        </a-card>
      </a-col>

      <!-- Right column: primary action + submission + timeline -->
      <a-col :xs="24" :lg="14">
        <!-- Primary action -->
        <a-card class="detail-card action-card">
          <div class="action-content">
            <div>
              <h3 style="margin: 0 0 4px">{{ primaryActionTitle }}</h3>
              <p class="muted" style="margin: 0">{{ primaryActionHint }}</p>
            </div>
            <a-space>
              <a-button
                v-if="isState(app.state, 'favorited')"
                type="primary"
                size="large"
                :loading="actionLoading === 'prepare'"
                @click="onPrepare"
              >
                开始准备
                <template #icon><ArrowRightOutlined /></template>
              </a-button>
              <a-button
                v-else-if="isState(app.state, 'preparing')"
                type="primary"
                size="large"
                @click="scrollToSubmission"
              >
                前往投递确认
                <template #icon><ArrowRightOutlined /></template>
              </a-button>
              <a-tag v-else-if="isTerminal(app.state)" color="green">
                已于 {{ formatDateTime(app.submitted_at) }} 投递
              </a-tag>
              <a-button v-else disabled size="large">无可执行操作</a-button>
            </a-space>
          </div>
          <a-alert
            v-if="actionError"
            type="error"
            show-icon
            :message="actionError"
            closable
            @close="actionError = ''"
            style="margin-top: 12px"
          />
        </a-card>

        <!-- Submission confirmation -->
        <a-card
          v-if="showSubmissionPanel"
          ref="submissionRef"
          title="投递确认"
          class="detail-card"
          style="margin-top: 16px"
        >
          <a-alert
            type="info"
            show-icon
            banner
            message="系统不会自动标记为已投递。请在官方渠道完成提交后，于此处明确确认。"
            style="margin-bottom: 16px"
          />
          <a-form layout="vertical">
            <a-form-item label="投递渠道">
              <a-tag :color="submissionChannel === 'manual' ? 'gold' : 'blue'">
                {{ channelLabel(submissionChannel) }}
              </a-tag>
            </a-form-item>
            <a-form-item
              v-if="submissionChannel === 'external_form'"
              label="申请链接（必填）"
              :validate-status="submissionFormError ? 'error' : ''"
              :help="submissionFormError"
            >
              <a-input v-model:value="submissionForm.apply_url" placeholder="https://..." />
            </a-form-item>
            <a-form-item label="投递时间">
              <a-date-picker
                v-model:value="submissionForm.submitted_at"
                show-time
                format="YYYY-MM-DD HH:mm"
                style="width: 100%"
                placeholder="默认为当前时间"
              />
            </a-form-item>
            <a-form-item label="备注（可选）">
              <a-textarea
                v-model:value="submissionForm.note"
                :rows="2"
                placeholder="例如：在官方表单完成提交，收到确认页。"
              />
            </a-form-item>
            <a-button
              type="primary"
              :loading="actionLoading === 'confirm'"
              :disabled="submissionChannel === 'external_form' && !submissionForm.apply_url"
              @click="onConfirmSubmission"
            >
              确认已完成投递
            </a-button>
          </a-form>
        </a-card>

        <!-- Timeline -->
        <a-card title="事件时间线" class="detail-card" style="margin-top: 16px">
          <a-alert
            v-if="timelineError"
            type="warning"
            show-icon
            :message="timelineError"
            closable
            @close="timelineError = ''"
            style="margin-bottom: 12px"
          />
          <a-spin :spinning="timelineLoading">
            <template v-if="timeline.length > 0">
              <a-timeline>
                <a-timeline-item
                  v-for="item in timeline"
                  :key="item.id"
                  :color="timelineColor(item.status)"
                >
                  <div class="timeline-item">
                    <div class="timeline-item__head">
                      <span class="timeline-item__title">{{ item.title || item.kind }}</span>
                      <a-tag :color="statusTagColor(item.status)" size="small">
                        {{ statusLabel(item.status) }}
                      </a-tag>
                    </div>
                    <p v-if="item.description" class="timeline-item__desc">{{ item.description }}</p>
                    <div class="timeline-item__meta">
                      <span class="muted">{{ formatDateTime(item.occurred_at) }}</span>
                      <span v-if="item.source" class="muted">· 来源 {{ item.source }}</span>
                    </div>
                    <div v-if="item.from_state || item.to_state" class="timeline-item__transition">
                      <a-tag size="small">{{ stateLabel(item.from_state) }}</a-tag>
                      <ArrowRightOutlined style="font-size: 10px; color: #98a2b3" />
                      <a-tag size="small">{{ stateLabel(item.to_state) }}</a-tag>
                    </div>
                    <div v-if="item.evidence_refs?.length" class="timeline-item__evidence">
                      <span class="muted">证据: </span>
                      <a-tag
                        v-for="(ref, ri) in item.evidence_refs"
                        :key="ri"
                        size="small"
                        color="default"
                      >
                        {{ truncateUrl(ref) }}
                      </a-tag>
                    </div>
                  </div>
                </a-timeline-item>
              </a-timeline>
            </template>
            <a-empty v-else description="暂无事件记录" />
          </a-spin>
        </a-card>
      </a-col>
    </a-row>

    <!-- Full-width package editor (Section 8) -->
    <div ref="packageEditorRef">
      <PackageEditor
        :application-id="appId"
        @approved="onPackageApproved"
        @created="onPackageCreated"
      />
    </div>
  </div>

  <!-- Loading state -->
  <div v-else-if="loading" class="page-loading">
    <a-spin size="large" />
    <span>正在加载申请工作区…</span>
  </div>

  <!-- Service unavailable (503) -->
  <div v-else-if="unavailable" class="page-shell">
    <a-result status="warning" title="服务暂不可用" sub-title="申请服务依赖未就绪（503），请稍后重试。">
      <template #extra>
        <a-button type="primary" :loading="loading" @click="loadAll">重试</a-button>
        <a-button @click="router.push('/applications')">返回投递列表</a-button>
      </template>
    </a-result>
  </div>

  <!-- Not found (404) -->
  <a-result
    v-else-if="notFound"
    status="404"
    title="申请不存在"
    sub-title="该申请可能已被移除或不属于当前用户。"
  >
    <template #extra>
      <a-button type="primary" @click="router.push('/applications')">返回投递列表</a-button>
    </template>
  </a-result>

  <!-- Generic error -->
  <a-result v-else-if="error" status="error" title="加载失败" :sub-title="error">
    <template #extra>
      <a-button type="primary" @click="loadAll">重新加载</a-button>
      <a-button @click="router.push('/applications')">返回投递列表</a-button>
    </template>
  </a-result>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  DownOutlined,
} from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { api, parseApiError } from '../api/client.js'
import PackageEditor from '../components/PackageEditor.vue'

const route = useRoute()
const router = useRouter()

// --- state ---
const app = ref(null)
const channels = ref([])
const timeline = ref([])
const loading = ref(true)
const channelsLoading = ref(false)
const timelineLoading = ref(false)
const error = ref('')
const channelError = ref('')
const timelineError = ref('')
const actionError = ref('')
const unavailable = ref(false)
const notFound = ref(false)
const actionLoading = ref('')

const submissionRef = ref(null)
const packageEditorRef = ref(null)

const submissionForm = ref({
  apply_url: '',
  submitted_at: null,
  note: '',
})
const submissionFormError = ref('')

// --- computed ---
const appId = computed(() => route.params.id)

const submissionChannel = computed(() => {
  const sel = app.value?.submission_channel
  if (sel === 'external_form' || sel === 'manual') return sel
  // Default to manual when nothing selected yet; external_form requires apply_url
  if (app.value?.apply_url) return 'external_form'
  return 'manual'
})

const showSubmissionPanel = computed(() => {
  if (!app.value) return false
  if (!isState(app.value.state, 'preparing')) return false
  return ['external_form', 'manual'].includes(submissionChannel.value)
})

const canSelectChannel = computed(() => {
  if (!app.value) return false
  // Allow channel selection once preparing; favorited must start preparation first
  return isState(app.value.state, 'preparing')
})

const primaryActionTitle = computed(() => {
  const s = app.value?.state
  if (isState(s, 'favorited')) return '开始准备申请材料'
  if (isState(s, 'preparing')) return '准备就绪？前往投递确认'
  if (isTerminal(s)) return '申请已完成'
  if (isState(s, 'on_hold')) return '申请已暂停'
  if (isState(s, 'withdrawn')) return '申请已撤回'
  return '当前状态无可执行操作'
})

const primaryActionHint = computed(() => {
  const s = app.value?.state
  if (isState(s, 'favorited')) return '进入准备阶段以选择渠道、绑定投递包并确认投递。'
  if (isState(s, 'preparing')) return '在官方渠道完成提交后，请明确确认投递。'
  if (isTerminal(s)) return '此申请已投递，可在时间线查看完整记录。'
  return '如需恢复，请在「更多操作」中调整状态。'
})

// --- fetch ---
async function loadAll() {
  loading.value = true
  error.value = ''
  unavailable.value = false
  notFound.value = false
  app.value = null
  channels.value = []
  timeline.value = []
  try {
    const data = await api.getApplication(appId.value)
    app.value = data
    // Prefill submission apply_url from the application's trusted link
    submissionForm.value.apply_url = data.apply_url || ''
    // Load channels + timeline in parallel (non-fatal if either fails)
    await Promise.all([loadChannels(), loadTimeline()])
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      unavailable.value = true
    } else if (parsed.status === 404) {
      notFound.value = true
    } else {
      error.value = parsed.message || '加载申请详情失败，请稍后重试。'
    }
  } finally {
    loading.value = false
  }
}

async function loadChannels() {
  channelsLoading.value = true
  channelError.value = ''
  try {
    const data = await api.getApplicationChannels(appId.value)
    channels.value = data.channels || []
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      channelError.value = '渠道服务暂不可用，请稍后重试。'
    } else {
      channelError.value = parsed.message || '加载渠道失败。'
    }
    channels.value = []
  } finally {
    channelsLoading.value = false
  }
}

async function loadTimeline() {
  timelineLoading.value = true
  timelineError.value = ''
  try {
    const data = await api.getApplicationTimeline(appId.value)
    // Sort ascending by occurred_at for a chronological read
    const items = data.items || []
    items.sort((a, b) => new Date(a.occurred_at) - new Date(b.occurred_at))
    timeline.value = items
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      timelineError.value = '时间线服务暂不可用，请稍后重试。'
    } else {
      timelineError.value = parsed.message || '加载时间线失败。'
    }
    timeline.value = []
  } finally {
    timelineLoading.value = false
  }
}

// --- actions ---
async function onPrepare() {
  actionLoading.value = 'prepare'
  actionError.value = ''
  try {
    const data = await api.prepareApplication(appId.value)
    app.value = data
    message.success('已进入准备阶段')
    await loadChannels()
    await loadTimeline()
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      actionError.value = '服务暂不可用，请稍后重试。'
    } else {
      actionError.value = parsed.message || '操作失败，请稍后重试。'
    }
  } finally {
    actionLoading.value = ''
  }
}

async function onSelectChannel(channel) {
  actionLoading.value = 'channel-' + channel
  channelError.value = ''
  try {
    const data = await api.selectApplicationChannel(appId.value, channel)
    app.value = data
    message.success(`已选择渠道：${channelLabel(channel)}`)
    await loadTimeline()
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      channelError.value = '服务暂不可用，请稍后重试。'
    } else {
      channelError.value = parsed.message || '选择渠道失败。'
    }
  } finally {
    actionLoading.value = ''
  }
}

async function onStateMenu({ key }) {
  const toState = key
  const note = toState === 'on_hold' ? '用户设置暂停' : '用户撤回申请'
  actionLoading.value = 'state-' + toState
  actionError.value = ''
  try {
    const data = await api.changeApplicationState(appId.value, { to_state: toState, note })
    app.value = data
    message.success('状态已更新')
    await loadTimeline()
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      actionError.value = '服务暂不可用，请稍后重试。'
    } else {
      actionError.value = parsed.message || '状态更新失败。'
    }
  } finally {
    actionLoading.value = ''
  }
}

async function onConfirmSubmission() {
  if (submissionChannel.value === 'external_form' && !submissionForm.value.apply_url) {
    submissionFormError.value = '外部表单渠道需要提供申请链接。'
    return
  }
  submissionFormError.value = ''
  actionLoading.value = 'confirm'
  actionError.value = ''
  try {
    const body = {
      apply_url: submissionForm.value.apply_url || app.value?.apply_url || '',
      channel: submissionChannel.value,
    }
    if (submissionForm.value.submitted_at) {
      body.submitted_at = toDateIso(submissionForm.value.submitted_at)
    }
    if (submissionForm.value.note) {
      body.note = submissionForm.value.note
    }
    const data = await api.confirmExternalSubmission(appId.value, body)
    app.value = data
    message.success('已确认投递')
    submissionForm.value.note = ''
    await loadTimeline()
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      actionError.value = '服务暂不可用，请稍后重试。'
    } else {
      actionError.value = parsed.message || '确认失败，请稍后重试。'
    }
  } finally {
    actionLoading.value = ''
  }
}

function scrollToSubmission() {
  nextTick(() => {
    submissionRef.value?.$el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  })
}

function scrollToPackageEditor() {
  nextTick(() => {
    packageEditorRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  })
}

// --- package editor callbacks ---
// When a package is approved, reload the application so the binding
// (package_version_id / payload_hash) reflects the approved version.
async function onPackageApproved() {
  try {
    const data = await api.getApplication(appId.value)
    app.value = data
  } catch {
    // Non-fatal: the editor already reflects the approved state inline.
  }
}

// When the first draft is created, no application-level binding changes
// (binding happens on approval). Kept as a hook for future telemetry.
function onPackageCreated() {
  // no-op for now
}

// --- helpers: state ---
function isState(value, expected) {
  return String(value || '').toLowerCase() === expected
}

function isTerminal(state) {
  return isState(state, 'submitted') ||
    isState(state, 'interviewing') ||
    isState(state, 'offer') ||
    isState(state, 'rejected')
}

function stateColor(state) {
  const s = String(state || '').toLowerCase()
  return {
    favorited: 'gold',
    preparing: 'blue',
    submitted: 'green',
    interviewing: 'cyan',
    offer: 'purple',
    rejected: 'red',
    withdrawn: 'default',
    on_hold: 'orange',
    ignored: 'default',
  }[s] || 'default'
}

function stateLabel(state) {
  if (!state) return '-'
  const s = String(state).toLowerCase()
  return {
    favorited: '已收藏',
    preparing: '准备中',
    submitted: '已投递',
    interviewing: '面试中',
    offer: 'Offer',
    rejected: '未通过',
    withdrawn: '已撤回',
    on_hold: '暂停',
    ignored: '已忽略',
    none: '未开始',
  }[s] || state
}

// --- helpers: channel ---
function channelLabel(channel) {
  return {
    email: '邮件 (Email)',
    external_form: '外部表单 (External form)',
    manual: '人工 (Manual)',
  }[channel] || channel || '-'
}

// --- helpers: timeline status ---
function timelineColor(status) {
  // a-timeline color: gray / blue / red / green
  const s = String(status || '').toLowerCase()
  if (s === 'confirmed') return 'green'
  if (s === 'proposed' || s === 'pending') return 'blue'
  if (s === 'rejected' || s === 'failed') return 'red'
  if (s === 'reconciliation_required') return 'orange'
  return 'gray'
}

function statusTagColor(status) {
  const s = String(status || '').toLowerCase()
  if (s === 'confirmed') return 'green'
  if (s === 'proposed') return 'gold'
  if (s === 'pending') return 'blue'
  if (s === 'rejected' || s === 'failed') return 'red'
  if (s === 'reconciliation_required') return 'orange'
  return 'default'
}

function statusLabel(status) {
  const s = String(status || '').toLowerCase()
  return {
    confirmed: '已确认',
    proposed: '建议',
    rejected: '已驳回',
    pending: '待处理',
    failed: '失败',
    reconciliation_required: '需人工对账',
  }[s] || status || '-'
}

// --- helpers: formatting ---
function shortId(id) {
  if (!id) return '-'
  const s = String(id)
  return s.length > 10 ? s.slice(0, 8) + '…' : s
}

function truncateHash(hash) {
  if (!hash) return '-'
  const s = String(hash)
  return s.length > 16 ? s.slice(0, 12) + '…' + s.slice(-4) : s
}

function truncateUrl(url) {
  if (!url) return '-'
  const s = String(url)
  if (s.length <= 48) return s
  try {
    const { hostname, pathname } = new URL(s)
    const path = pathname.length > 20 ? pathname.slice(0, 20) + '…' : pathname
    return hostname + path
  } catch {
    return s.slice(0, 48) + '…'
  }
}

function toDateIso(value) {
  if (!value) return null
  // antd dayjs or Date — accept both
  if (value instanceof Date) return value.toISOString()
  if (typeof value === 'object' && typeof value.toDate === 'function') {
    return value.toDate().toISOString()
  }
  const d = new Date(value)
  return isNaN(d.getTime()) ? null : d.toISOString()
}

function formatDateTime(dateStr) {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

// --- lifecycle ---
onMounted(loadAll)
</script>

<style scoped>
.detail-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 20px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}
.detail-title {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.detail-title h1 {
  margin: 0;
  font-size: 24px;
  color: #101828;
}
.detail-actions {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}

.detail-card {
  border: 1px solid #eaecf0;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #667085;
}
.muted {
  color: #98a2b3;
  font-size: 12px;
}
.table-link {
  color: #1677ff;
  font-weight: 500;
}

/* Action card */
.action-card {
  border-color: #b7deb8;
  background: #f6ffed;
}
.action-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

/* Channels */
.channel-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.channel-card {
  border: 1px solid #eaecf0;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fff;
  transition: border-color 0.2s;
}
.channel-card--active {
  border-color: #1677ff;
  background: #f0f7ff;
}
.channel-card--disabled {
  background: #fafafa;
  opacity: 0.85;
}
.channel-card__head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.channel-card__name {
  font-weight: 600;
  color: #101828;
}
.channel-card__reason {
  margin: 4px 0 6px;
}
.channel-card__url {
  margin-bottom: 6px;
}
.channel-card__action {
  display: flex;
  justify-content: flex-end;
}

/* Timeline */
.timeline-item__head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.timeline-item__title {
  font-weight: 600;
  color: #101828;
}
.timeline-item__desc {
  color: #344054;
  font-size: 13px;
  margin: 4px 0;
}
.timeline-item__meta {
  font-size: 12px;
  display: flex;
  gap: 6px;
}
.timeline-item__transition {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}
.timeline-item__evidence {
  margin-top: 4px;
}
</style>
