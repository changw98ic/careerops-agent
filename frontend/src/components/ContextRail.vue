<template>
  <aside
    class="context-rail"
    lang="zh-CN"
    role="complementary"
    aria-label="上下文信息"
  >
    <!-- Header -->
    <div class="rail-header">
      <div class="rail-eyebrow">工作上下文</div>
      <h3 class="rail-title">当前选择</h3>
    </div>

    <!-- Stale warning -->
    <div
      v-if="stale"
      class="rail-stale-warning"
      role="alert"
      aria-describedby="stale-desc"
    >
      <ExclamationCircleOutlined class="stale-icon" />
      <span id="stale-desc" class="stale-text">{{ staleReason || '上下文数据已过期，请重新选择' }}</span>
    </div>

    <!-- Context items -->
    <div class="rail-items">
      <!-- Job -->
      <div class="rail-item" :class="{ 'rail-item--missing': !context.job_id }">
        <div class="rail-item-header">
          <BankOutlined class="rail-item-icon" />
          <span class="rail-item-label">目标职位</span>
          <span v-if="context.job_id" class="rail-item-status rail-item-status--active" aria-label="已选择">
            <CheckCircleFilled />
          </span>
          <span v-else class="rail-item-status rail-item-status--empty" aria-label="未选择">
            <MinusCircleOutlined />
          </span>
        </div>
        <div class="rail-item-body">
          <span v-if="context.job_id" class="rail-item-id" :title="context.job_id">
            {{ truncateId(context.job_id) }}
          </span>
          <span v-else class="rail-item-placeholder">未选择职位</span>
        </div>
        <div v-if="context.job_freshness" class="rail-item-freshness">
          <ClockCircleOutlined class="freshness-icon" />
          <span>{{ formatFreshness(context.job_freshness) }}</span>
        </div>
      </div>

      <!-- Profile -->
      <div class="rail-item" :class="{ 'rail-item--missing': !context.profile_version_id }">
        <div class="rail-item-header">
          <UserOutlined class="rail-item-icon" />
          <span class="rail-item-label">职业画像</span>
          <span v-if="context.profile_version_id" class="rail-item-status rail-item-status--active" aria-label="已选择">
            <CheckCircleFilled />
          </span>
          <span v-else class="rail-item-status rail-item-status--empty" aria-label="未选择">
            <MinusCircleOutlined />
          </span>
        </div>
        <div class="rail-item-body">
          <span v-if="context.profile_version_id" class="rail-item-id" :title="context.profile_version_id">
            {{ truncateId(context.profile_version_id) }}
          </span>
          <span v-else class="rail-item-placeholder">未选择画像</span>
        </div>
        <div v-if="context.profile_freshness" class="rail-item-freshness">
          <ClockCircleOutlined class="freshness-icon" />
          <span>{{ formatFreshness(context.profile_freshness) }}</span>
        </div>
      </div>

      <!-- Resume -->
      <div class="rail-item" :class="{ 'rail-item--missing': !context.resume_version_id }">
        <div class="rail-item-header">
          <FileTextOutlined class="rail-item-icon" />
          <span class="rail-item-label">确认简历</span>
          <span v-if="context.resume_version_id" class="rail-item-status rail-item-status--active" aria-label="已选择">
            <CheckCircleFilled />
          </span>
          <span v-else class="rail-item-status rail-item-status--empty" aria-label="未选择">
            <MinusCircleOutlined />
          </span>
        </div>
        <div class="rail-item-body">
          <span v-if="context.resume_version_id" class="rail-item-id" :title="context.resume_version_id">
            {{ truncateId(context.resume_version_id) }}
          </span>
          <span v-else class="rail-item-placeholder">未选择简历</span>
        </div>
        <div v-if="context.resume_freshness" class="rail-item-freshness">
          <ClockCircleOutlined class="freshness-icon" />
          <span>{{ formatFreshness(context.resume_freshness) }}</span>
        </div>
      </div>

      <!-- Evidence -->
      <div class="rail-item" :class="{ 'rail-item--missing': !hasEvidence }">
        <div class="rail-item-header">
          <SearchOutlined class="rail-item-icon" />
          <span class="rail-item-label">证据材料</span>
          <span v-if="hasEvidence" class="rail-item-status rail-item-status--active" aria-label="已选择">
            <CheckCircleFilled />
          </span>
          <span v-else class="rail-item-status rail-item-status--empty" aria-label="未选择">
            <MinusCircleOutlined />
          </span>
        </div>
        <div class="rail-item-body">
          <span v-if="hasEvidence" class="rail-item-count">
            {{ context.evidence_ids.length }} 项已选
          </span>
          <span v-else class="rail-item-placeholder">未选择证据</span>
        </div>
      </div>
    </div>

    <!-- Context ID -->
    <div v-if="context.context_id" class="rail-footer">
      <div class="rail-context-id" :title="context.context_id">
        <span class="context-id-label">上下文 ID</span>
        <code class="context-id-value">{{ truncateId(context.context_id) }}</code>
      </div>
    </div>
  </aside>
</template>

<script setup>
import { computed } from 'vue'
import {
  BankOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
  ExclamationCircleOutlined,
  FileTextOutlined,
  MinusCircleOutlined,
  SearchOutlined,
  UserOutlined,
} from '@ant-design/icons-vue'

const props = defineProps({
  context: {
    type: Object,
    default: () => ({
      context_id: '',
      job_id: '',
      profile_version_id: '',
      resume_version_id: '',
      evidence_ids: [],
      job_freshness: '',
      profile_freshness: '',
      resume_freshness: '',
    }),
  },
  stale: { type: Boolean, default: false },
  staleReason: { type: String, default: '' },
})

const hasEvidence = computed(() =>
  Array.isArray(props.context.evidence_ids) && props.context.evidence_ids.length > 0,
)

function truncateId(id) {
  if (!id) return ''
  if (id.length <= 12) return id
  return `${id.slice(0, 8)}...${id.slice(-4)}`
}

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
</script>

<style scoped>
/* ============================================
   ContextRail component styles
   ============================================ */

.context-rail {
  max-width: 100%;
}

/* Header */
.rail-header {
  margin-bottom: 16px;
}

.rail-eyebrow {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--text-tertiary);
  margin-bottom: 6px;
}

.rail-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.02em;
  margin: 0;
}

/* Stale warning */
.rail-stale-warning {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-radius: var(--radius-md);
  background: rgba(255, 149, 0, 0.08);
  border: 1px solid rgba(255, 149, 0, 0.15);
  margin-bottom: 16px;
}

.stale-icon {
  font-size: 15px;
  color: #cc7700;
  flex-shrink: 0;
}

.stale-text {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}

/* Context items */
.rail-items {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.rail-item {
  padding: 14px 16px;
  border-radius: var(--radius-md);
  background: var(--surface-secondary);
  border: 1px solid var(--border-light);
  transition: border-color 0.3s ease;
}

.rail-item--missing {
  border-style: dashed;
  opacity: 0.7;
}

.rail-item-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.rail-item-icon {
  font-size: 14px;
  color: var(--text-tertiary);
}

.rail-item-label {
  flex: 1;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.rail-item-status {
  font-size: 14px;
}

.rail-item-status--active {
  color: var(--accent-green);
}

.rail-item-status--empty {
  color: var(--text-tertiary);
  opacity: 0.5;
}

.rail-item-body {
  margin-bottom: 4px;
}

.rail-item-id {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  font-family: 'Geist Mono', 'SF Mono', 'Monaco', 'Inconsolata', 'Fira Code', monospace;
}

.rail-item-count {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.rail-item-placeholder {
  font-size: 13px;
  color: var(--text-tertiary);
  font-style: italic;
}

.rail-item-freshness {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.freshness-icon {
  font-size: 11px;
}

/* Footer with context ID */
.rail-footer {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border-light);
}

.rail-context-id {
  display: flex;
  align-items: center;
  gap: 8px;
}

.context-id-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.context-id-value {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  font-family: 'Geist Mono', 'SF Mono', 'Monaco', 'Inconsolata', 'Fira Code', monospace;
  background: var(--surface-secondary);
  padding: 2px 8px;
  border-radius: var(--radius-xs);
}

/* ============================================
   Responsive
   ============================================ */

@media (max-width: 768px) {
  .rail-items {
    flex-direction: row;
    flex-wrap: wrap;
  }

  .rail-item {
    flex: 1;
    min-width: 140px;
  }
}

/* ============================================
   Reduced motion
   ============================================ */

@media (prefers-reduced-motion: reduce) {
  .rail-item {
    transition: none;
  }
}
</style>
