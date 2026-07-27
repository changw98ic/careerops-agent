<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>回复评审队列</h1>
        <p>
          评审招聘邮件的回复草稿：收件人、线程上下文、风险类别、证据与确认动作。
          仅获批准的低风险回复可通过受控发送链路发送；高风险类别永久拒绝系统发送。
        </p>
      </div>
      <a-space>
        <a-select
          v-model:value="stateFilter"
          style="width: 160px"
          placeholder="按状态筛选"
          allow-clear
          @change="refresh"
        >
          <a-select-option value="draft">草稿</a-select-option>
          <a-select-option value="pending_review">待评审</a-select-option>
          <a-select-option value="approved">已批准</a-select-option>
          <a-select-option value="rejected">已拒绝</a-select-option>
        </a-select>
        <a-button :loading="loading" @click="refresh">刷新</a-button>
      </a-space>
    </div>

    <a-alert
      v-if="unavailable"
      type="warning"
      show-icon
      message="回复草稿服务暂不可用"
      description="后端依赖未就绪（503）。请稍后重试。"
      closable
      @close="unavailable = false"
    />
    <a-alert
      v-if="error"
      type="error"
      show-icon
      :message="error"
      closable
      @close="error = ''"
    />

    <a-spin :spinning="loading && drafts.length === 0">
      <a-empty v-if="!drafts.length && !loading" description="暂无回复草稿" />
      <a-row :gutter="16">
        <a-col :xs="24" :lg="selected ? 14 : 24">
          <a-card title="草稿列表" class="section-card">
            <a-list :data-source="drafts" :locale="{ emptyText: ' ' }">
              <template #renderItem="{ item }">
                <a-list-item>
                  <a-list-item-meta>
                    <template #title>
                      <a-space>
                        <span class="draft-subject">{{ item.subject || '(无主题)' }}</span>
                        <a-tag :color="riskColor(item.risk_category)">
                          {{ riskLabel(item.risk_category) }}
                        </a-tag>
                        <a-tag :color="stateColor(item.approval_state)">
                          {{ stateLabel(item.approval_state) }}
                        </a-tag>
                      </a-space>
                    </template>
                    <template #description>
                      收件人: {{ item.recipient }}
                      <span v-if="item.application_id"> · 申请 {{ item.application_id.slice(0, 8) }}</span>
                      <span v-if="item.validation_issues.length">
                        · <a-typography-text type="danger">
                          {{ item.validation_issues.length }} 项未支持断言
                        </a-typography-text>
                      </span>
                    </template>
                  </a-list-item-meta>
                  <template #actions>
                    <a-button size="small" @click="select(item)">评审</a-button>
                  </template>
                </a-list-item>
              </template>
            </a-list>
            <div v-if="nextCursor" class="pagination-row">
              <a-button :loading="loading" @click="loadMore">加载更多</a-button>
            </div>
          </a-card>
        </a-col>

        <a-col v-if="selected" :xs="24" :lg="10">
          <a-card title="草稿评审" class="section-card">
            <template #extra>
              <a-button size="small" type="text" @click="selected = null">关闭</a-button>
            </template>

            <!-- Thread context (task 13.3 bounded excerpt) -->
            <a-descriptions :column="1" size="small" bordered>
              <a-descriptions-item label="收件人">{{ selected.recipient }}</a-descriptions-item>
              <a-descriptions-item label="主题">{{ selected.subject }}</a-descriptions-item>
              <a-descriptions-item label="意图">{{ selected.intent }}</a-descriptions-item>
              <a-descriptions-item label="风险类别">
                <a-tag :color="riskColor(selected.risk_category)">
                  {{ riskLabel(selected.risk_category) }}
                </a-tag>
              </a-descriptions-item>
              <a-descriptions-item label="线程摘要">
                <span class="excerpt">{{ selected.context.thread_excerpt || '(无)' }}</span>
              </a-descriptions-item>
              <a-descriptions-item label="证据引用">
                <span v-if="selected.context.evidence_refs.length">
                  {{ selected.context.evidence_refs.length }} 项已确认证据
                </span>
                <span v-else>无</span>
              </a-descriptions-item>
            </a-descriptions>

            <!-- Draft body diff (task 13.5 version + payload hash) -->
            <div class="section-block">
              <div class="section-title">正文（版本 {{ selected.version_number }}）</div>
              <a-typography-paragraph>
                <pre class="draft-body">{{ selected.body }}</pre>
              </a-typography-paragraph>
              <div class="payload-hash">payload: {{ selected.payload_hash.slice(0, 16) }}…</div>
            </div>

            <!-- Unsupported-claim findings (task 13.4) -->
            <a-alert
              v-if="selected.validation_issues.length"
              type="warning"
              show-icon
              message="存在未支持的断言，需用户修正后方可批准"
            >
              <template #description>
                <ul class="issue-list">
                  <li v-for="(issue, idx) in selected.validation_issues" :key="idx">{{ issue }}</li>
                </ul>
              </template>
            </a-alert>

            <!-- Send outcome (task 13.6 / 13.8) -->
            <a-alert
              v-if="sendOutcome"
              :type="sendOutcome.phase === 'sent' ? 'success' : sendOutcome.phase === 'reconciliation_required' ? 'warning' : 'info'"
              show-icon
              :message="`发送状态: ${sendOutcome.phase}`"
            >
              <template #description>
                <span v-if="sendOutcome.denial_reasons.length">
                  拒绝原因: {{ sendOutcome.denial_reasons.join(', ') }}
                </span>
                <span v-else-if="sendOutcome.provider_resource_id">
                  provider: {{ sendOutcome.provider_resource_id }}
                </span>
              </template>
            </a-alert>

            <!-- Confirmation actions (task 13.6 mandatory review) -->
            <div class="actions-row">
              <a-button :loading="acting" @click="editDraft">编辑正文</a-button>
              <a-button
                type="primary"
                :loading="acting"
                :disabled="!!selected.validation_issues.length"
                @click="approve"
              >
                批准
              </a-button>
              <a-button :loading="acting" danger @click="reject">拒绝</a-button>
              <a-button
                :loading="acting"
                :disabled="!canSend"
                @click="send"
              >
                发送
              </a-button>
            </div>

            <a-modal
              v-model:open="editing"
              title="编辑回复正文"
              @ok="submitEdit"
              :confirm-loading="acting"
            >
              <a-form layout="vertical">
                <a-form-item label="主题">
                  <a-input v-model:value="editForm.subject" />
                </a-form-item>
                <a-form-item label="正文">
                  <a-textarea v-model:value="editForm.body" :rows="6" />
                </a-form-item>
              </a-form>
            </a-modal>
          </a-card>
        </a-col>
      </a-row>
    </a-spin>
  </div>
</template>

<script setup>
// Reply review queue (Section 13, task 13.8). Drafts are review-only; only an
// approved low-risk reply may be sent via the reused Section 10 chain. Auto-send
// is permanently disabled; high-risk categories permanently denied system send.
import { onMounted, ref, computed } from 'vue'
import { api, parseApiError } from '../api/client.js'

const loading = ref(false)
const acting = ref(false)
const unavailable = ref(false)
const error = ref('')
const drafts = ref([])
const nextCursor = ref(null)
const stateFilter = ref(null)
const selected = ref(null)
const editing = ref(false)
const editForm = ref({ subject: '', body: '' })
const sendOutcome = ref(null)

const canSend = computed(() => {
  if (!selected.value) return false
  if (selected.value.approval_state !== 'approved') return false
  // High-risk / permanently-denied categories can never be system-sent.
  return !['high_risk', 'permanently_denied'].includes(selected.value.risk_category)
})

function riskColor(category) {
  if (['high_risk', 'permanently_denied'].includes(category)) return 'red'
  if (category === 'scheduling') return 'blue'
  if (category === 'info_request') return 'cyan'
  return 'green'
}
function riskLabel(category) {
  return {
    low_risk: '低风险',
    scheduling: '日程协调',
    info_request: '信息补充',
    high_risk: '高风险',
    permanently_denied: '永久拒绝发送',
    unknown: '未知',
  }[category] || category
}
function stateColor(state) {
  return {
    draft: 'default',
    pending_review: 'processing',
    approved: 'green',
    rejected: 'red',
    expired: 'orange',
    superseded: 'orange',
  }[state] || 'default'
}
function stateLabel(state) {
  return {
    draft: '草稿',
    pending_review: '待评审',
    approved: '已批准',
    rejected: '已拒绝',
    expired: '已过期',
    superseded: '已取代',
  }[state] || state
}

function select(draft) {
  selected.value = draft
  sendOutcome.value = null
}

async function refresh() {
  loading.value = true
  error.value = ''
  unavailable.value = false
  nextCursor.value = null
  try {
    const params = { limit: 20 }
    if (stateFilter.value) params.state = stateFilter.value
    const res = await api.listReplyDrafts(params)
    drafts.value = res.items || []
    nextCursor.value = res.next_cursor || null
  } catch (err) {
    handleError(err)
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  if (!nextCursor.value) return
  loading.value = true
  try {
    const params = { limit: 20, cursor: nextCursor.value }
    if (stateFilter.value) params.state = stateFilter.value
    const res = await api.listReplyDrafts(params)
    drafts.value = [...drafts.value, ...(res.items || [])]
    nextCursor.value = res.next_cursor || null
  } catch (err) {
    handleError(err)
  } finally {
    loading.value = false
  }
}

async function approve() {
  acting.value = true
  try {
    const updated = await api.approveReplyDraft(selected.value.id)
    selected.value = updated
    await refresh()
  } catch (err) {
    handleError(err)
  } finally {
    acting.value = false
  }
}

async function reject() {
  acting.value = true
  try {
    const updated = await api.rejectReplyDraft(selected.value.id)
    selected.value = updated
    await refresh()
  } catch (err) {
    handleError(err)
  } finally {
    acting.value = false
  }
}

function editDraft() {
  editForm.value = { subject: selected.value.subject, body: selected.value.body }
  editing.value = true
}

async function submitEdit() {
  acting.value = true
  try {
    const updated = await api.editReplyDraft(selected.value.id, {
      subject: editForm.value.subject,
      body: editForm.value.body,
    })
    selected.value = updated
    editing.value = false
    await refresh()
  } catch (err) {
    handleError(err)
  } finally {
    acting.value = false
  }
}

async function send() {
  acting.value = true
  sendOutcome.value = null
  try {
    sendOutcome.value = await api.sendReplyDraft(selected.value.id, {
      account_email: '',
    })
    await refresh()
  } catch (err) {
    handleError(err)
  } finally {
    acting.value = false
  }
}

function handleError(err) {
  const parsed = parseApiError(err)
  if (parsed.status === 503) {
    unavailable.value = true
  } else {
    error.value = parsed.message || String(err)
  }
}

onMounted(refresh)
</script>

<style scoped>
.page-shell {
  padding: 16px 24px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}
.page-header h1 {
  margin: 0 0 4px;
  font-size: 20px;
}
.page-header p {
  margin: 0;
  color: rgba(0, 0, 0, 0.55);
  max-width: 720px;
}
.section-card {
  margin-bottom: 16px;
}
.draft-subject {
  font-weight: 600;
}
.excerpt {
  white-space: pre-wrap;
  color: rgba(0, 0, 0, 0.65);
  font-size: 13px;
}
.section-block {
  margin-top: 12px;
}
.section-title {
  font-weight: 600;
  margin-bottom: 4px;
}
.draft-body {
  background: rgba(0, 0, 0, 0.04);
  padding: 8px 12px;
  border-radius: 4px;
  white-space: pre-wrap;
  font-family: inherit;
  margin: 0;
}
.payload-hash {
  font-family: monospace;
  font-size: 11px;
  color: rgba(0, 0, 0, 0.4);
  margin-top: 4px;
}
.issue-list {
  margin: 0;
  padding-left: 18px;
}
.actions-row {
  margin-top: 16px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.pagination-row {
  margin-top: 12px;
  text-align: center;
}
</style>
