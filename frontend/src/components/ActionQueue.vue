<template>
  <section
    class="action-queue"
    lang="zh-CN"
    aria-label="行动队列"
    role="region"
  >
    <!-- Queue version live region for screen readers -->
    <div
      class="sr-only"
      aria-live="polite"
      aria-atomic="true"
      :aria-label="`队列版本 ${queueVersion}`"
    >
      <span v-if="queueVersion">队列已更新，版本 {{ queueVersion }}</span>
    </div>

    <!-- Section header -->
    <div class="queue-header">
      <div class="queue-header-left">
        <div class="queue-eyebrow">待办事项</div>
        <h3 class="queue-title">行动队列</h3>
      </div>
      <div v-if="queueVersion" class="queue-version-tag" aria-label="队列版本">
        v{{ queueVersion }}
      </div>
    </div>

    <!-- Error state -->
    <div
      v-if="error"
      class="queue-error"
      role="alert"
      aria-describedby="queue-error-desc"
    >
      <div class="queue-error-content">
        <ExclamationCircleOutlined class="queue-error-icon" />
        <span id="queue-error-desc" class="queue-error-text">{{ error }}</span>
        <button
          class="queue-error-retry"
          type="button"
          aria-label="重试加载行动队列"
          @click="$emit('retry')"
        >
          重试
        </button>
      </div>
    </div>

    <!-- Loading skeleton -->
    <div v-else-if="loading" class="queue-loading" aria-label="正在加载行动队列">
      <div v-for="i in 2" :key="i" class="action-card-skeleton">
        <div class="skeleton-line skeleton-line--wide"></div>
        <div class="skeleton-line skeleton-line--medium"></div>
        <div class="skeleton-line skeleton-line--narrow"></div>
      </div>
    </div>

    <!-- Empty state -->
    <div v-else-if="!actions.length" class="queue-empty">
      <div class="empty-state">
        <CheckCircleOutlined class="empty-state-icon" />
        <p class="empty-state-text">{{ emptyMessage }}</p>
        <p class="empty-state-hint">{{ emptyHint }}</p>
      </div>
    </div>

    <!-- Action cards -->
    <div v-else class="queue-list" role="list" aria-label="行动列表">
      <div
        v-for="(action, index) in actions"
        :key="action.action_key"
        class="action-card"
        role="listitem"
        :aria-label="`行动 ${index + 1}/${actions.length}：${action.title}`"
        :data-priority="action.priority"
        tabindex="0"
        @keydown.enter.prevent="handleAccept(action)"
        @keydown.space.prevent="handleAccept(action)"
      >
        <div class="action-card-inner">
          <!-- Priority indicator -->
          <div class="action-priority" :aria-label="`优先级：${priorityLabel(action.priority)}`">
            <span class="priority-badge" :class="`priority-${action.priority}`">
              <component :is="priorityIcon(action.priority)" class="priority-icon" />
              <span class="priority-text">{{ priorityLabel(action.priority) }}</span>
            </span>
          </div>

          <!-- Action content -->
          <div class="action-content">
            <h4 class="action-title">{{ action.title }}</h4>
            <p v-if="action.reason" class="action-reason">
              <InfoCircleOutlined class="reason-icon" />
              <span>{{ reasonLabel(action.reason) }}</span>
            </p>

            <!-- Metadata row -->
            <div class="action-meta">
              <span v-if="action.freshness" class="meta-item" :title="`数据更新时间：${action.freshness}`">
                <ClockCircleOutlined class="meta-icon" />
                <span>{{ formatFreshness(action.freshness) }}</span>
              </span>
              <span
                v-if="action.prerequisite_met === false"
                class="meta-item meta-item--blocked"
                aria-label="前置条件未满足"
              >
                <LockOutlined class="meta-icon" />
                <span>需先完成前置条件</span>
              </span>
              <span
                v-if="action.prerequisite_met === true"
                class="meta-item meta-item--ready"
                aria-label="前置条件已满足"
              >
                <CheckCircleOutlined class="meta-icon" />
                <span>条件已就绪</span>
              </span>
            </div>
          </div>

          <!-- Action controls -->
          <div class="action-controls" role="group" aria-label="行动操作">
            <button
              type="button"
              class="action-btn action-btn--accept"
              :disabled="action.prerequisite_met === false || busyKey === action.action_key"
              :aria-label="`接受：${action.title}`"
              :aria-describedby="action.prerequisite_met === false ? `blocked-${action.action_key}` : undefined"
              @click.stop="handleAccept(action)"
            >
              <CheckOutlined class="btn-icon" />
              <span>接受</span>
            </button>
            <button
              type="button"
              class="action-btn action-btn--snooze"
              :disabled="busyKey === action.action_key"
              :aria-label="`延迟：${action.title}`"
              @click.stop="handleSnooze(action)"
            >
              <ClockCircleOutlined class="btn-icon" />
              <span>延迟</span>
            </button>
            <button
              type="button"
              class="action-btn action-btn--dismiss"
              :disabled="busyKey === action.action_key"
              :aria-label="`忽略：${action.title}`"
              @click.stop="handleDismiss(action)"
            >
              <CloseOutlined class="btn-icon" />
              <span>忽略</span>
            </button>
          </div>
        </div>

        <!-- Hidden blocked description for screen readers -->
        <span
          v-if="action.prerequisite_met === false"
          :id="`blocked-${action.action_key}`"
          class="sr-only"
        >
          此行动的前置条件尚未满足，请先完成前置任务
        </span>
      </div>
    </div>

    <!-- Snooze dialog -->
    <div
      v-if="snoozeTarget"
      class="snooze-overlay"
      role="dialog"
      aria-label="选择延迟时长"
      aria-modal="true"
      @keydown.escape="cancelSnooze"
    >
      <div class="snooze-dialog" ref="snoozeDialogRef">
        <h4 class="snooze-title">延迟多长时间？</h4>
        <div class="snooze-options" role="group" aria-label="延迟时长选项">
          <button
            v-for="opt in snoozeOptions"
            :key="opt.value"
            type="button"
            class="snooze-option"
            :aria-label="opt.label"
            @click="confirmSnooze(opt.value)"
          >
            {{ opt.label }}
          </button>
        </div>
        <button
          type="button"
          class="snooze-cancel"
          aria-label="取消延迟"
          @click="cancelSnooze"
        >
          取消
        </button>
      </div>
    </div>

    <!-- Mutation error toast -->
    <div
      v-if="mutationError"
      class="mutation-error"
      role="alert"
      aria-live="assertive"
      aria-describedby="mutation-error-desc"
    >
      <span id="mutation-error-desc">{{ mutationError }}</span>
      <button
        type="button"
        class="mutation-error-dismiss"
        aria-label="关闭错误提示"
        @click="mutationError = ''"
      >
        <CloseOutlined />
      </button>
    </div>
  </section>
</template>

<script setup>
import { ref, nextTick, watch } from 'vue'
import {
  ArrowUpOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseOutlined,
  ExclamationCircleOutlined,
  InfoCircleOutlined,
  LockOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import { api, formatApiError } from '../api/client.js'

const props = defineProps({
  actions: { type: Array, default: () => [] },
  queueVersion: { type: [String, Number], default: '' },
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
  emptyMessage: { type: String, default: '暂无待办事项' },
  emptyHint: { type: String, default: '所有行动已完成或无需处理' },
})

const emit = defineEmits(['retry', 'refresh', 'navigate'])

const busyKey = ref('')
const mutationError = ref('')
const snoozeTarget = ref(null)
const snoozeDialogRef = ref(null)

const snoozeOptions = [
  { label: '1 小时', value: '1h' },
  { label: '4 小时', value: '4h' },
  { label: '明天', value: '1d' },
  { label: '本周', value: '1w' },
]

// ---------- Priority helpers ----------

const PRIORITY_LABELS = {
  critical: '紧急',
  high: '重要',
  medium: '一般',
  low: '低',
}

function priorityLabel(priority) {
  return PRIORITY_LABELS[priority] || '一般'
}

function priorityIcon(priority) {
  if (priority === 'critical') return ExclamationCircleOutlined
  if (priority === 'high') return ArrowUpOutlined
  if (priority === 'medium') return ThunderboltOutlined
  return InfoCircleOutlined
}

// ---------- Reason labels ----------

const REASON_LABELS = {
  stale_crawl: '爬取数据已过期',
  pending_review: '有待审查的内容',
  ready_draft: '草稿已就绪',
  setup_required: '需要完成初始设置',
  profile_missing: '缺少职业画像',
  resume_missing: '缺少确认简历',
  evidence_pending: '有未审查的证据',
  follow_up_due: '跟进时间已到',
  interview_prep: '面试准备提醒',
  match_found: '发现新的匹配职位',
}

function reasonLabel(reason) {
  return REASON_LABELS[reason] || reason || ''
}

// ---------- Freshness formatting ----------

function formatFreshness(isoString) {
  if (!isoString) return ''
  const now = Date.now()
  const then = new Date(isoString).getTime()
  if (Number.isNaN(then)) return ''
  const diffMs = now - then
  const diffMin = Math.floor(diffMs / 60000)
  if (diffMin < 1) return '刚刚更新'
  if (diffMin < 60) return `${diffMin} 分钟前`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24) return `${diffHr} 小时前`
  const diffDay = Math.floor(diffHr / 24)
  if (diffDay < 30) return `${diffDay} 天前`
  return '数据较旧'
}

// ---------- Action handlers ----------

async function handleAccept(action) {
  if (action.prerequisite_met === false || busyKey.value) return
  busyKey.value = action.action_key
  mutationError.value = ''
  try {
    const result = await api.acceptAction(action.action_key, {
      queue_version: props.queueVersion,
    })
    if (result?.navigate_to) {
      emit('navigate', result.navigate_to)
    }
    emit('refresh')
  } catch (err) {
    mutationError.value = formatApiError(err, '接受行动失败，请稍后重试。')
  } finally {
    busyKey.value = ''
  }
}

function handleSnooze(action) {
  if (busyKey.value) return
  snoozeTarget.value = action
  nextTick(() => {
    snoozeDialogRef.value?.focus()
  })
}

async function confirmSnooze(duration) {
  if (!snoozeTarget.value) return
  const actionKey = snoozeTarget.value.action_key
  snoozeTarget.value = null
  busyKey.value = actionKey
  mutationError.value = ''
  try {
    await api.snoozeAction(actionKey, {
      duration,
      queue_version: props.queueVersion,
    })
    emit('refresh')
  } catch (err) {
    mutationError.value = formatApiError(err, '延迟行动失败，请稍后重试。')
  } finally {
    busyKey.value = ''
  }
}

function cancelSnooze() {
  snoozeTarget.value = null
}

async function handleDismiss(action) {
  if (busyKey.value) return
  busyKey.value = action.action_key
  mutationError.value = ''
  try {
    await api.dismissAction(action.action_key, {
      queue_version: props.queueVersion,
    })
    emit('refresh')
  } catch (err) {
    mutationError.value = formatApiError(err, '忽略行动失败，请稍后重试。')
  } finally {
    busyKey.value = ''
  }
}
</script>

<style scoped>
/* ============================================
   ActionQueue component styles
   ============================================ */

.action-queue {
  max-width: 100%;
}

/* Screen reader only */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

/* Header */
.queue-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 20px;
}

.queue-eyebrow {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--text-tertiary);
  margin-bottom: 6px;
}

.queue-title {
  font-size: 20px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.02em;
  margin: 0;
}

.queue-version-tag {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-tertiary);
  padding: 4px 10px;
  border-radius: var(--radius-xs);
  background: var(--surface-secondary);
  border: 1px solid var(--border-light);
}

/* Error state */
.queue-error {
  margin-bottom: 16px;
}

.queue-error-content {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 18px;
  border-radius: var(--radius-md);
  background: rgba(255, 59, 48, 0.06);
  border: 1px solid rgba(255, 59, 48, 0.12);
}

.queue-error-icon {
  font-size: 16px;
  color: var(--accent-red);
  flex-shrink: 0;
}

.queue-error-text {
  flex: 1;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}

.queue-error-retry {
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid rgba(255, 59, 48, 0.2);
  background: rgba(255, 59, 48, 0.08);
  color: var(--accent-red);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.3s ease;
}

.queue-error-retry:hover {
  background: rgba(255, 59, 48, 0.14);
}

.queue-error-retry:focus-visible {
  outline: 2px solid var(--accent-blue);
  outline-offset: 2px;
}

/* Loading skeleton */
.queue-loading {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.action-card-skeleton {
  padding: 20px;
  border-radius: var(--radius-md);
  background: var(--surface-secondary);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.skeleton-line {
  height: 12px;
  border-radius: var(--radius-xs);
  background: linear-gradient(
    90deg,
    var(--surface-tertiary) 25%,
    var(--surface-secondary) 50%,
    var(--surface-tertiary) 75%
  );
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.5s infinite;
}

.skeleton-line--wide { width: 80%; }
.skeleton-line--medium { width: 60%; }
.skeleton-line--narrow { width: 35%; }

@keyframes skeleton-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* Empty state */
.queue-empty {
  padding: 40px 0;
}

.empty-state {
  text-align: center;
  padding: 24px;
}

.empty-state-icon {
  font-size: 40px;
  color: var(--accent-green);
  margin-bottom: 12px;
  opacity: 0.6;
}

.empty-state-text {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-secondary);
  margin: 0 0 6px 0;
}

.empty-state-hint {
  font-size: 13px;
  color: var(--text-tertiary);
  margin: 0;
}

/* Queue list */
.queue-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* Action card */
.action-card {
  border-radius: var(--radius-lg);
  background: var(--surface-secondary);
  border: 1px solid var(--border-light);
  padding: 6px;
  transition: transform 0.3s cubic-bezier(0.32, 0.72, 0, 1), box-shadow 0.3s ease;
}

.action-card:focus-visible {
  outline: 2px solid var(--accent-blue);
  outline-offset: 2px;
}

.action-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.06);
}

.action-card-inner {
  background: var(--surface-primary);
  border-radius: calc(var(--radius-lg) - 6px);
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* Priority indicator */
.action-priority {
  display: flex;
}

.priority-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border-radius: var(--radius-xs);
  font-size: 12px;
  font-weight: 600;
}

.priority-badge.priority-critical {
  background: rgba(255, 59, 48, 0.1);
  color: var(--accent-red);
}

.priority-badge.priority-high {
  background: rgba(255, 149, 0, 0.1);
  color: #cc7700;
}

.priority-badge.priority-medium {
  background: rgba(0, 113, 227, 0.08);
  color: var(--accent-blue);
}

.priority-badge.priority-low {
  background: rgba(0, 0, 0, 0.04);
  color: var(--text-tertiary);
}

.priority-icon {
  font-size: 12px;
}

/* Action content */
.action-content {
  flex: 1;
  min-width: 0;
}

.action-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0 0 6px 0;
  letter-spacing: -0.01em;
  line-height: 1.3;
}

.action-reason {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-secondary);
  margin: 0 0 10px 0;
}

.reason-icon {
  font-size: 13px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}

/* Metadata row */
.action-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-tertiary);
}

.meta-icon {
  font-size: 12px;
}

.meta-item--blocked {
  color: var(--accent-red);
}

.meta-item--blocked .meta-icon {
  color: var(--accent-red);
}

.meta-item--ready {
  color: var(--accent-green);
}

.meta-item--ready .meta-icon {
  color: var(--accent-green);
}

/* Action controls */
.action-controls {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.action-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  border: none;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.3s ease;
}

.action-btn:focus-visible {
  outline: 2px solid var(--accent-blue);
  outline-offset: 2px;
}

.action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.action-btn .btn-icon {
  font-size: 13px;
}

.action-btn--accept {
  background: var(--accent-blue);
  color: white;
}

.action-btn--accept:hover:not(:disabled) {
  background: #0066d6;
  transform: translateY(-1px);
}

.action-btn--snooze {
  background: rgba(0, 0, 0, 0.04);
  color: var(--text-secondary);
  border: 1px solid var(--border-light);
}

.action-btn--snooze:hover:not(:disabled) {
  background: rgba(0, 0, 0, 0.08);
  color: var(--text-primary);
}

.action-btn--dismiss {
  background: transparent;
  color: var(--text-tertiary);
}

.action-btn--dismiss:hover:not(:disabled) {
  background: rgba(255, 59, 48, 0.06);
  color: var(--accent-red);
}

/* Snooze dialog overlay */
.snooze-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.snooze-dialog {
  background: var(--surface-primary);
  border-radius: var(--radius-xl);
  padding: 28px;
  max-width: 320px;
  width: 90%;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.15);
}

.snooze-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0 0 20px 0;
  text-align: center;
}

.snooze-options {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-bottom: 16px;
}

.snooze-option {
  padding: 12px 16px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-light);
  background: var(--surface-secondary);
  color: var(--text-primary);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.3s ease;
  text-align: center;
}

.snooze-option:hover {
  background: var(--surface-tertiary);
  border-color: var(--accent-blue);
}

.snooze-option:focus-visible {
  outline: 2px solid var(--accent-blue);
  outline-offset: 2px;
}

.snooze-cancel {
  width: 100%;
  padding: 10px;
  border-radius: var(--radius-sm);
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: color 0.3s ease;
}

.snooze-cancel:hover {
  color: var(--text-primary);
}

.snooze-cancel:focus-visible {
  outline: 2px solid var(--accent-blue);
  outline-offset: 2px;
}

/* Mutation error toast */
.mutation-error {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 20px;
  border-radius: var(--radius-md);
  background: var(--surface-primary);
  border: 1px solid rgba(255, 59, 48, 0.2);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12);
  z-index: 1001;
  max-width: 90vw;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
}

.mutation-error-dismiss {
  width: 24px;
  height: 24px;
  border-radius: var(--radius-xs);
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: color 0.3s ease;
}

.mutation-error-dismiss:hover {
  color: var(--text-primary);
}

.mutation-error-dismiss:focus-visible {
  outline: 2px solid var(--accent-blue);
  outline-offset: 2px;
}

/* ============================================
   Responsive
   ============================================ */

@media (max-width: 768px) {
  .action-card-inner {
    padding: 16px;
  }

  .action-controls {
    flex-direction: column;
  }

  .action-btn {
    width: 100%;
    justify-content: center;
  }
}

/* ============================================
   Reduced motion
   ============================================ */

@media (prefers-reduced-motion: reduce) {
  .action-card {
    transition: none;
  }

  .action-card:hover {
    transform: none;
  }

  .action-btn:hover:not(:disabled) {
    transform: none;
  }

  .skeleton-line {
    animation: none;
  }
}
</style>
