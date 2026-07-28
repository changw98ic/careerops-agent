<template>
  <a-card class="smart-intake-panel" :bordered="false">
    <div class="smart-intake-heading">
      <div>
        <div class="smart-intake-kicker">AI 辅助预填 · 人工确认</div>
        <h3>{{ title }}</h3>
        <p class="card-hint">只生成可撤回的草稿建议，不会保存画像、启动 Agent 或触发外部操作。</p>
      </div>
      <a-tag color="orange">Review-only</a-tag>
    </div>

    <a-alert
      v-if="!effectiveFeatureEnabled"
      type="info"
      show-icon
      message="智能预填当前关闭"
      description="当前环境保留手工表单路径；如需启用，必须同时由后端能力开关和前端发布配置明确开启。"
    />

    <a-alert
      v-else-if="missingContext.length"
      type="warning"
      show-icon
      message="开始智能预填前还需要补齐上下文"
      :description="`请先选择${missingContext.join('、')}；手工表单仍可继续使用。`"
    />

    <a-textarea
      v-model:value="text"
      :rows="target === 'profile' ? 4 : 3"
      :maxlength="target === 'profile' ? 12000 : 2000"
      show-count
      :placeholder="placeholder"
      :disabled="!effectiveFeatureEnabled || capabilityLoading || loading || applying"
    />
    <div class="smart-intake-actions">
      <a-button
        type="primary"
        :loading="loading"
        :disabled="!canCreate"
        @click="createPreview({ regenerate: Boolean(preview) })"
      >
        <RobotOutlined /> 生成智能预填建议
      </a-button>
      <span class="muted">输入只作为本次预览的非可信文本；不会成为证据或执行指令。</span>
    </div>

    <a-alert
      v-if="error"
      class="smart-intake-alert"
      type="warning"
      show-icon
      :message="error"
      closable
      @close="error = ''"
    />

    <template v-if="preview">
      <a-divider />
      <div class="smart-intake-result-head">
        <div>
          <strong>建议预览</strong>
          <span class="muted"> · {{ stateLabel(preview.state) }}</span>
        </div>
        <a-tag v-if="preview.model_id === 'disabled'" color="default">模型未启用</a-tag>
        <a-tag v-else color="blue">{{ preview.model_id }}</a-tag>
        <a-button size="small" @click="dismissPreview">关闭预览</a-button>
      </div>

      <a-alert
        v-if="preview.state !== 'ready'"
        type="info"
        show-icon
        :message="stateLabel(preview.state)"
        description="手工表单仍然可用；此状态没有可直接采纳的字段。"
      />
      <a-button
        v-if="['stale', 'expired', 'revoked'].includes(preview.state)"
        class="smart-intake-regenerate"
        size="small"
        @click="createPreview({ regenerate: true })"
      >
        重新生成预览
      </a-button>
      <div v-else-if="reviewFields.length === 0" class="smart-intake-empty muted">
        没有返回可审查字段，请继续手工填写。
      </div>
      <div v-else class="smart-intake-fields">
        <div v-for="field in reviewFields" :key="field.path" class="smart-intake-field">
          <div class="smart-intake-field-title">
            <a-checkbox
              v-if="field.status === 'proposed'"
              v-model:checked="selected[field.path]"
              :disabled="applied || unknownSelected[field.path]"
              @change="unknownSelected[field.path] = false"
            >
              <span class="smart-intake-path">{{ labelFor(field.path) }}</span>
            </a-checkbox>
            <span v-else class="smart-intake-path">{{ labelFor(field.path) }}</span>
            <a-tag :color="statusColor(field.status)">{{ statusLabel(field.status) }}</a-tag>
            <a-button
              v-if="field.status === 'proposed' && !applied"
              size="small"
              @click="setUnknown(field.path, !unknownSelected[field.path])"
            >
              {{ unknownSelected[field.path] ? '恢复建议' : '标记未知' }}
            </a-button>
          </div>
          <div class="smart-intake-value">
            <a-input
              v-if="isTextField(field)"
              v-model:value="edited[field.path]"
              size="small"
              :aria-label="`编辑${labelFor(field.path)}`"
              :disabled="applied || field.status !== 'proposed' || !selected[field.path]"
            />
            <a-input-number
              v-else-if="field.value_type === 'integer'"
              v-model:value="edited[field.path]"
              size="small"
              :min="0"
              :max="500"
              :aria-label="`编辑${labelFor(field.path)}`"
              :disabled="applied || field.status !== 'proposed' || !selected[field.path]"
            />
            <span v-else class="mono">{{ displayValue(edited[field.path]) }}</span>
          </div>
          <div class="smart-intake-meta">
            <span>置信度 {{ Math.round(Number(field.confidence || 0) * 100) }}%</span>
            <span v-if="field.source_refs?.length">来源：{{ sourceSummary(field.source_refs) }}</span>
            <span v-else>来源：无可验证片段</span>
          </div>
          <div class="smart-intake-reason">{{ field.reason || '来源已绑定到本次输入' }}</div>
        </div>
      </div>

      <div v-if="preview.state === 'ready' && reviewFields.length" class="smart-intake-apply">
        <a-button type="primary" :loading="applying" :disabled="applied" @click="applyPreview">
          采纳选中建议（仅写入当前草稿）
        </a-button>
        <a-tag v-if="applied" color="green">本次选择已记录</a-tag>
        <span v-if="conflictCount" class="smart-intake-conflict">有 {{ conflictCount }} 项因表单已修改而保留原值</span>
      </div>
    </template>
  </a-card>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { RobotOutlined } from '@ant-design/icons-vue'
import { api, formatApiError } from '../api/client.js'

const props = defineProps({
  target: { type: String, required: true },
  context: { type: Object, default: () => ({}) },
  baseSnapshot: { type: Object, default: () => ({}) },
  currentSnapshot: { type: Object, default: null },
  featureEnabled: { type: Boolean, default: false },
  title: { type: String, default: '把自然语言变成可审查的表单草稿' },
})

const emit = defineEmits(['applied'])
const text = ref('')
const preview = ref(null)
const selected = ref({})
const unknownSelected = ref({})
const edited = ref({})
const baseline = ref(null)
const loading = ref(false)
const applying = ref(false)
const error = ref('')
const conflictCount = ref(0)
const applied = ref(false)
const capabilityLoading = ref(false)
const backendReleased = ref(false)
const createIdempotencyKey = ref(randomKey())
const applyIdempotencyKey = ref('')

const title = computed(() => props.title)
const reviewFields = computed(() => preview.value?.fields || [])
const effectiveFeatureEnabled = computed(
  () => props.featureEnabled && backendReleased.value,
)
const missingContext = computed(() => {
  if (props.target !== 'interview_context') return []
  return [
    ['canonical_job_id', '目标职位'],
    ['job_version_id', '职位版本'],
    ['resume_version_id', '已确认简历'],
  ]
    .filter(([key]) => !props.context?.[key])
    .map(([, label]) => label)
})
const canCreate = computed(
  () =>
    effectiveFeatureEnabled.value &&
    !missingContext.value.length &&
    Boolean(text.value.trim()) &&
    !loading.value &&
    !capabilityLoading.value,
)
const placeholder = computed(() =>
  props.target === 'profile'
    ? '例如：我想找上海或远程的后端/平台工程岗位，偏 Senior，关键词包括 Python、PostgreSQL；不要推断薪酬、签证或硬性排除。'
    : '例如：我最担心系统设计环节，希望重点练习容量估算，并用我做过的项目举例。',
)

watch(
  () => props.baseSnapshot,
  () => {
    if (!preview.value) baseline.value = clone(props.baseSnapshot)
  },
  { deep: true },
)

onMounted(async () => {
  if (!props.featureEnabled) return
  capabilityLoading.value = true
  try {
    const capability = await api.getSmartIntakeCapability()
    backendReleased.value = capability?.released === true
  } catch {
    // The manual form remains the safe fallback when capability discovery is
    // unavailable or the backend has not wired smart intake.
    backendReleased.value = false
  } finally {
    capabilityLoading.value = false
  }
})

async function createPreview({ regenerate = false } = {}) {
  if (!canCreate.value) return
  if (regenerate) createIdempotencyKey.value = randomKey()
  loading.value = true
  error.value = ''
  preview.value = null
  conflictCount.value = 0
  applied.value = false
  baseline.value = clone(props.baseSnapshot)
  try {
    const context = props.context || {}
    const body = {
      target: props.target,
      input: { kind: 'text', text: text.value },
      idempotency_key: createIdempotencyKey.value,
    }
    if (props.target === 'profile' && context.profile_version_id) {
      body.context_refs = { profile_version_id: context.profile_version_id }
    }
    if (props.target === 'interview_context') {
      body.interview_refs = {
        canonical_job_id: context.canonical_job_id,
        job_version_id: context.job_version_id,
        resume_version_id: context.resume_version_id,
        profile_version_id: context.profile_version_id || null,
        evidence_ids: context.evidence_ids || [],
      }
    }
    const result = await api.createSmartIntakePreview(body)
    preview.value = result
    selected.value = Object.fromEntries(
      (result.fields || []).map((field) => [field.path, field.status === 'proposed']),
    )
    unknownSelected.value = Object.fromEntries(
      (result.fields || []).map((field) => [field.path, false]),
    )
    edited.value = Object.fromEntries(
      (result.fields || []).map((field) => [field.path, clone(field.value)]),
    )
    applyIdempotencyKey.value = randomKey()
  } catch (err) {
    error.value = formatApiError(err, '智能预填暂不可用，仍可手工填写。')
  } finally {
    loading.value = false
  }
}

async function applyPreview() {
  if (!preview.value || applied.value || preview.value.state !== 'ready') return
  applying.value = true
  error.value = ''
  const decisions = reviewFields.value.map((field) => {
    if (field.status !== 'proposed') {
      return {
        path: field.path,
        decision: 'unknown',
        value: null,
        reason: '字段未通过来源或目标字段校验，保持未知',
      }
    }
    const value = edited.value[field.path]
    const changed = stableStringify(value) !== stableStringify(field.value)
    return {
      path: field.path,
      decision: unknownSelected.value[field.path]
        ? 'unknown'
        : selected.value[field.path]
          ? changed
            ? 'edit'
            : 'accept'
          : 'reject',
      value: selected.value[field.path] ? value : null,
      reason: unknownSelected.value[field.path]
        ? '用户要求保持未知，不采纳该建议'
        : selected.value[field.path]
          ? '用户确认智能预填建议'
          : '用户拒绝智能预填建议',
    }
  })
  const canonical = [...decisions].sort((a, b) => {
    const left = `${a.path}\u0000${a.decision}\u0000${stableStringify(a.value)}`
    const right = `${b.path}\u0000${b.decision}\u0000${stableStringify(b.value)}`
    return left < right ? -1 : left > right ? 1 : 0
  })
  try {
    const result = await api.applySmartIntakePreview(preview.value.preview_id, {
      apply_idempotency_key: applyIdempotencyKey.value,
      context_digest: preview.value.context_digest,
      decision_set_hash: await sha256(stableStringify(canonical)),
      decisions,
    })
    const patch = result.draft_patch?.fields || {}
    const current = props.currentSnapshot || props.baseSnapshot
    conflictCount.value = Object.keys(patch).filter(
      (path) => !sameValue(getAt(current, path), getAt(baseline.value, path)),
    ).length
    emit('applied', { patch, baseline: clone(baseline.value), result, conflictCount: conflictCount.value })
    applied.value = true
  } catch (err) {
    if (err?.code === 'STALE_SMART_INTAKE_PREVIEW') {
      preview.value = preview.value ? { ...preview.value, state: 'stale' } : null
    } else if (err?.code === 'SMART_PREVIEW_EXPIRED') {
      preview.value = preview.value ? { ...preview.value, state: 'expired' } : null
    }
    error.value = formatApiError(err, '预填建议未能应用，请重新生成预览。')
  } finally {
    applying.value = false
  }
}

function isTextField(field) {
  return field.value_type === 'string'
}

function statusLabel(status) {
  return { proposed: '可审查', unknown: '未知', blocked: '已拦截' }[status] || '未知'
}

function statusColor(status) {
  return { proposed: 'blue', unknown: 'gold', blocked: 'red' }[status] || 'default'
}

function sourceSummary(refs) {
  const normalized = normalizeInput(text.value)
  return refs
    .map((source) => {
      const start = source.start_offset
      const end = source.end_offset
      const value = Array.from(normalized).slice(start, end).join('')
      return value ? `“${value}” (${start}-${end})` : `${start}-${end}`
    })
    .join('、')
}

function labelFor(path) {
  return path
    .replace('target_roles[', '目标职位 #')
    .replace('locations[', '地点 #')
    .replace('].title', ' · 标题')
    .replace('].seniority', ' · 级别')
    .replace('].notes', ' · 备注')
    .replace('].name', ' · 名称')
    .replace('].radius_km', ' · 半径 km')
    .replace('include_keywords[', '包含关键词 #')
    .replace('exclude_keywords[', '排除关键词 #')
    .replace('user_context', '面试上下文')
}

function stateLabel(state) {
  return {
    unavailable: '模型未启用或暂不可用',
    abstained: '模型选择不作建议',
    invalid: '模型输出未通过结构校验',
    ready: '建议已通过基础来源校验',
    stale: '上下文已变化，需要重新生成',
    expired: '预览已过期，需要重新生成',
    revoked: '预览已撤回，需要重新生成',
  }[state] || '预览状态未知'
}

function displayValue(value) {
  return Array.isArray(value) ? value.join('、') : String(value ?? '')
}

function dismissPreview() {
  preview.value = null
  selected.value = {}
  unknownSelected.value = {}
  edited.value = {}
  applied.value = false
  conflictCount.value = 0
  error.value = ''
}

function setUnknown(path, value) {
  unknownSelected.value[path] = value
  if (value) selected.value[path] = false
}

function pathParts(path) {
  return path.replaceAll('[', '.').replaceAll(']', '').split('.').filter(Boolean)
}

function getAt(root, path) {
  return pathParts(path).reduce((current, part) => current?.[part], root)
}

function sameValue(left, right) {
  return stableStringify(left) === stableStringify(right)
}

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value))
}

function randomKey() {
  return globalThis.crypto?.randomUUID?.() || `smart-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

async function sha256(value) {
  const bytes = new TextEncoder().encode(value)
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

function normalizeInput(value) {
  return String(value || '').normalize('NFC').replace(/\r\n/g, '\n').replace(/\r/g, '\n')
}
</script>

<style scoped>
.smart-intake-panel {
  margin-bottom: 16px;
  border: 1px solid rgba(194, 65, 12, 0.18);
  background: linear-gradient(180deg, #fffaf5 0%, #ffffff 100%);
}
.smart-intake-heading,
.smart-intake-result-head,
.smart-intake-actions,
.smart-intake-apply {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.smart-intake-heading h3 { margin: 4px 0; }
.smart-intake-kicker {
  color: var(--accent-primary);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.smart-intake-actions { justify-content: flex-start; margin-top: 12px; }
.smart-intake-alert { margin-top: 12px; }
.smart-intake-fields { display: grid; gap: 8px; margin-top: 12px; }
.smart-intake-field {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(180px, 2fr);
  gap: 4px 12px;
  padding: 10px 12px;
  border: 1px solid #eee7df;
  border-radius: 8px;
  background: #fff;
}
.smart-intake-path { font-weight: 600; }
.smart-intake-field-title,
.smart-intake-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.smart-intake-value { min-width: 0; }
.smart-intake-meta {
  grid-column: 2;
  color: var(--color-tertiary);
  font-size: 12px;
}
.smart-intake-reason {
  grid-column: 2;
  color: var(--color-tertiary);
  font-size: 12px;
}
.smart-intake-apply { justify-content: flex-start; margin-top: 16px; }
.smart-intake-conflict { color: var(--color-warning); font-size: 12px; }
.smart-intake-empty { padding: 18px 0; text-align: center; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
@media (max-width: 640px) {
  .smart-intake-field { grid-template-columns: 1fr; }
  .smart-intake-reason { grid-column: 1; }
}
</style>
