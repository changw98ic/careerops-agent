<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>简历版本</h1>
        <p>每个版本内容寻址、不可变。仅「解析成功且已确认」的版本可用于投递包。</p>
      </div>
      <a-tag color="blue">{{ resumes.length }} 个版本</a-tag>
    </div>

    <a-alert
      v-if="unavailable"
      type="warning"
      show-icon
      message="简历服务暂不可用"
      description="后端仓储未就绪（503）。请稍后重试。"
      closable
      @close="unavailable = false"
    />
    <a-alert v-if="error" type="error" show-icon :message="error" closable @close="error = ''" />
    <a-alert
      v-if="uploadError"
      type="error"
      show-icon
      :message="uploadError"
      closable
      @close="uploadError = ''"
    />

    <!-- Upload card -->
    <a-card class="detail-card" title="注册新版本">
      <a-upload
        :file-list="fileList"
        :before-upload="beforeUpload"
        :max-count="1"
        :accept="ACCEPT_EXT"
        @remove="onRemove"
      >
        <a-button :disabled="!!pendingFile">
          <UploadOutlined /> 选择简历文件
        </a-button>
      </a-upload>
      <div class="upload-meta muted">
        支持 {{ ACCEPT_EXT }}；大小上限 {{ MAX_MB }} MB。PDF 可注册但当前仅文本类可解析。
      </div>

      <a-space v-if="pendingFile" style="margin-top: 12px" wrap>
        <a-input
          v-model:value="targetType"
          placeholder="target_type (如 general / backend)"
          style="width: 220px"
        />
        <a-input
          v-model:value="sourceReference"
          placeholder="来源备注 (可选)"
          style="width: 260px"
        />
        <a-button type="primary" :loading="uploading" :disabled="!pendingFile" @click="upload">
          上传并解析
        </a-button>
        <a-button @click="cancelPending" :disabled="uploading">取消</a-button>
      </a-space>
    </a-card>

    <!-- Versions table -->
    <a-card class="table-card" title="版本列表" style="margin-top: 16px">
      <div class="toolbar">
        <span class="muted">仅展示该候选人的简历版本</span>
        <a-button :loading="loading" @click="loadResumes">
          <ReloadOutlined /> 刷新
        </a-button>
      </div>

      <a-spin :spinning="loading && resumes.length === 0">
        <a-table
          v-if="resumes.length > 0 || !loading"
          :columns="columns"
          :data-source="resumes"
          :pagination="false"
          row-key="id"
          :scroll="{ x: 980 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'version'">
              <strong>v{{ record.version_number }}</strong>
              <div class="muted mono">{{ shortHash(record.content_hash) }}</div>
            </template>
            <template v-else-if="column.key === 'target'">
              {{ record.target_type || 'general' }}
            </template>
            <template v-else-if="column.key === 'parse'">
              <a-tag :color="parseColor(record.parse_status)">{{ parseLabel(record.parse_status) }}</a-tag>
              <div v-if="record.parse_status === 'failed'" class="parse-error">
                解析失败：{{ record.source_reference || '原因未记录' }}
              </div>
            </template>
            <template v-else-if="column.key === 'confirm'">
              <a-tag :color="confirmColor(record.confirmation_status)">
                {{ confirmLabel(record.confirmation_status) }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'eligible'">
              <a-tag v-if="isEligible(record)" color="green">可用于投递包</a-tag>
              <span v-else class="muted">不可用</span>
            </template>
            <template v-else-if="column.key === 'created'">
              {{ formatDate(record.created_at) }}
            </template>
            <template v-else-if="column.key === 'action'">
              <a-space direction="vertical" :size="0">
                <a-button
                  v-if="record.parse_status === 'parsed' && record.confirmation_status !== 'confirmed'"
                  type="link"
                  size="small"
                  :loading="confirmingId === record.id"
                  @click="confirm(record.id)"
                >
                  确认内容
                </a-button>
                <router-link v-if="record.parse_status === 'parsed'" :to="`/evidence?resume=${record.id}`">
                  <a-button type="link" size="small">查看证据</a-button>
                </router-link>
                <span v-if="record.confirmation_status === 'confirmed'" class="muted">已确认</span>
                <span v-if="record.parse_status === 'failed'" class="muted">解析失败</span>
              </a-space>
            </template>
          </template>
          <template #emptyText>
            <a-empty description="尚无简历版本，上传第一份简历即可" />
          </template>
        </a-table>
      </a-spin>
    </a-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ReloadOutlined, UploadOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { api, formatApiError, parseApiError } from '../api/client.js'

// Mirrors backend constants (resume_service.SUPPORTED_RESUME_MEDIA_TYPES).
const ACCEPT_TYPES = ['text/plain', 'text/markdown', 'application/pdf']
const ACCEPT_EXT = '.txt,.md,.pdf'
const EXT_TO_TYPE = {
  txt: 'text/plain',
  md: 'text/markdown',
  markdown: 'text/markdown',
  pdf: 'application/pdf',
}
// Mirrors config.storage_max_object_bytes default (10 MiB).
const MAX_BYTES = 10 * 1024 * 1024
const MAX_MB = Math.round(MAX_BYTES / (1024 * 1024))

const resumes = ref([])
const loading = ref(false)
const uploading = ref(false)
const confirmingId = ref('')
const error = ref('')
const uploadError = ref('')
const unavailable = ref(false)

const pendingFile = ref(null)
const fileList = ref([])
const targetType = ref('general')
const sourceReference = ref('')

const columns = [
  { title: '版本', key: 'version', dataIndex: 'version_number', width: 160 },
  { title: '类型', key: 'target', dataIndex: 'target_type', width: 120 },
  { title: '解析', key: 'parse', dataIndex: 'parse_status', width: 200 },
  { title: '确认', key: 'confirm', dataIndex: 'confirmation_status', width: 120 },
  { title: '投递资格', key: 'eligible', width: 140 },
  { title: '创建时间', key: 'created', dataIndex: 'created_at', width: 140 },
  { title: '操作', key: 'action', width: 140 },
]

function inferType(file) {
  const declared = (file.type || '').toLowerCase()
  if (ACCEPT_TYPES.includes(declared)) return declared
  const ext = (file.name.split('.').pop() || '').toLowerCase()
  return EXT_TO_TYPE[ext] || declared
}

function beforeUpload(file) {
  uploadError.value = ''
  const mediaType = inferType(file)
  if (!ACCEPT_TYPES.includes(mediaType)) {
    uploadError.value = `不支持的类型「${mediaType || file.type || '未知'}」，仅接受 ${ACCEPT_EXT}。`
    return false
  }
  if (file.size > MAX_BYTES) {
    uploadError.value = `文件过大（${formatBytes(file.size)}），上限 ${MAX_MB} MB。`
    return false
  }
  pendingFile.value = file
  fileList.value = [file]
  // Returning false prevents a-upload auto-upload.
  return false
}

function onRemove() {
  cancelPending()
  return true
}

function cancelPending() {
  pendingFile.value = null
  fileList.value = []
}

async function upload() {
  if (!pendingFile.value) return
  uploading.value = true
  uploadError.value = ''
  try {
    const result = await api.registerResume({
      file: pendingFile.value,
      target_type: targetType.value || 'general',
      source_reference: sourceReference.value || '',
    })
    if (result.deduplicated) {
      message.info('内容已存在：返回已有版本（未重复存储 blob）')
    } else if (result.parse_status === 'failed') {
      message.warning('已注册，但解析失败 — 该版本不可用于投递包')
    } else {
      message.success(`已注册 v${result.resume.version_number}，提取 ${result.extracted_evidence_count} 条待确认证据`)
    }
    cancelPending()
    targetType.value = 'general'
    sourceReference.value = ''
    await loadResumes()
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else if (info.status === 409 || info.code === 'INVALID_STATE') {
      uploadError.value = formatApiError(err, '上传被拒绝（类型或大小校验未通过）。')
    } else {
      uploadError.value = formatApiError(err, '上传失败，请稍后重试。')
    }
  } finally {
    uploading.value = false
  }
}

async function confirm(id) {
  confirmingId.value = id
  error.value = ''
  try {
    await api.confirmResume(id)
    message.success('已确认该简历内容，现可用于投递包')
    await loadResumes()
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else if (info.status === 409 || info.code === 'INVALID_STATE') {
      error.value = formatApiError(err, '该简历当前无法确认（解析未成功）。')
    } else {
      error.value = formatApiError(err, '确认失败。')
    }
  } finally {
    confirmingId.value = ''
  }
}

async function loadResumes() {
  loading.value = true
  error.value = ''
  unavailable.value = false
  try {
    const data = await api.listResumes({ limit: 50 })
    resumes.value = data.items || []
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else {
      error.value = formatApiError(err, '加载简历列表失败。')
    }
  } finally {
    loading.value = false
  }
}

function isEligible(r) {
  return r.parse_status === 'parsed' && r.confirmation_status === 'confirmed'
}

function parseColor(s) {
  return { parsed: 'green', failed: 'red', pending: 'gold' }[s] || 'default'
}
function parseLabel(s) {
  return { parsed: '解析成功', failed: '解析失败', pending: '解析中' }[s] || s || '未知'
}
function confirmColor(s) {
  return { confirmed: 'green', unconfirmed: 'gold', rejected: 'red' }[s] || 'default'
}
function confirmLabel(s) {
  return { confirmed: '已确认', unconfirmed: '未确认', rejected: '已拒绝' }[s] || s || '未知'
}

function shortHash(h) {
  if (!h) return ''
  return h.slice(0, 12) + '…'
}
function formatDate(d) {
  if (!d) return '-'
  return new Date(d).toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' })
}
function formatBytes(n) {
  if (n == null) return ''
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / (1024 * 1024)).toFixed(1) + ' MB'
}

onMounted(loadResumes)
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.upload-meta {
  margin-top: 8px;
  font-size: 12px;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.parse-error {
  color: #d4380d;
  font-size: 12px;
  margin-top: 2px;
}
</style>
