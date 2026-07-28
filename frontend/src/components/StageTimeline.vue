<template>
  <div class="stage-timeline" role="list" aria-label="运行阶段时间线">
    <div
      v-if="!stages.length"
      class="stage-timeline__empty"
    >
      <span class="muted">暂无阶段记录。</span>
    </div>
    <div
      v-for="(stage, index) in stages"
      :key="stage.stage_event_id || index"
      class="stage-timeline__item"
      :class="stageItemClass(stage)"
      role="listitem"
      :aria-label="`${stageLabel(stage.stage)} - ${statusLabel(stage.status)}`"
    >
      <div class="stage-timeline__connector">
        <span class="stage-timeline__dot" :class="dotClass(stage)" />
        <span v-if="index < stages.length - 1" class="stage-timeline__line" />
      </div>
      <div class="stage-timeline__body">
        <div class="stage-timeline__header">
          <span class="stage-timeline__name">{{ stageLabel(stage.stage) }}</span>
          <a-tag :color="statusColor(stage.status)" size="small">
            {{ statusLabel(stage.status) }}
          </a-tag>
        </div>
        <div class="stage-timeline__meta">
          <span v-if="stage.attempt > 1" class="stage-timeline__badge">
            第 {{ stage.attempt }} 次尝试
          </span>
          <span v-if="stage.duration_ms != null" class="stage-timeline__duration">
            {{ formatDuration(stage.duration_ms) }}
          </span>
          <span v-if="stage.cause_code" class="stage-timeline__cause">
            原因：{{ causeLabel(stage.cause_code) }}
          </span>
          <span v-if="stage.retryable" class="stage-timeline__retryable">
            可重试
          </span>
        </div>
        <div v-if="stage.provider_state" class="stage-timeline__provider">
          提供方状态：{{ stage.provider_state }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * StageTimeline -- append-only stage timeline for agent runs.
 *
 * Displays stage name, status, attempt, duration, cause code, and
 * retryability. All content is redacted: no raw prompts, outputs, or
 * provider payloads are rendered.
 */

defineProps({
  stages: {
    type: Array,
    default: () => [],
  },
})

const STAGE_LABELS = {
  resolve_agent_context: '解析上下文',
  run_deterministic_stage: '确定性处理',
  invoke_model_stage: '模型调用',
  persist_agent_stage: '结果持久化',
  reconcile_agent_run: '运行协调',
}

const STATUS_LABELS = {
  pending: '排队中',
  running: '运行中',
  succeeded: '已完成',
  failed: '失败',
  skipped: '已跳过',
  cancelled: '已取消',
}

const STATUS_COLORS = {
  pending: 'default',
  running: 'processing',
  succeeded: 'success',
  failed: 'error',
  skipped: 'warning',
  cancelled: 'warning',
}

const CAUSE_LABELS = {
  timeout: '超时',
  provider_error: '提供方错误',
  rate_limited: '频率限制',
  policy_denied: '策略拒绝',
  invalid_input: '输入无效',
  internal_error: '内部错误',
  cancelled: '已取消',
}

function stageLabel(stage) {
  return STAGE_LABELS[stage] || stage || '未知阶段'
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status || '未知'
}

function statusColor(status) {
  return STATUS_COLORS[status] || 'default'
}

function causeLabel(code) {
  return CAUSE_LABELS[code] || code
}

function formatDuration(ms) {
  if (ms == null || ms < 0) return ''
  if (ms < 1000) return `${ms}ms`
  const seconds = (ms / 1000).toFixed(1)
  return `${seconds}s`
}

function stageItemClass(stage) {
  return {
    'stage-timeline__item--terminal': stage.terminal,
    'stage-timeline__item--failed': stage.status === 'failed',
    'stage-timeline__item--running': stage.status === 'running',
  }
}

function dotClass(stage) {
  return {
    'stage-timeline__dot--succeeded': stage.status === 'succeeded',
    'stage-timeline__dot--failed': stage.status === 'failed',
    'stage-timeline__dot--running': stage.status === 'running',
    'stage-timeline__dot--pending': stage.status === 'pending',
    'stage-timeline__dot--skipped': stage.status === 'skipped' || stage.status === 'cancelled',
  }
}
</script>

<style scoped>
.stage-timeline {
  display: grid;
  gap: 0;
}

.stage-timeline__empty {
  padding: 16px 0;
}

.stage-timeline__item {
  display: flex;
  gap: 12px;
  padding: 8px 0;
}

.stage-timeline__connector {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 16px;
  flex-shrink: 0;
}

.stage-timeline__dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2px solid #d9d9d9;
  background: white;
  flex-shrink: 0;
  margin-top: 4px;
}

.stage-timeline__dot--succeeded {
  border-color: #52c41a;
  background: #52c41a;
}

.stage-timeline__dot--failed {
  border-color: #ff4d4f;
  background: #ff4d4f;
}

.stage-timeline__dot--running {
  border-color: #1677ff;
  background: #1677ff;
  animation: pulse-dot 1.5s ease-in-out infinite;
}

.stage-timeline__dot--pending {
  border-color: #d9d9d9;
  background: white;
}

.stage-timeline__dot--skipped {
  border-color: #faad14;
  background: #faad14;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.stage-timeline__line {
  width: 2px;
  flex: 1;
  min-height: 16px;
  background: #e8e8e8;
  margin-top: 4px;
}

.stage-timeline__item--failed .stage-timeline__line {
  background: #ffccc7;
}

.stage-timeline__body {
  flex: 1;
  min-width: 0;
}

.stage-timeline__header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.stage-timeline__name {
  font-weight: 600;
  font-size: 13px;
  color: var(--color-primary, #1f1f1f);
}

.stage-timeline__meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 4px;
  font-size: 12px;
  color: var(--color-tertiary, #8c8c8c);
}

.stage-timeline__badge {
  background: #f0f0f0;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 11px;
}

.stage-timeline__cause {
  color: #d4380d;
}

.stage-timeline__retryable {
  color: #faad14;
  font-weight: 500;
}

.stage-timeline__provider {
  margin-top: 4px;
  font-size: 12px;
  color: var(--color-tertiary, #8c8c8c);
}
</style>
