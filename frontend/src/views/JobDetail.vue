<template>
  <div v-if="job" class="page-shell">
    <div class="detail-heading">
      <div class="detail-title">
        <a-button type="text" @click="router.back()">
          <template #icon><ArrowLeftOutlined /></template>
          返回
        </a-button>
        <h1>{{ job.canonical_title }}</h1>
        <a-tag :color="stateColor(job.aggregate_state)">{{ stateLabel(job.aggregate_state) }}</a-tag>
      </div>
      <div class="detail-actions">
        <a-button
          v-if="!applied"
          type="primary"
          size="large"
          :loading="draftLoading"
          @click="onApplyClick"
        >
          <template #icon><SendOutlined /></template>
          投递申请
        </a-button>
        <a-space v-else wrap>
          <a-tag color="green" style="font-size: 14px; padding: 6px 16px">
            <template #icon><CheckCircleOutlined /></template>
            已投递
          </a-tag>
          <!-- Action path (task 14.3): once an application record exists,
               continue the path (prepare -> package -> channel -> timeline)
               inside the application workspace. -->
          <a-button v-if="applicationId" type="primary" @click="goToWorkspace">
            进入申请工作区
            <template #icon><ArrowRightOutlined /></template>
          </a-button>
        </a-space>
      </div>
    </div>

    <a-alert v-if="error" type="error" show-icon :message="error" closable @close="error = ''" />

    <a-row :gutter="[24, 24]">
      <a-col :xs="24" :lg="7">
        <a-card title="职位信息" class="detail-card">
          <a-descriptions :column="1" size="small" :label-style="{ color: '#667085', width: '72px' }">
            <a-descriptions-item label="公司">
              <span style="font-weight: 600">{{ companyName }}</span>
            </a-descriptions-item>
            <a-descriptions-item label="地点">
              {{ location || '未标注' }}
            </a-descriptions-item>
            <a-descriptions-item label="状态">
              <a-tag :color="stateColor(job.aggregate_state)">{{ stateLabel(job.aggregate_state) }}</a-tag>
            </a-descriptions-item>
            <a-descriptions-item v-if="job.current_apply_url" label="申请链接">
              <a :href="job.current_apply_url" target="_blank" rel="noreferrer" class="table-link">
                打开链接 ↗
              </a>
            </a-descriptions-item>
          </a-descriptions>
        </a-card>
      </a-col>

      <a-col :xs="24" :lg="17">
        <a-card title="职位描述" class="detail-card">
          <div v-if="description" class="description-body" v-html="description"></div>
          <a-empty v-else description="暂无职位描述" />
        </a-card>
      </a-col>
    </a-row>

    <!-- 邮件草稿预览弹窗 -->
    <a-modal
      v-model:open="draftModalVisible"
      title="确认投递邮件"
      :width="640"
      :confirm-loading="sending"
      ok-text="确认发送"
      cancel-text="取消"
      @ok="confirmSend"
      @cancel="draftModalVisible = false"
    >
      <a-form layout="vertical">
        <a-form-item label="收件人">
          <a-input v-model:value="draft.to" placeholder="recruiting@company.com" />
        </a-form-item>
        <a-form-item label="邮件主题">
          <a-input v-model:value="draft.subject" />
        </a-form-item>
        <a-form-item label="邮件正文">
          <a-textarea v-model:value="draft.body" :rows="12" />
        </a-form-item>
      </a-form>
      <a-alert
        v-if="!draft.to"
        type="warning"
        show-icon
        message="请填写收件人邮箱"
        style="margin-top: 8px"
      />
    </a-modal>
  </div>

  <div v-else-if="loading" class="page-loading">
    <a-spin size="large" />
    <span>加载中...</span>
  </div>

  <a-result v-else-if="error" status="error" title="加载失败" :sub-title="error">
    <template #extra>
      <a-button type="primary" @click="fetchJob">重新加载</a-button>
    </template>
  </a-result>

  <a-result v-else status="404" title="职位未找到" sub-title="该职位可能已被移除或不存在。" />
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  CheckCircleOutlined,
  SendOutlined,
} from '@ant-design/icons-vue'
import message from 'ant-design-vue/es/message'
import { api, parseApiError } from '../api/client.js'

const route = useRoute()
const router = useRouter()
const job = ref(null)
const versions = ref([])
const loading = ref(true)
const applied = ref(false)
const error = ref('')

// 邮件草稿相关
const draftLoading = ref(false)
const draftModalVisible = ref(false)
const sending = ref(false)
const applicationId = ref(null)
const draft = reactive({
  to: '',
  subject: '',
  body: '',
  company: '',
  title: '',
})

const latestVersion = computed(() => versions.value[0] || null)

const companyName = computed(() => {
  const postings = job.value?.postings || []
  if (postings.length && postings[0].source?.identifier) {
    return postings[0].source.identifier
  }
  return job.value?.company_id?.slice(0, 8) || '未知'
})

const location = computed(() => {
  return latestVersion.value?.structured_data?.location || ''
})

const description = computed(() => {
  for (const version of versions.value) {
    const value = version.structured_data?.description
    if (value && value.length > 10) return value
  }
  return ''
})

async function fetchJob() {
  loading.value = true
  error.value = ''
  try {
    const detail = await api.getJob(route.params.id)
    job.value = detail.canonical_job || detail
    versions.value = detail.versions || []
  } catch (err) {
    // Dependency-not-ready (task 14.2/14.6): surface a 503 from a missing
    // repository/capability as a dedicated, actionable message instead of a
    // generic server error. Other errors keep the existing message mapping
    // so the job-level apply flow and its tests stay unchanged.
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      error.value = '职位服务依赖未就绪（503），请稍后重试。'
    } else {
      error.value = parseErrorMessage(err)
    }
    job.value = null
  } finally {
    loading.value = false
  }
}

// Action path (task 14.3): continue into the application workspace once an
// application record has been created for this job.
function goToWorkspace() {
  if (!applicationId.value) return
  router.push(`/applications/${applicationId.value}`)
}

async function onApplyClick() {
  if (draftLoading.value) return
  draftLoading.value = true
  error.value = ''

  try {
    // 1. 创建申请记录
    const result = await api.createApplication({
      canonical_job_id: route.params.id,
      apply_url: job.value?.current_apply_url || '',
    })
    applicationId.value = result.id
    applied.value = true

    // 2. 获取邮件草稿
    try {
      const draftData = await api.getEmailDraft(result.id)
      draft.to = draftData.to || ''
      draft.subject = draftData.subject || ''
      draft.body = draftData.body || ''
      draft.company = draftData.company || ''
      draft.title = draftData.title || ''
      draftModalVisible.value = true
    } catch (draftErr) {
      message.warning('申请已记录，但邮件草稿生成失败: ' + parseErrorMessage(draftErr))
    }
  } catch (err) {
    const msg = parseErrorMessage(err)
    if (msg.includes('409') || msg.includes('CONFLICT')) {
      applied.value = true
      message.info('该职位已有申请记录')
      // 尝试获取已有申请的草稿
      // TODO: need application id from existing record
    } else {
      error.value = msg
      message.error(msg)
    }
  } finally {
    draftLoading.value = false
  }
}

async function confirmSend() {
  if (!draft.to || sending.value) return
  sending.value = true

  try {
    await api.sendApplicationEmail(applicationId.value, {
      to: draft.to,
      subject: draft.subject,
      body: draft.body,
    })
    draftModalVisible.value = false
    message.success('投递邮件已发送')
  } catch (err) {
    message.error('发送失败: ' + parseErrorMessage(err))
  } finally {
    sending.value = false
  }
}

function parseErrorMessage(err) {
  if (!err?.message) return '操作失败，请稍后重试。'
  const msg = err.message
  if (msg.includes('409')) return '该职位已投递。'
  if (msg.includes('401') || msg.includes('unauthorized')) return '登录已过期，请重新登录。'
  if (msg.includes('404')) return '职位不存在或已被移除。'
  if (msg.includes('429')) return '操作过于频繁，请稍后再试。'
  if (msg.includes('500')) return '服务器错误，请稍后重试。'
  if (msg.includes('503')) return '邮件服务未配置。'
  if (msg.includes('502')) return '邮件发送失败，请检查收件人地址。'
  return msg
}

function stateColor(state) {
  return { active: 'green', closed: 'default', expired: 'red' }[state] || 'gold'
}

function stateLabel(state) {
  return { active: '进行中', closed: '已关闭', expired: '已过期' }[state] || state || '未知'
}

onMounted(fetchJob)
</script>

<style scoped>
.detail-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 20px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}
.detail-title {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.detail-title h1 {
  margin: 0;
  font-size: 24px;
  color: #101828;
}
.detail-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}
.detail-card {
  border: 1px solid #eaecf0;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
}
.description-body {
  color: #344054;
  line-height: 1.8;
  font-size: 14px;
}
.description-body :deep(h2) {
  font-size: 18px;
  font-weight: 700;
  color: #101828;
  margin: 28px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid #eaecf0;
}
.description-body :deep(h2:first-child) {
  margin-top: 0;
}
.description-body :deep(h3) {
  font-size: 16px;
  font-weight: 600;
  color: #101828;
  margin: 20px 0 8px;
}
.description-body :deep(p) {
  margin: 8px 0;
}
.description-body :deep(ul),
.description-body :deep(ol) {
  padding-left: 24px;
  margin: 8px 0;
}
.description-body :deep(li) {
  margin: 4px 0;
}
.description-body :deep(a) {
  color: #1677ff;
  text-decoration: none;
}
.description-body :deep(strong) {
  font-weight: 600;
  color: #101828;
}
.page-loading {
  display: grid;
  place-items: center;
  gap: 12px;
  min-height: 240px;
  color: #667085;
}
.table-link {
  color: #1677ff;
  font-weight: 500;
}
</style>
