<template>
  <a-card title="邮件投递预览与确认" class="submission-preview detail-card">
    <!-- Unavailable (503) -->
    <a-alert
      v-if="unavailable"
      type="warning"
      show-icon
      message="预览服务暂不可用"
      description="后端依赖未就绪（503）。请稍后重试。"
      style="margin-bottom: 12px"
    />

    <!-- Non-fatal error -->
    <a-alert
      v-if="error"
      type="error"
      show-icon
      :message="error"
      closable
      @close="error = ''"
      style="margin-bottom: 12px"
    />

    <!-- No approved package: cannot preview an email submission -->
    <a-alert
      v-if="!unavailable && !hasApprovedPackage"
      type="info"
      show-icon
      message="尚无已批准的投递包"
      description="邮件投递需要一个已批准的投递包（Section 8）。请先创建并批准投递包。"
      style="margin-bottom: 12px"
    />

    <!-- Form: pick account + recipient + subject + body -->
    <a-form layout="vertical" class="preview-form">
      <a-row :gutter="16">
        <a-col :xs="24" :md="12">
          <a-form-item label="发送账户 (account_id) *" required>
            <a-input
              v-model:value="form.account_id"
              placeholder="专用招聘邮箱账户 UUID"
              allow-clear
            />
            <div class="form-hint muted">
              系统将通过此账户经后端受控链路发送；用户无需手动打开邮箱。
            </div>
          </a-form-item>
        </a-col>
        <a-col :xs="24" :md="12">
          <a-form-item label="收件人 (recipient) *" required>
            <a-select
              v-if="contacts.length > 0"
              v-model:value="form.recipient_email"
              placeholder="选择可信招聘联系人"
              allow-clear
            >
              <a-select-option
                v-for="c in contacts"
                :key="c.email"
                :value="c.email"
              >
                {{ c.email }} · {{ c.contact_type }} · {{ c.confidence }}
              </a-select-option>
            </a-select>
            <a-input
              v-else
              v-model:value="form.recipient_email"
              placeholder="收件人邮箱（须匹配可信公司域名）"
            />
            <div v-if="contacts.length === 0" class="form-hint form-hint--warn">
              未找到可信招聘联系人证据；任何收件人都将被拒绝（域名/来源校验）。
            </div>
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="主题 (subject) *" required>
        <a-input v-model:value="form.subject" placeholder="邮件主题" allow-clear />
      </a-form-item>

      <a-form-item label="正文 (body)">
        <a-textarea
          v-model:value="form.body"
          :rows="5"
          placeholder="邮件正文（建议来源于已批准投递包的求职信）"
        />
      </a-form-item>

      <a-form-item label="附件哈希 (attachment_hashes)">
        <a-select
          v-model:value="form.attachment_hashes"
          mode="tags"
          placeholder="输入简历/附件 sha256（来源于已批准投递包）"
          :token-separators="[',', ' ']"
        />
        <div class="form-hint muted">仅允许 PDF / 文本 / 日历；其他类型将被拒绝。</div>
      </a-form-item>

      <div class="preview-actions">
        <a-button type="primary" :loading="loading" @click="onPreview">
          生成预览（不发送）
        </a-button>
        <span class="muted preview-hint">
          预览仅计算可发送表示，不产生任何外部写入。
        </span>
      </div>
    </a-form>

    <!-- Preview result -->
    <div v-if="preview" class="preview-result">
      <a-divider />

      <!-- Validation errors -->
      <a-alert
        v-if="preview.errors.length > 0"
        type="error"
        show-icon
        message="预览校验未通过 — 不会发送"
      >
        <template #description>
          <ul class="error-list">
            <li v-for="(e, i) in preview.errors" :key="i">{{ e }}</li>
          </ul>
        </template>
      </a-alert>

      <!-- Recipient verdict -->
      <a-descriptions
        v-if="preview.recipient_verdict"
        title="收件人可信裁决"
        :column="1"
        size="small"
        :label-style="{ color: '#667085', width: '120px' }"
        bordered
        style="margin-top: 12px"
      >
        <a-descriptions-item label="收件人">
          {{ preview.recipient_verdict.email }}
        </a-descriptions-item>
        <a-descriptions-item label="公司域名">
          {{ preview.recipient_verdict.company_domain || '（未验证）' }}
        </a-descriptions-item>
        <a-descriptions-item label="来源证据">
          <span v-if="preview.recipient_verdict.source_evidence_url" class="table-link">
            {{ truncateUrl(preview.recipient_verdict.source_evidence_url) }}
          </span>
          <span v-else class="muted">无（仅有模型建议时将被拒绝）</span>
        </a-descriptions-item>
        <a-descriptions-item label="可信">
          <a-tag :color="preview.recipient_verdict.eligible ? 'green' : 'red'">
            {{ preview.recipient_verdict.eligible ? '可信' : '不可信' }}
          </a-tag>
          <a-tag
            v-for="r in preview.recipient_verdict.denial_reasons"
            :key="r"
            color="default"
            size="small"
          >
            {{ r }}
          </a-tag>
        </a-descriptions-item>
      </a-descriptions>

      <!-- Exact payload -->
      <a-descriptions
        v-if="preview.payload"
        title="精确可发送载荷（与发送字节一致）"
        :column="1"
        size="small"
        :label-style="{ color: '#667085', width: '120px' }"
        bordered
        style="margin-top: 16px"
      >
        <a-descriptions-item label="收件人">
          {{ preview.payload.recipient_email }}
        </a-descriptions-item>
        <a-descriptions-item label="主题">
          {{ preview.payload.subject }}
        </a-descriptions-item>
        <a-descriptions-item label="正文">
          <pre class="payload-body">{{ preview.payload.body }}</pre>
        </a-descriptions-item>
        <a-descriptions-item label="附件">
          <a-tag v-for="a in preview.payload.attachments" :key="a.content_hash" color="blue">
            {{ a.name }} · {{ shortHash(a.content_hash) }} · {{ a.media_type }}
          </a-tag>
          <span v-if="preview.payload.attachments.length === 0" class="muted">无</span>
        </a-descriptions-item>
        <a-descriptions-item label="来源证据引用">
          <a-tag v-for="(e, i) in preview.payload.evidence_refs" :key="i" size="small">
            {{ shortHash(String(e)) }}
          </a-tag>
          <span v-if="preview.payload.evidence_refs.length === 0" class="muted">无</span>
        </a-descriptions-item>
        <a-descriptions-item label="payload_hash">
          <span class="mono">{{ shortHash(preview.payload.payload_hash) }}</span>
        </a-descriptions-item>
        <a-descriptions-item label="idempotency_key">
          <span class="mono">{{ shortHash(preview.payload.idempotency_key) }}</span>
        </a-descriptions-item>
      </a-descriptions>

      <!-- Confirmation notice: system will send after confirmation -->
      <a-alert
        v-if="preview.sendable"
        type="success"
        show-icon
        banner
        style="margin-top: 16px"
      >
        <template #message>
          <strong>确认后系统将通过受控后端链路发送此邮件。</strong>
          你无需手动打开邮箱。当前仅为预览，尚未产生任何外部写入。
        </template>
      </a-alert>
    </div>
  </a-card>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { api, parseApiError } from '../api/client.js'

const props = defineProps({
  applicationId: { type: String, required: true },
})

const loading = ref(false)
const unavailable = ref(false)
const error = ref('')
const contacts = ref([])
const preview = ref(null)
const hasApprovedPackage = ref(true)

const form = ref({
  account_id: '',
  recipient_email: '',
  subject: '',
  body: '',
  attachment_hashes: [],
})

function shortHash(h) {
  if (!h) return ''
  const s = String(h)
  return s.length > 12 ? s.slice(0, 8) + '…' + s.slice(-4) : s
}

function truncateUrl(u) {
  if (!u) return ''
  return u.length > 60 ? u.slice(0, 57) + '…' : u
}

async function loadContacts() {
  try {
    const res = await api.listRecruitingContacts(props.applicationId)
    contacts.value = res.contacts || []
  } catch (err) {
    // 503 → unavailable; otherwise a soft warning (contacts are best-effort).
    if (err.status === 503) {
      unavailable.value = true
    } else {
      error.value = parseApiError(err)
    }
  }
}

async function onPreview() {
  error.value = ''
  if (!form.value.account_id || !form.value.recipient_email || !form.value.subject) {
    message.warning('请填写发送账户、收件人和主题')
    return
  }
  loading.value = true
  try {
    preview.value = await api.previewSubmission(props.applicationId, {
      account_id: form.value.account_id,
      recipient_email: form.value.recipient_email,
      subject: form.value.subject,
      body: form.value.body,
      attachment_hashes: form.value.attachment_hashes,
      evidence_ids: [],
    })
  } catch (err) {
    if (err.status === 503) {
      unavailable.value = true
    } else {
      error.value = parseApiError(err)
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadContacts()
})

defineExpose({ loadContacts })
</script>

<style scoped>
.submission-preview {
  margin-top: 16px;
}
.preview-form {
  margin-top: 8px;
}
.form-hint {
  font-size: 12px;
  margin-top: 4px;
}
.form-hint--warn {
  color: #d46b08;
}
.form-hint--error {
  color: #cf1322;
}
.preview-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 8px;
}
.preview-hint {
  font-size: 12px;
}
.preview-result {
  margin-top: 8px;
}
.error-list {
  margin: 0;
  padding-left: 18px;
}
.payload-body {
  margin: 0;
  white-space: pre-wrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #344054;
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
</style>
