<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>证据评审</h1>
        <p>确认或拒绝从简历提取的候选声明。模型提议仅作评审，不构成可信证据。</p>
      </div>
      <a-tag color="blue">{{ filtered.length }} 条</a-tag>
    </div>

    <a-alert
      v-if="unavailable"
      type="warning"
      show-icon
      message="证据服务暂不可用"
      description="后端仓储未就绪（503）。请稍后重试。"
      closable
      @close="unavailable = false"
    />
    <a-alert v-if="error" type="error" show-icon :message="error" closable @close="error = ''" />

    <a-card class="table-card">
      <div class="toolbar">
        <div class="toolbar-controls">
          <a-select v-model:value="statusFilter" style="width: 180px" @change="loadEvidence">
            <a-select-option value="">全部</a-select-option>
            <a-select-option value="unconfirmed">待评审</a-select-option>
            <a-select-option value="confirmed">已确认</a-select-option>
            <a-select-option value="rejected">已拒绝</a-select-option>
          </a-select>
          <a-checkbox v-model:checked="onlyModel">仅看模型提议</a-checkbox>
        </div>
        <a-button :loading="loading" @click="loadEvidence">
          <ReloadOutlined /> 刷新
        </a-button>
      </div>

      <a-spin :spinning="loading && evidence.length === 0">
        <a-list
          v-if="filtered.length > 0 || !loading"
          :data-source="filtered"
          :loading="false"
        >
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta>
                <template #title>
                  <a-space wrap>
                    <strong>{{ item.name }}</strong>
                    <a-tag>{{ item.kind }}</a-tag>
                    <a-tag :color="statusColor(item.confirmation_status)">
                      {{ statusLabel(item.confirmation_status) }}
                    </a-tag>
                    <a-tag v-if="isModelProposed(item)" color="purple">
                      模型提议 (review-only)
                    </a-tag>
                    <a-tag v-else color="blue">确定性提取</a-tag>
                    <a-tag v-if="item.resume_version_id" color="default">
                      resume v-link
                    </a-tag>
                  </a-space>
                </template>
                <template #description>
                  <div v-if="item.description" class="evidence-desc">{{ item.description }}</div>
                  <div class="evidence-meta">
                    <span class="muted">extractor:</span>
                    <span class="mono">{{ item.extractor_version || '-' }}</span>
                    <span class="meta-sep">·</span>
                    <span class="muted">evidence hash:</span>
                    <span class="mono">{{ shortHash(item.evidence_hash) }}</span>
                  </div>
                  <div v-if="item.source_span" class="source-span">
                    <span class="muted">source span:</span>
                    <code>{{ item.source_span }}</code>
                  </div>
                  <div v-if="isModelProposed(item)" class="model-warning">
                    ⚠ 该声明由模型生成且无可信来源，未确认前不会进入任何投递包。
                  </div>
                </template>
              </a-list-item-meta>

              <template #actions>
                <span class="actions-cell">
                  <a-button
                    v-if="item.confirmation_status !== 'confirmed'"
                    type="link"
                    size="small"
                    :loading="actingId === item.id + ':confirm'"
                    @click="confirm(item)"
                  >
                    确认
                  </a-button>
                  <a-button
                    v-if="item.confirmation_status !== 'rejected'"
                    type="link"
                    size="small"
                    danger
                    :loading="actingId === item.id + ':reject'"
                    @click="reject(item)"
                  >
                    拒绝
                  </a-button>
                </span>
              </template>
            </a-list-item>
          </template>
          <template #emptyText>
            <a-empty :description="emptyText" />
          </template>
        </a-list>
      </a-spin>
    </a-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { api, formatApiError, parseApiError } from '../api/client.js'

const route = useRoute()

const evidence = ref([])
const statusFilter = ref('')
const onlyModel = ref(false)
const loading = ref(false)
const actingId = ref('')
const error = ref('')
const unavailable = ref(false)

// Deterministic extractor tag (mirrors resume_service.RESUME_EXTRACTOR_VERSION).
// Anything that is NOT this is treated as a model proposal (review-only) per
// Iron Rule 4: model-produced interpretations are PROPOSALS, never evidence.
const DETERMINISTIC_PREFIX = 'resume-analysis'

function isModelProposed(item) {
  const v = (item.extractor_version || '').toLowerCase()
  return !v.startsWith(DETERMINISTIC_PREFIX)
}

const filtered = computed(() => {
  if (!onlyModel.value) return evidence.value
  return evidence.value.filter(isModelProposed)
})

const emptyText = computed(() => {
  if (statusFilter.value === 'unconfirmed') return '没有待评审的证据'
  if (statusFilter.value === 'confirmed') return '尚无已确认的证据'
  if (statusFilter.value === 'rejected') return '尚无已拒绝的证据'
  if (onlyModel.value) return '当前没有模型提议（确定性提取均带 resume-analysis 标记）'
  return '尚无证据 — 上传并确认简历后会自动提取证据'
})

async function loadEvidence() {
  loading.value = true
  error.value = ''
  unavailable.value = false
  try {
    const params = {}
    if (statusFilter.value) params.status = statusFilter.value
    // If routed from Resumes.vue with ?resume=, scope the list client-side.
    const resumeFilter = route.query.resume
    const data = await api.listEvidence(params)
    let items = data.items || []
    if (resumeFilter) {
      items = items.filter((i) => i.resume_version_id === resumeFilter)
    }
    // Defensive shallow copy so optimistic local updates never mutate the
    // caller's objects (and stays immune to shared-reference test fixtures).
    evidence.value = items.map((i) => ({ ...i }))
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else {
      error.value = formatApiError(err, '加载证据失败。')
    }
  } finally {
    loading.value = false
  }
}

async function confirm(item) {
  await act(item, 'confirm', () => api.confirmEvidence(item.id))
}
async function reject(item) {
  await act(item, 'reject', () => api.rejectEvidence(item.id))
}

async function act(item, kind, fn) {
  actingId.value = item.id + ':' + kind
  error.value = ''
  try {
    const result = await fn()
    // Idempotency surface: was_change=false means the decision was already this
    // state — no audit event was appended (Iron Rule 7). Show it explicitly.
    if (result.was_change) {
      message.success(`已${kind === 'confirm' ? '确认' : '拒绝'}：${item.name}`)
    } else {
      message.info(`无变更：该证据已处于「${statusLabel(result.status)}」状态`)
    }
    // Update local row without a full reload.
    const idx = evidence.value.findIndex((e) => e.id === item.id)
    if (idx >= 0) {
      evidence.value[idx] = { ...evidence.value[idx], confirmation_status: result.status }
      // Re-filter if a status filter is active (drop the row if it no longer matches).
      if (statusFilter.value && result.status !== statusFilter.value) {
        evidence.value.splice(idx, 1)
      }
    }
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else {
      error.value = formatApiError(err, '操作失败。')
    }
  } finally {
    actingId.value = ''
  }
}

function statusColor(s) {
  return { confirmed: 'green', unconfirmed: 'gold', rejected: 'red' }[s] || 'default'
}
function statusLabel(s) {
  return { confirmed: '已确认', unconfirmed: '待评审', rejected: '已拒绝' }[s] || s || '未知'
}

function shortHash(h) {
  if (!h) return '-'
  return h.slice(0, 10) + '…'
}

onMounted(loadEvidence)
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  gap: 12px;
  flex-wrap: wrap;
}
.toolbar-controls {
  display: flex;
  gap: 16px;
  align-items: center;
  flex-wrap: wrap;
}
.evidence-desc {
  margin-bottom: 4px;
  color: #344054;
}
.evidence-meta {
  font-size: 12px;
  color: #667085;
}
.meta-sep {
  margin: 0 6px;
}
.source-span {
  margin-top: 4px;
  font-size: 12px;
}
.source-span code {
  display: block;
  background: #f5f7fb;
  border: 1px solid #eaecf0;
  border-radius: 4px;
  padding: 4px 8px;
  margin-top: 2px;
  color: #475467;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.model-warning {
  margin-top: 6px;
  color: #722ed1;
  font-size: 12px;
}
.actions-cell {
  display: inline-flex;
  gap: 4px;
}
.muted {
  color: #667085;
}
</style>
