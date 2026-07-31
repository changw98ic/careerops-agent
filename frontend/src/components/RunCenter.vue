<template>
  <section class="run-center" :aria-busy="loading">
    <!-- Empty state -->
    <div v-if="!run && !loading" class="run-center__empty">
      <a-empty description="选择一条运行记录以查看详情。" />
    </div>

    <!-- Loading skeleton -->
    <div v-else-if="loading && !run" class="run-center__loading">
      <a-spin tip="加载运行详情..." />
    </div>

    <!-- Run detail -->
    <template v-else-if="run">
      <!-- Live region for state changes -->
      <div
        class="run-center__live"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {{ liveMessage }}
      </div>

      <!-- Header: state + actions -->
      <div class="run-center__header">
        <div class="run-center__state-group">
          <a-tag :color="stateColor(run.state)" class="run-center__state-tag">
            {{ stateLabel(run.state) }}
          </a-tag>
          <span v-if="run.capability" class="run-center__capability">
            {{ capabilityLabel(run.capability) }}
          </span>
        </div>
        <div class="run-center__actions">
          <a-button
            v-if="canRetry"
            size="small"
            @click="onRetry"
            :loading="actionLoading.retry"
            aria-label="重试此运行"
          >
            重试
          </a-button>
          <a-button
            v-if="canStop"
            size="small"
            danger
            @click="onStop"
            :loading="actionLoading.stop"
            aria-label="停止此运行"
          >
            停止
          </a-button>
          <a-button
            v-if="canReview"
            size="small"
            type="primary"
            @click="onReview('accepted')"
            aria-label="接受此结果"
          >
            接受
          </a-button>
          <a-button
            v-if="canReview"
            size="small"
            @click="onReview('edited')"
            aria-label="标记已编辑"
          >
            已编辑
          </a-button>
          <a-button
            v-if="canReview"
            size="small"
            danger
            ghost
            @click="onReview('rejected')"
            aria-label="拒绝此结果"
          >
            拒绝
          </a-button>
        </div>
      </div>

      <!-- Capability state banner -->
      <a-alert
        v-if="run.state === 'unavailable'"
        type="warning"
        show-icon
        message="当前未启用大模型"
        description="此运行因模型能力未启用而被安全降级，显示的结果为确定性降级提示，非模型判断。"
        class="run-center__alert"
      />

      <!-- Review decision banner -->
      <a-alert
        v-if="run.review_decision"
        :type="reviewAlertType"
        show-icon
        :message="`审核结果：${reviewDecisionLabel(run.review_decision)}`"
        :description="run.review_note || undefined"
        class="run-center__alert"
      />

      <!-- Trace ID (copyable) -->
      <div class="run-center__field" v-if="run.trace_id">
        <span class="run-center__label">跟踪标识</span>
        <button
          type="button"
          class="run-center__copy-btn"
          @click="copyTraceId"
          :aria-label="`复制跟踪标识 ${run.trace_id}`"
        >
          <code class="run-center__trace">{{ run.trace_id }}</code>
          <span class="run-center__copy-hint" aria-hidden="true">
            {{ copied ? '已复制' : '复制' }}
          </span>
        </button>
      </div>

      <!-- Input digests (redacted) -->
      <a-card title="输入摘要" size="small" class="run-center__card">
        <a-descriptions :column="{ xs: 1, sm: 2 }" size="small" bordered>
          <a-descriptions-item label="输入哈希">
            <code>{{ run.input_hash || '-' }}</code>
          </a-descriptions-item>
          <a-descriptions-item label="模式版本">
            {{ run.schema_version || '-' }}
          </a-descriptions-item>
          <a-descriptions-item label="提示版本">
            {{ run.prompt_version || '-' }}
          </a-descriptions-item>
          <a-descriptions-item label="证据数量">
            {{ evidenceCount }}
          </a-descriptions-item>
        </a-descriptions>
        <div v-if="identityKeys.length" class="run-center__identities">
          <span class="run-center__label">输入身份（已脱敏）</span>
          <div class="run-center__identity-list">
            <a-tag v-for="key in identityKeys" :key="key" size="small">
              {{ key }}
            </a-tag>
          </div>
        </div>
      </a-card>

      <!-- Model / provider state -->
      <a-card title="模型与提供方" size="small" class="run-center__card">
        <a-descriptions :column="{ xs: 1, sm: 2 }" size="small" bordered>
          <a-descriptions-item label="模型标识">
            {{ run.model_id || '未指定' }}
          </a-descriptions-item>
          <a-descriptions-item label="输入 Token">
            {{ run.input_tokens || 0 }}
          </a-descriptions-item>
          <a-descriptions-item label="输出 Token">
            {{ run.output_tokens || 0 }}
          </a-descriptions-item>
          <a-descriptions-item label="错误类别">
            {{ run.error_category || '-' }}
          </a-descriptions-item>
        </a-descriptions>
      </a-card>

      <!-- Stage timeline -->
      <a-card title="阶段时间线" size="small" class="run-center__card">
        <StageTimeline v-if="stages.length" :stages="stages" />
        <div v-else class="run-center__stages-empty">
          <span class="muted">
            同步执行：该运行由后端进程直接完成，不产生分阶段事件。
          </span>
        </div>
      </a-card>

      <!-- Timing -->
      <a-card title="时间信息" size="small" class="run-center__card">
        <a-descriptions :column="{ xs: 1, sm: 2 }" size="small" bordered>
          <a-descriptions-item label="创建时间">
            {{ formatDateTime(run.created_at) }}
          </a-descriptions-item>
          <a-descriptions-item label="开始时间">
            {{ formatDateTime(run.started_at) }}
          </a-descriptions-item>
          <a-descriptions-item label="完成时间">
            {{ formatDateTime(run.finished_at) }}
          </a-descriptions-item>
          <a-descriptions-item label="审核时间">
            {{ formatDateTime(run.reviewed_at) }}
          </a-descriptions-item>
        </a-descriptions>
      </a-card>

      <!-- Review metadata -->
      <a-card v-if="run.review_decision" title="审核详情" size="small" class="run-center__card">
        <a-descriptions :column="{ xs: 1, sm: 2 }" size="small" bordered>
          <a-descriptions-item label="审核决定">
            {{ reviewDecisionLabel(run.review_decision) }}
          </a-descriptions-item>
          <a-descriptions-item label="审核人">
            {{ run.reviewed_by || '-' }}
          </a-descriptions-item>
          <a-descriptions-item label="审核备注" :span="2">
            {{ run.review_note || '-' }}
          </a-descriptions-item>
        </a-descriptions>
      </a-card>

      <!-- Result (redacted summary) -->
      <a-card title="结果摘要" size="small" class="run-center__card">
        <pre class="run-center__result-json">{{ prettyResult }}</pre>
      </a-card>
    </template>
  </section>
</template>

<script setup>
/**
 * RunCenter -- Agent run lifecycle display.
 *
 * Shows run state, stage timeline, copyable trace ID, redacted input
 * digests, model/provider state, review decision metadata, and
 * state-dependent retry/stop/review controls.
 *
 * All content is redacted: no raw prompts, outputs, credentials, or
 * browser session material are ever rendered.
 */

import { computed, ref, watch } from 'vue'
import StageTimeline from './StageTimeline.vue'

const props = defineProps({
  run: {
    type: Object,
    default: null,
  },
  stages: {
    type: Array,
    default: () => [],
  },
  loading: {
    type: Boolean,
    default: false,
  },
  actionLoading: {
    type: Object,
    default: () => ({ retry: false, stop: false, review: false }),
  },
})

const emit = defineEmits(['retry', 'stop', 'review'])

// --- Copyable trace ID ---
const copied = ref(false)
let copyTimer = null

function copyTraceId() {
  if (!props.run?.trace_id) return
  navigator.clipboard.writeText(props.run.trace_id).then(() => {
    copied.value = true
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => {
      copied.value = false
    }, 2000)
  })
}

// --- Live region message ---
const liveMessage = computed(() => {
  if (!props.run) return ''
  const label = stateLabel(props.run.state)
  const cap = capabilityLabel(props.run.capability)
  return `${cap} 运行状态：${label}`
})

// --- State-dependent visibility ---
// Retry is offered only for runs that finished WITHOUT a reviewable outcome:
// the backend replays their stored input into a new run. SUCCEEDED/REVIEWED
// runs are never retried (a silent duplicate).
const RETRYABLE_STATES = new Set(['failed', 'abstained', 'unavailable', 'cancelled'])
const REVIEWABLE_STATES = new Set(['succeeded', 'unavailable', 'abstained'])

const canRetry = computed(() => {
  if (!props.run) return false
  return RETRYABLE_STATES.has(props.run.state)
})

// Synchronous execution runs inline in the HTTP call, so PENDING is only a
// transient state; stop is offered whenever one is visible anyway.
const canStop = computed(() => {
  if (!props.run) return false
  return props.run.state === 'pending'
})

const canReview = computed(() => {
  if (!props.run) return false
  return REVIEWABLE_STATES.has(props.run.state) && !props.run.review_decision
})

// --- Computed fields ---
const identityKeys = computed(() => {
  if (!props.run?.input_identities) return []
  return Object.keys(props.run.input_identities)
})

const evidenceCount = computed(() => {
  if (!props.run?.evidence_ids) return 0
  return Array.isArray(props.run.evidence_ids) ? props.run.evidence_ids.length : 0
})

const prettyResult = computed(() => {
  if (!props.run?.result) return '{}'
  return JSON.stringify(props.run.result, null, 2)
})

const reviewAlertType = computed(() => {
  const d = props.run?.review_decision
  if (d === 'accepted') return 'success'
  if (d === 'rejected') return 'error'
  return 'info'
})

// --- Labels ---
const STATE_LABELS = {
  pending: '排队中',
  running: '运行中',
  succeeded: '已完成',
  failed: '失败',
  unavailable: '模型关闭',
  abstained: '已弃答',
  stale: '已过期',
  cancelled: '已取消',
  reviewed: '已审核',
}

const STATE_COLORS = {
  pending: 'default',
  running: 'processing',
  succeeded: 'success',
  failed: 'error',
  unavailable: 'orange',
  abstained: 'red',
  stale: 'default',
  cancelled: 'warning',
  reviewed: 'green',
}

const CAPABILITY_LABELS = {
  resume_review: '简历审查',
  interview_preparation: '面试准备',
  job_matching: '职位匹配',
}

const REVIEW_DECISION_LABELS = {
  accepted: '已接受',
  rejected: '已拒绝',
  edited: '已编辑',
}

function stateLabel(state) {
  return STATE_LABELS[state] || state || '未知'
}

function stateColor(state) {
  return STATE_COLORS[state] || 'default'
}

function capabilityLabel(cap) {
  return CAPABILITY_LABELS[cap] || cap || '未知能力'
}

function reviewDecisionLabel(decision) {
  return REVIEW_DECISION_LABELS[decision] || decision || '未知'
}

function formatDateTime(value) {
  if (!value) return '-'
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

// --- Actions ---
function onRetry() {
  emit('retry', props.run)
}

function onStop() {
  emit('stop', props.run)
}

function onReview(decision) {
  emit('review', props.run, decision)
}
</script>

<style scoped>
.run-center {
  display: grid;
  gap: 12px;
}

.run-center__empty,
.run-center__loading {
  display: grid;
  place-items: center;
  min-height: 120px;
}

.run-center__live {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}

.run-center__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.run-center__state-group {
  display: flex;
  align-items: center;
  gap: 10px;
}

.run-center__state-tag {
  font-size: 14px;
  padding: 2px 10px;
}

.run-center__capability {
  color: var(--color-tertiary, #8c8c8c);
  font-size: 13px;
}

.run-center__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.run-center__alert {
  margin-top: 4px;
}

.run-center__field {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.run-center__label {
  font-size: 12px;
  color: var(--color-tertiary, #8c8c8c);
  font-weight: 500;
}

.run-center__copy-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid #d9d9d9;
  border-radius: 6px;
  padding: 4px 10px;
  background: #fafafa;
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color 0.2s;
}

.run-center__copy-btn:hover {
  border-color: #1677ff;
}

.run-center__copy-btn:focus-visible {
  outline: 2px solid rgba(22, 119, 255, 0.3);
  outline-offset: 2px;
}

.run-center__trace {
  font-size: 12px;
  font-family: monospace;
  word-break: break-all;
}

.run-center__copy-hint {
  font-size: 11px;
  color: #1677ff;
  white-space: nowrap;
}

.run-center__card {
  margin-top: 4px;
}

.run-center__identities {
  margin-top: 12px;
}

.run-center__identity-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.run-center__stages-empty {
  padding: 14px 0;
  color: var(--color-tertiary, #8c8c8c);
  font-size: 13px;
}

.run-center__result-json {
  max-height: 300px;
  overflow: auto;
  padding: 12px;
  border-radius: 6px;
  background: #1f1f1f;
  color: #f5f5f5;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
</style>
