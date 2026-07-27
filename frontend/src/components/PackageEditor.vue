<template>
  <a-card title="投递包编辑器" class="package-editor detail-card">
    <!-- Top-level unavailable (503 on initial load) -->
    <a-alert
      v-if="unavailable"
      type="warning"
      show-icon
      message="包服务暂不可用"
      description="后端仓储未就绪（503）。请稍后重试。"
      closable
      @close="unavailable = false"
      style="margin-bottom: 12px"
    />

    <!-- Top-level non-fatal error -->
    <a-alert
      v-if="error"
      type="error"
      show-icon
      :message="error"
      closable
      @close="error = ''"
      style="margin-bottom: 12px"
    />

    <!-- Loading skeleton -->
    <div v-if="loading" class="package-loading">
      <a-spin size="large" />
      <span class="muted">正在加载投递包版本…</span>
    </div>

    <!-- Empty state: no version yet → create draft form -->
    <div v-else-if="!latest" class="package-empty">
      <a-empty description="尚未创建投递包草稿，请在下方完成基础信息后创建首版草稿。" />
      <a-form layout="vertical" class="draft-form">
        <a-row :gutter="16">
          <a-col :xs="24" :md="12">
            <a-form-item label="简历版本 (resume_version_id) *" required>
              <a-select
                v-if="eligibleResumes.length > 0"
                v-model:value="draft.resume_version_id"
                placeholder="选择已确认的简历版本"
                :loading="eligibleLoading"
                allow-clear
              >
                <a-select-option
                  v-for="r in eligibleResumes"
                  :key="r.id"
                  :value="r.id"
                >
                  v{{ r.version_number }} · {{ shortHash(r.content_hash) }} · {{ r.target_type || 'general' }}
                </a-select-option>
              </a-select>
              <a-input
                v-else
                v-model:value="draft.resume_version_id"
                placeholder="粘贴 resume version UUID（无可选简历时）"
              />
              <div v-if="eligibleError" class="form-hint form-hint--error">
                {{ eligibleError }}
              </div>
              <div v-else-if="eligibleResumes.length === 0 && !eligibleLoading" class="form-hint muted">
                暂无可用于投递的已确认简历，请先在「Resumes」页面确认简历内容，或直接粘贴 UUID。
              </div>
            </a-form-item>
          </a-col>
          <a-col :xs="24" :md="12">
            <a-form-item label="Job 版本 (可选)">
              <a-input
                v-model:value="draft.job_version_id"
                placeholder="job version UUID（可选）"
              />
            </a-form-item>
          </a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :xs="24" :md="12">
            <a-form-item label="Profile 版本 (可选)">
              <a-input
                v-model:value="draft.profile_version_id"
                placeholder="profile version UUID（可选）"
              />
            </a-form-item>
          </a-col>
          <a-col :xs="24" :md="12">
            <a-form-item label="备注 (可选)">
              <a-input
                v-model:value="draft.notes"
                placeholder="内部备注，不进入投递正文"
              />
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item label="求职信正文 (cover_letter_text)">
          <a-textarea
            v-model:value="draft.cover_letter_text"
            :rows="4"
            placeholder="求职信正文，将进入最终投递载荷。"
          />
        </a-form-item>

        <!-- Claims -->
        <div class="section-block">
          <div class="section-block__head">
            <span class="section-block__title">声明 (claims)</span>
            <a-button size="small" type="dashed" @click="addClaim">
              <PlusOutlined /> 新增声明
            </a-button>
          </div>
          <p class="muted section-block__hint">
            每条声明必须绑定至少一条已确认证据，否则审批将被拒绝（422）。
          </p>
          <div v-for="(c, idx) in draft.claims" :key="idx" class="claim-row">
            <a-textarea
              v-model:value="c.claim_text"
              :rows="2"
              placeholder="声明文本，例如「有 5 年后端经验」"
            />
            <a-input
              v-model:value="c.evidenceIdsText"
              placeholder="证据 UUID，逗号分隔（evidence_ids）"
              class="claim-row__evidence"
            />
            <a-button size="small" danger type="text" @click="removeClaim(idx)">
              <DeleteOutlined />
            </a-button>
          </div>
          <a-empty
            v-if="draft.claims.length === 0"
            :image="false"
            description="尚未添加声明 — 可留空创建草稿，但审批前必须为每条声明绑定证据。"
          />
        </div>

        <!-- Attachments -->
        <div class="section-block">
          <div class="section-block__head">
            <span class="section-block__title">附件 (attachments)</span>
            <a-button size="small" type="dashed" @click="addAttachment">
              <PlusOutlined /> 新增附件
            </a-button>
          </div>
          <p class="muted section-block__hint">
            附件以内容哈希（sha256 hex）引用，不在此上传文件本身。
          </p>
          <div v-for="(a, idx) in draft.attachments" :key="idx" class="attachment-row">
            <a-input
              v-model:value="a.name"
              placeholder="文件名，如 resume.pdf"
              class="attachment-row__name"
            />
            <a-input
              v-model:value="a.content_hash"
              placeholder="sha256 hex"
              class="attachment-row__hash"
            />
            <a-input
              v-model:value="a.media_type"
              placeholder="MIME（可选）"
              class="attachment-row__mime"
            />
            <a-input-number
              v-model:value="a.size_bytes"
              placeholder="字节大小"
              :min="0"
              class="attachment-row__size"
            />
            <a-button size="small" danger type="text" @click="removeAttachment(idx)">
              <DeleteOutlined />
            </a-button>
          </div>
          <a-empty
            v-if="draft.attachments.length === 0"
            :image="false"
            description="暂无附件 — 投递包可不带附件。"
          />
        </div>

        <a-space style="margin-top: 12px">
          <a-button
            type="primary"
            :loading="actionLoading === 'create'"
            :disabled="!canCreateDraft"
            @click="onCreateDraft"
          >
            创建草稿
          </a-button>
          <a-button @click="resetDraft">重置表单</a-button>
        </a-space>
      </a-form>
    </div>

    <!-- Latest version + review/approve flow -->
    <template v-else>
      <!-- Latest version summary -->
      <div class="latest-card">
        <div class="latest-card__head">
          <a-space wrap>
            <span class="latest-card__title">最新版本</span>
            <a-tag color="blue">v{{ latest.version_number }}</a-tag>
            <a-tag :color="approvalColor(latest.approval_state)">
              {{ approvalLabel(latest.approval_state) }}
            </a-tag>
            <span class="mono muted">payload {{ truncateHash(latest.payload_hash) }}</span>
            <span v-if="latest.approved_at" class="muted">
              审批于 {{ formatDateTime(latest.approved_at) }}
            </span>
            <span v-if="latest.approved_by" class="muted">
              by {{ latest.approved_by }}
            </span>
          </a-space>
          <a-space>
            <a-button size="small" :loading="loading" @click="loadVersions">
              <ReloadOutlined /> 刷新
            </a-button>
          </a-space>
        </div>

        <a-descriptions :column="2" size="small" :label-style="{ color: '#667085', width: '130px' }" style="margin-top: 8px">
          <a-descriptions-item label="resume 版本">
            <span class="mono">{{ shortId(latest.resume_version_id) }}</span>
          </a-descriptions-item>
          <a-descriptions-item v-if="latest.job_version_id" label="job 版本">
            <span class="mono">{{ shortId(latest.job_version_id) }}</span>
          </a-descriptions-item>
          <a-descriptions-item v-if="latest.profile_version_id" label="profile 版本">
            <span class="mono">{{ shortId(latest.profile_version_id) }}</span>
          </a-descriptions-item>
          <a-descriptions-item label="声明数">
            {{ latest.claims?.length || 0 }}
          </a-descriptions-item>
          <a-descriptions-item label="附件数">
            {{ latest.attachments?.length || 0 }}
          </a-descriptions-item>
          <a-descriptions-item label="diff 条目">
            {{ latest.diff?.length || 0 }}
          </a-descriptions-item>
        </a-descriptions>
      </div>

      <!-- Approval block (422 reasons surface here) -->
      <div class="approval-block">
        <a-alert
          v-if="approvalError"
          type="error"
          show-icon
          message="审批被拒绝 — 需解决以下问题后重试"
        >
          <template #description>
            <ul class="reason-list">
              <li v-for="(r, i) in approvalReasons" :key="i">{{ r }}</li>
            </ul>
            <span v-if="approvalReasons.length === 0">{{ approvalError }}</span>
          </template>
        </a-alert>

        <a-button
          v-if="!isApproved(latest)"
          type="primary"
          size="large"
          :loading="actionLoading === 'approve'"
          @click="onApprove"
        >
          <template #icon><CheckCircleOutlined /></template>
          审批通过 (Approve)
        </a-button>
        <a-alert
          v-else
          type="success"
          show-icon
          banner
          :message="`已审批通过 — payload_hash ${truncateHash(latest.payload_hash)} 已冻结，可用于投递。`"
        />
      </div>

      <!-- Warnings (pre-approval checklist, derived from latest version state) -->
      <div v-if="warnings.length > 0" class="warnings-banner">
        <div class="warnings-banner__title">审批前注意事项</div>
        <ul class="warning-list">
          <li v-for="(w, i) in warnings" :key="i" class="warning-item">
            <div v-for="(line, j) in w.lines" :key="j" :class="{ 'warning-subline muted': j > 0 }">
              {{ line }}
            </div>
          </li>
        </ul>
      </div>

      <!-- Cover letter / notes / answers -->
      <a-collapse :default-active-key="['cover']" class="package-collapse">
        <a-collapse-panel key="cover" header="求职信 / 备注 / 答题">
          <div class="kv-block">
            <h4>求职信正文</h4>
            <a-typography-paragraph v-if="latest.cover_letter_text" class="long-text">
              {{ latest.cover_letter_text }}
            </a-typography-paragraph>
            <span v-else class="muted">（未提供）</span>
          </div>
          <div class="kv-block">
            <h4>备注</h4>
            <a-typography-paragraph v-if="latest.notes" class="long-text">
              {{ latest.notes }}
            </a-typography-paragraph>
            <span v-else class="muted">（未提供）</span>
          </div>
          <div v-if="latest.answers && Object.keys(latest.answers).length > 0" class="kv-block">
            <h4>答题</h4>
            <a-descriptions :column="1" size="small" bordered>
              <a-descriptions-item
                v-for="(v, k) in latest.answers"
                :key="k"
                :label="String(k)"
              >
                {{ v }}
              </a-descriptions-item>
            </a-descriptions>
          </div>
        </a-collapse-panel>
      </a-collapse>

      <!-- Claims + evidence references -->
      <a-collapse :default-active-key="['claims']" class="package-collapse">
        <a-collapse-panel key="claims" :header="`声明与证据引用 (${latest.claims?.length || 0})`">
          <div v-if="latest.claims && latest.claims.length > 0" class="claims-list">
            <div v-for="(c, i) in latest.claims" :key="i" class="claim-item">
              <div class="claim-item__text">
                <span class="claim-item__idx">#{{ i + 1 }}</span>
                {{ c.claim_text }}
              </div>
              <div class="claim-item__evidence">
                <span class="muted">证据: </span>
                <a-tag
                  v-for="eid in c.evidence_ids || []"
                  :key="eid"
                  size="small"
                  color="blue"
                  class="evidence-tag"
                  @click="goToEvidence(eid)"
                >
                  {{ shortId(eid) }}
                </a-tag>
                <a-tag v-if="!c.evidence_ids || c.evidence_ids.length === 0" size="small" color="red">
                  缺少证据
                </a-tag>
              </div>
            </div>
          </div>
          <a-empty v-else :image="false" description="该版本未声明任何 claim。" />
        </a-collapse-panel>
      </a-collapse>

      <!-- Side-by-side diff -->
      <a-collapse :default-active-key="latest.diff && latest.diff.length > 0 ? ['diff'] : []" class="package-collapse">
        <a-collapse-panel key="diff" :header="`差异对比 (${latest.diff?.length || 0})`">
          <a-alert
            type="info"
            show-icon
            banner
            message="来源为 model 的条目仅作建议（review-only），未人工确认前不构成可信声明。"
            style="margin-bottom: 12px"
          />
          <div v-if="latest.diff && latest.diff.length > 0" class="diff-list">
            <div
              v-for="(d, i) in latest.diff"
              :key="i"
              class="diff-item"
              :class="{ 'diff-item--model': isModelSource(d.source) }"
            >
              <div class="diff-item__head">
                <span class="diff-item__section">{{ d.section }}</span>
                <a-tag :color="isModelSource(d.source) ? 'purple' : 'blue'">
                  {{ isModelSource(d.source) ? '模型建议 (advisory)' : '人工编辑' }}
                </a-tag>
              </div>
              <a-row :gutter="12" class="diff-item__body">
                <a-col :xs="24" :md="12">
                  <div class="diff-col diff-col--original">
                    <div class="diff-col__label">原文 (original)</div>
                    <a-typography-paragraph class="diff-col__text">
                      {{ d.original_text || '—' }}
                    </a-typography-paragraph>
                  </div>
                </a-col>
                <a-col :xs="24" :md="12">
                  <div class="diff-col diff-col--proposed">
                    <div class="diff-col__label">建议 (proposed)</div>
                    <a-typography-paragraph class="diff-col__text">
                      {{ d.proposed_text || '—' }}
                    </a-typography-paragraph>
                  </div>
                </a-col>
              </a-row>
              <div v-if="d.evidence_ids && d.evidence_ids.length > 0" class="diff-item__evidence">
                <span class="muted">证据: </span>
                <a-tag
                  v-for="eid in d.evidence_ids"
                  :key="eid"
                  size="small"
                  color="default"
                  class="evidence-tag"
                  @click="goToEvidence(eid)"
                >
                  {{ shortId(eid) }}
                </a-tag>
              </div>
            </div>
          </div>
          <a-empty v-else :image="false" description="该版本没有 diff 条目。" />
        </a-collapse-panel>
      </a-collapse>

      <!-- Attachments -->
      <a-collapse :default-active-key="latest.attachments && latest.attachments.length > 0 ? ['att'] : []" class="package-collapse">
        <a-collapse-panel key="att" :header="`附件 (${latest.attachments?.length || 0})`">
          <a-list
            v-if="latest.attachments && latest.attachments.length > 0"
            size="small"
            :data-source="latest.attachments"
            :split="false"
          >
            <template #renderItem="{ item }">
              <a-list-item>
                <a-list-item-meta>
                  <template #title>
                    <span class="attachment-name">{{ item.name }}</span>
                  </template>
                  <template #description>
                    <div class="attachment-meta">
                      <span class="mono">sha256:{{ truncateHash(item.content_hash) }}</span>
                      <span class="meta-sep">·</span>
                      <span class="muted">{{ item.media_type || '未标 MIME' }}</span>
                      <span class="meta-sep">·</span>
                      <span class="muted">{{ formatBytes(item.size_bytes) }}</span>
                    </div>
                  </template>
                </a-list-item-meta>
              </a-list-item>
            </template>
          </a-list>
          <a-empty v-else :image="false" description="该版本未引用附件。" />
        </a-collapse-panel>
      </a-collapse>

      <!-- Apply edits (produces a new draft) -->
      <a-collapse class="package-collapse">
        <a-collapse-panel key="edit" header="追加编辑（生成新草稿）">
          <a-alert
            type="info"
            show-icon
            banner
            message="在最新版本基础上追加编辑，会生成一个全新的草稿版本，并使之前的审批失效。"
            style="margin-bottom: 12px"
          />
          <div v-for="(e, idx) in editForm.edits" :key="idx" class="edit-row">
            <a-input
              v-model:value="e.section"
              placeholder="section，如 summary / experience_1"
              class="edit-row__section"
            />
            <a-input
              v-model:value="e.original_text"
              placeholder="原文（可选）"
              class="edit-row__text"
            />
            <a-input
              v-model:value="e.proposed_text"
              placeholder="建议文本"
              class="edit-row__text"
            />
            <a-input
              v-model:value="e.evidenceIdsText"
              placeholder="证据 UUID，逗号分隔"
              class="edit-row__evidence"
            />
            <a-select
              v-model:value="e.source"
              class="edit-row__source"
            >
              <a-select-option value="user">user</a-select-option>
              <a-select-option value="model">model (advisory)</a-select-option>
            </a-select>
            <a-button size="small" danger type="text" @click="removeEdit(idx)">
              <DeleteOutlined />
            </a-button>
          </div>
          <a-space style="margin-top: 12px" wrap>
            <a-button size="small" type="dashed" @click="addEdit">
              <PlusOutlined /> 添加一条编辑
            </a-button>
            <a-button
              type="primary"
              :loading="actionLoading === 'edit'"
              :disabled="editForm.edits.length === 0"
              @click="onApplyEdits"
            >
              应用编辑（生成新草稿）
            </a-button>
          </a-space>
        </a-collapse-panel>
      </a-collapse>

      <!-- Version history -->
      <a-collapse class="package-collapse">
        <a-collapse-panel key="history" :header="`版本历史 (${versions.length})`">
          <a-timeline>
            <a-timeline-item
              v-for="v in versions"
              :key="v.id"
              :color="historyTimelineColor(v.approval_state)"
            >
              <div class="history-item">
                <span class="history-item__title">v{{ v.version_number }}</span>
                <a-tag size="small" :color="approvalColor(v.approval_state)">
                  {{ approvalLabel(v.approval_state) }}
                </a-tag>
                <span class="mono muted">payload {{ truncateHash(v.payload_hash) }}</span>
                <div class="muted history-item__meta">
                  <span v-if="v.approved_at">审批于 {{ formatDateTime(v.approved_at) }}</span>
                  <span v-else>未审批</span>
                  <span class="meta-sep">·</span>
                  <span>{{ v.claims?.length || 0 }} 声明</span>
                  <span class="meta-sep">·</span>
                  <span>{{ v.attachments?.length || 0 }} 附件</span>
                </div>
              </div>
            </a-timeline-item>
          </a-timeline>
        </a-collapse-panel>
      </a-collapse>
    </template>
  </a-card>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  CheckCircleOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { api, parseApiError } from '../api/client.js'

const props = defineProps({
  applicationId: { type: String, required: true },
})
const emit = defineEmits(['approved', 'created'])

const router = useRouter()

// --- state ---
const loading = ref(true)
const unavailable = ref(false)
const error = ref('')
const versions = ref([])
const latest = ref(null)
const actionLoading = ref('')

// approval
const approvalError = ref('')

// create draft
const eligibleResumes = ref([])
const eligibleLoading = ref(false)
const eligibleError = ref('')
const draft = ref(emptyDraft())

// edit form
const editForm = ref({ edits: [] })

// --- computed ---
const canCreateDraft = computed(() => !!draft.value.resume_version_id)

const approvalReasons = computed(() => {
  if (!approvalError.value) return []
  // The backend 422 message typically lists reasons separated by '; ' or newlines.
  // Split on common delimiters to surface as a list.
  const raw = approvalError.value
  const parts = raw
    .split(/[\n;]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  return parts.length > 0 ? parts : [raw]
})

// Pre-approval warnings derived from the current latest version state.
// These explain WHY approval would be blocked, before the user clicks approve.
const warnings = computed(() => {
  if (!latest.value) return []
  const out = []

  // 1. Claims without evidence (hard block — 422 on approve)
  const claims = latest.value.claims || []
  const unevidenced = claims.filter(
    (c) => !c.evidence_ids || c.evidence_ids.length === 0,
  )
  if (unevidenced.length > 0) {
    out.push({
      lines: [
        `${unevidenced.length} 条声明缺少证据引用 — 审批将被拒绝（422）。`,
      ],
    })
  }

  // 2. Requirement gaps (non-empty gaps surface match failures)
  const gaps = latest.value.requirement_gaps || []
  if (gaps.length > 0) {
    const lines = [`${gaps.length} 条要求缺口（requirement_gaps）：`]
    for (const g of gaps) {
      lines.push(
        `[${matchLevelLabel(g.match_level)}] ${g.requirement_name}${g.reason ? ' — ' + g.reason : ''}`,
      )
    }
    out.push({ lines })
  }

  // 3. Model-sourced diff entries (advisory, not a hard block)
  const diffs = latest.value.diff || []
  const modelDiffs = diffs.filter(
    (d) => String(d.source || '').toLowerCase() === 'model',
  )
  if (modelDiffs.length > 0) {
    out.push({
      lines: [
        `${modelDiffs.length} 条 diff 条目来自模型（advisory，需人工确认后才能视为可信）。`,
      ],
    })
  }

  return out
})

// --- fetch ---
async function loadVersions() {
  loading.value = true
  error.value = ''
  unavailable.value = false
  try {
    const data = await api.listApplicationPackages(props.applicationId)
    const items = data.items || []
    versions.value = items
    // latest_id points to the newest; fall back to items[0]
    const latestId = data.latest_id
    latest.value = latestId
      ? items.find((v) => v.id === latestId) || items[0] || null
      : items[0] || null
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      unavailable.value = true
    } else if (parsed.status === 404) {
      // No package versions yet — not an error, just empty state.
      versions.value = []
      latest.value = null
    } else {
      error.value = parsed.message || '加载投递包版本失败。'
    }
  } finally {
    loading.value = false
  }
}

async function loadEligibleResumes() {
  eligibleLoading.value = true
  eligibleError.value = ''
  try {
    const data = await api.listEligibleResumes()
    eligibleResumes.value = data.items || []
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      eligibleError.value = '简历服务暂不可用，请稍后重试。'
    } else {
      eligibleError.value = parsed.message || '加载可选简历失败，可手动粘贴 UUID。'
    }
    eligibleResumes.value = []
  } finally {
    eligibleLoading.value = false
  }
}

// --- actions ---
async function onCreateDraft() {
  if (!canCreateDraft.value) return
  actionLoading.value = 'create'
  error.value = ''
  try {
    const body = {
      resume_version_id: draft.value.resume_version_id,
      claims: draft.value.claims.map((c) => ({
        claim_text: c.claim_text,
        evidence_ids: parseIdList(c.evidenceIdsText),
      })),
      attachments: draft.value.attachments.map((a) => ({
        name: a.name,
        content_hash: a.content_hash,
        media_type: a.media_type || '',
        size_bytes: typeof a.size_bytes === 'number' ? a.size_bytes : 0,
      })),
    }
    if (draft.value.job_version_id) body.job_version_id = draft.value.job_version_id
    if (draft.value.profile_version_id) body.profile_version_id = draft.value.profile_version_id
    if (draft.value.cover_letter_text) body.cover_letter_text = draft.value.cover_letter_text
    if (draft.value.notes) body.notes = draft.value.notes
    await api.createPackageDraft(props.applicationId, body)
    message.success('已创建首版草稿')
    resetDraft()
    await loadVersions()
    emit('created')
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      unavailable.value = true
    } else if (parsed.status === 422) {
      error.value = parsed.message || '草稿校验未通过（422）。'
    } else {
      error.value = parsed.message || '创建草稿失败，请稍后重试。'
    }
  } finally {
    actionLoading.value = ''
  }
}

async function onApprove() {
  if (!latest.value) return
  actionLoading.value = 'approve'
  approvalError.value = ''
  try {
    const result = await api.approvePackage(props.applicationId, latest.value.id)
    // Replace latest with the approved version inline.
    latest.value = result
    const idx = versions.value.findIndex((v) => v.id === result.id)
    if (idx >= 0) versions.value[idx] = result
    message.success(`v${result.version_number} 已审批通过 — payload_hash 已冻结`)
    emit('approved', result)
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      unavailable.value = true
    } else if (parsed.status === 422) {
      // 422 message lists the blocking reasons; surface inline.
      approvalError.value = parsed.message || '审批未通过（422），请处理阻塞项后重试。'
    } else {
      approvalError.value = parsed.message || '审批失败，请稍后重试。'
    }
  } finally {
    actionLoading.value = ''
  }
}

async function onApplyEdits() {
  if (!latest.value || editForm.value.edits.length === 0) return
  actionLoading.value = 'edit'
  error.value = ''
  try {
    const body = {
      edits: editForm.value.edits.map((e) => ({
        section: e.section,
        original_text: e.original_text || '',
        proposed_text: e.proposed_text || '',
        evidence_ids: parseIdList(e.evidenceIdsText),
        source: e.source || 'user',
      })),
    }
    const result = await api.applyPackageEdits(props.applicationId, latest.value.id, body)
    message.success(`已生成新草稿 v${result.version_number}（旧版本审批已失效）`)
    editForm.value = { edits: [] }
    approvalError.value = ''
    await loadVersions()
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      unavailable.value = true
    } else if (parsed.status === 422) {
      error.value = parsed.message || '编辑校验未通过（422）。'
    } else {
      error.value = parsed.message || '应用编辑失败，请稍后重试。'
    }
  } finally {
    actionLoading.value = ''
  }
}

// --- draft form helpers ---
function emptyDraft() {
  return {
    resume_version_id: '',
    job_version_id: '',
    profile_version_id: '',
    cover_letter_text: '',
    notes: '',
    claims: [],
    attachments: [],
  }
}

function resetDraft() {
  draft.value = emptyDraft()
}

function addClaim() {
  draft.value.claims.push({ claim_text: '', evidenceIdsText: '' })
}

function removeClaim(idx) {
  draft.value.claims.splice(idx, 1)
}

function addAttachment() {
  draft.value.attachments.push({
    name: '',
    content_hash: '',
    media_type: '',
    size_bytes: 0,
  })
}

function removeAttachment(idx) {
  draft.value.attachments.splice(idx, 1)
}

function addEdit() {
  editForm.value.edits.push({
    section: '',
    original_text: '',
    proposed_text: '',
    evidenceIdsText: '',
    source: 'user',
  })
}

function removeEdit(idx) {
  editForm.value.edits.splice(idx, 1)
}

// --- utilities ---
function parseIdList(text) {
  if (!text) return []
  return text
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function isModelSource(source) {
  return String(source || '').toLowerCase() === 'model'
}

function isApproved(v) {
  return String(v?.approval_state || '').toLowerCase() === 'approved'
}

function approvalColor(state) {
  const s = String(state || '').toLowerCase()
  return {
    draft: 'default',
    pending_review: 'gold',
    approved: 'green',
    rejected: 'red',
  }[s] || 'default'
}

function approvalLabel(state) {
  const s = String(state || '').toLowerCase()
  return {
    draft: '草稿',
    pending_review: '待评审',
    approved: '已审批',
    rejected: '已拒绝',
  }[s] || state || '-'
}

function matchLevelLabel(level) {
  const l = String(level || '').toLowerCase()
  return {
    strong: '完全匹配',
    partial: '部分匹配',
    unsupported: '不满足',
    missing: '缺失',
  }[l] || level || '-'
}

function historyTimelineColor(state) {
  const s = String(state || '').toLowerCase()
  if (s === 'approved') return 'green'
  if (s === 'rejected') return 'red'
  return 'gray'
}

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

function formatBytes(n) {
  if (n == null || isNaN(Number(n))) return '-'
  const num = Number(n)
  if (num < 1024) return num + ' B'
  if (num < 1024 * 1024) return (num / 1024).toFixed(1) + ' KB'
  return (num / (1024 * 1024)).toFixed(1) + ' MB'
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

function goToEvidence(eid) {
  router.push(`/evidence?highlight=${eid}`)
}

// --- lifecycle ---
onMounted(async () => {
  await loadVersions()
  // Load eligible resumes in the background (non-fatal if it fails).
  await loadEligibleResumes()
})
</script>

<style scoped>
.package-editor {
  margin-top: 24px;
}

.package-loading {
  display: grid;
  place-items: center;
  gap: 12px;
  min-height: 180px;
  color: #667085;
}

.package-empty {
  padding-top: 12px;
}

.draft-form {
  margin-top: 16px;
}

.form-hint {
  font-size: 12px;
  margin-top: 4px;
}
.form-hint--error {
  color: #d4380d;
}

/* Section blocks inside the create form */
.section-block {
  border: 1px dashed #d9d9d9;
  border-radius: 8px;
  padding: 12px 14px;
  margin-top: 16px;
  background: #fafafa;
}
.section-block__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.section-block__title {
  font-weight: 600;
  color: #101828;
}
.section-block__hint {
  margin: 0 0 8px;
}

.claim-row {
  display: grid;
  grid-template-columns: 1fr;
  gap: 6px;
  margin-bottom: 8px;
  padding: 8px;
  background: #fff;
  border: 1px solid #eaecf0;
  border-radius: 6px;
}
.claim-row__evidence {
  width: 100%;
}

.attachment-row {
  display: grid;
  grid-template-columns: 1.2fr 1.6fr 0.9fr 0.9fr auto;
  gap: 6px;
  margin-bottom: 8px;
  padding: 8px;
  background: #fff;
  border: 1px solid #eaecf0;
  border-radius: 6px;
  align-items: center;
}

/* Latest card */
.latest-card {
  border: 1px solid #eaecf0;
  border-radius: 8px;
  padding: 14px 16px;
  background: #f8fbff;
  margin-bottom: 16px;
}
.latest-card__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.latest-card__title {
  font-weight: 600;
  color: #101828;
  font-size: 15px;
}

/* Approval block */
.approval-block {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 16px;
}
.reason-list {
  margin: 4px 0 0;
  padding-left: 18px;
}
.reason-list li {
  margin: 2px 0;
}

/* Warnings banner (pre-approval checklist) */
.warnings-banner {
  border: 1px solid #ffe58f;
  background: #fffbe6;
  border-radius: 6px;
  padding: 10px 14px;
  margin-bottom: 16px;
}
.warnings-banner__title {
  font-weight: 600;
  color: #ad6800;
  margin-bottom: 6px;
}
.warning-list {
  margin: 0;
  padding-left: 20px;
}
.warning-item {
  color: #344054;
  font-size: 13px;
  margin: 4px 0;
  list-style: disc;
}
.warning-subline {
  font-size: 12px;
  padding-left: 12px;
}

/* Collapses */
.package-collapse {
  margin-top: 12px;
}

.kv-block {
  margin-bottom: 16px;
}
.kv-block h4 {
  margin: 0 0 6px;
  font-size: 13px;
  color: #101828;
}
.long-text {
  color: #344054;
  line-height: 1.7;
  white-space: pre-wrap;
  margin: 0;
}

/* Claims list */
.claims-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.claim-item {
  border: 1px solid #eaecf0;
  border-radius: 6px;
  padding: 10px 12px;
  background: #fff;
}
.claim-item__text {
  color: #344054;
  line-height: 1.6;
  margin-bottom: 6px;
}
.claim-item__idx {
  font-weight: 600;
  color: #667085;
  margin-right: 6px;
}
.claim-item__evidence {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

/* Diff */
.diff-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.diff-item {
  border: 1px solid #eaecf0;
  border-radius: 6px;
  padding: 10px 12px;
  background: #fff;
}
.diff-item--model {
  border-left: 3px solid #722ed1;
  background: #faf5ff;
}
.diff-item__head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.diff-item__section {
  font-weight: 600;
  color: #101828;
}
.diff-item__body {
  margin-top: 4px;
}
.diff-col {
  border: 1px solid #eaecf0;
  border-radius: 4px;
  padding: 8px 10px;
  background: #fafafa;
  height: 100%;
}
.diff-col--original {
  background: #fff7f0;
}
.diff-col--proposed {
  background: #f0f7ff;
}
.diff-col__label {
  font-size: 12px;
  color: #667085;
  margin-bottom: 4px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.diff-col__text {
  color: #344054;
  line-height: 1.6;
  white-space: pre-wrap;
  margin: 0;
}
.diff-item__evidence {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

/* Attachment list */
.attachment-name {
  font-weight: 600;
  color: #101828;
}
.attachment-meta {
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.meta-sep {
  color: #d0d5dd;
}

/* Edit row */
.edit-row {
  display: grid;
  grid-template-columns: 0.8fr 1.2fr 1.2fr 1fr 0.6fr auto;
  gap: 6px;
  margin-bottom: 8px;
  align-items: center;
}

/* History */
.history-item {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.history-item__title {
  font-weight: 600;
  color: #101828;
}
.history-item__meta {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 2px;
}

/* Evidence tag (clickable) */
.evidence-tag {
  cursor: pointer;
}

/* Shared */
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.muted {
  color: #98a2b3;
  font-size: 12px;
}
</style>
