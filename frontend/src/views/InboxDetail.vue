<template>
  <div v-if="job" class="page-shell">
    <!-- Header -->
    <div class="detail-heading">
      <div class="detail-title">
        <a-button type="text" @click="router.push('/inbox')">
          <template #icon><ArrowLeftOutlined /></template>
          返回收件箱
        </a-button>
        <h1>{{ job.title || 'Untitled' }}</h1>
        <a-tag :color="verdictColor(job.verdict)">{{ verdictLabel(job.verdict) }}</a-tag>
        <a-tag v-if="job.application_state && job.application_state !== 'none'" :color="appStateColor(job.application_state)">
          {{ appStateLabel(job.application_state) }}
        </a-tag>
      </div>
      <div class="detail-actions">
        <a-button
          v-if="job.application_state !== 'favorited'"
          type="primary"
          :loading="actionLoading === 'fav'"
          @click="onFavorite"
        >
          <template #icon><StarOutlined /></template>
          收藏
        </a-button>
        <a-button
          v-else
          type="primary"
          ghost
          disabled
        >
          <template #icon><StarFilled /></template>
          已收藏
        </a-button>

        <a-button
          v-if="job.application_state !== 'ignored'"
          danger
          :loading="actionLoading === 'ign'"
          @click="onIgnore"
        >
          <template #icon><CloseCircleOutlined /></template>
          忽略
        </a-button>
        <a-button
          v-else
          disabled
        >
          <template #icon><CloseCircleOutlined /></template>
          已忽略
        </a-button>

        <a-button :loading="actionLoading === 'snz'" @click="onSnooze">
          <template #icon><ClockCircleOutlined /></template>
          稍后处理
        </a-button>
      </div>
    </div>

    <a-alert v-if="error" type="error" show-icon :message="error" closable @close="error = ''" />

    <a-row :gutter="[24, 24]">
      <!-- Left column: job info + provenance -->
      <a-col :xs="24" :lg="7">
        <!-- Job info card -->
        <a-card title="职位信息" class="detail-card">
          <a-descriptions :column="1" size="small" :label-style="{ color: '#667085', width: '80px' }">
            <a-descriptions-item label="公司">
              <span style="font-weight: 600">{{ job.company || '未知' }}</span>
            </a-descriptions-item>
            <a-descriptions-item label="地点">
              {{ job.location || '未标注' }}
            </a-descriptions-item>
            <a-descriptions-item v-if="job.remote_type" label="远程类型">
              {{ remoteLabel(job.remote_type) }}
            </a-descriptions-item>
            <a-descriptions-item v-if="job.seniority" label="职级">
              {{ job.seniority }}
            </a-descriptions-item>
            <a-descriptions-item v-if="job.compensation" label="薪资">
              {{ job.compensation }}
            </a-descriptions-item>
            <a-descriptions-item v-if="job.apply_url" label="申请链接">
              <a :href="job.apply_url" target="_blank" rel="noreferrer" class="table-link">
                打开链接 ↗
              </a>
            </a-descriptions-item>
          </a-descriptions>
        </a-card>

        <!-- Provenance card -->
        <a-card title="来源信息" class="detail-card" style="margin-top: 16px">
          <a-descriptions :column="1" size="small" :label-style="{ color: '#667085', width: '80px' }">
            <a-descriptions-item v-if="job.source_name" label="来源">
              {{ job.source_name }}
            </a-descriptions-item>
            <a-descriptions-item v-if="job.source_type" label="来源类型">
              {{ job.source_type }}
            </a-descriptions-item>
            <a-descriptions-item v-if="job.crawl_run_id" label="采集运行">
              <router-link
                v-if="job.crawl_run_id"
                :to="`/crawl-runs/${job.crawl_run_id}`"
                class="table-link"
              >
                {{ job.crawl_run_id.slice(0, 8) }}...
              </router-link>
            </a-descriptions-item>
            <a-descriptions-item v-if="job.plan_version_id" label="计划版本">
              {{ job.plan_version_id.slice(0, 8) }}...
            </a-descriptions-item>
            <a-descriptions-item v-if="job.captured_at" label="采集时间">
              {{ formatDateTime(job.captured_at) }}
            </a-descriptions-item>
          </a-descriptions>
        </a-card>

        <!-- Excluded reasons card (only for excluded jobs) -->
        <a-card
          v-if="job.verdict === 'excluded' && excludedReasons.length > 0"
          title="排除原因"
          class="detail-card excluded-card"
          style="margin-top: 16px"
        >
          <div v-for="(reason, idx) in excludedReasons" :key="idx" class="excluded-reason">
            <a-tag color="red" size="small">{{ reason.rule || reason.category || '未知规则' }}</a-tag>
            <p class="excluded-reason__text">{{ reason.message || reason.reason || '' }}</p>
            <div v-if="reason.evidence_refs?.length" class="excluded-reason__evidence">
              <span class="muted">证据: </span>
              <a-tag v-for="(ref, ri) in reason.evidence_refs" :key="ri" size="small" color="default">
                {{ ref }}
              </a-tag>
            </div>
          </div>
        </a-card>
      </a-col>

      <!-- Right column: description + match results -->
      <a-col :xs="24" :lg="17">
        <!-- Description card -->
        <a-card title="职位描述" class="detail-card">
          <div v-if="job.description" class="description-body" v-html="job.description"></div>
          <a-empty v-else description="暂无职位描述" />
        </a-card>

        <!-- Requirement match results -->
        <a-card title="要求匹配" class="detail-card" style="margin-top: 16px">
          <template v-if="matchResults.length > 0">
            <div class="match-notice">
              <a-alert
                type="info"
                show-icon
                message="以下匹配结果仅供参考，不构成自动申请依据。"
                banner
              />
            </div>
            <a-table
              :columns="matchColumns"
              :data-source="matchResults"
              :pagination="false"
              row-key="requirement"
              size="small"
              :scroll="{ x: 600 }"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'level'">
                  <a-tag :color="matchLevelColor(record.level)">
                    {{ matchLevelLabel(record.level) }}
                  </a-tag>
                </template>
                <template v-else-if="column.key === 'evidence'">
                  <a-space v-if="record.evidence_refs?.length" wrap>
                    <a-tag
                      v-for="(ref, ri) in record.evidence_refs"
                      :key="ri"
                      size="small"
                      color="blue"
                      class="evidence-tag"
                      @click="goToEvidence(ref)"
                    >
                      {{ ref.slice(0, 8) }}...
                    </a-tag>
                  </a-space>
                  <span v-else class="muted">-</span>
                </template>
              </template>
            </a-table>
          </template>
          <a-empty v-else description="暂无匹配结果（确定性筛选未生成或模型不可用）" />
        </a-card>

        <!-- Action path (task 14.3): recommendation -> favorite -> prepare ->
             package -> channel -> application timeline. The whole downstream
             path lives in the application workspace; this CTA is the bridge
             from the inbox recommendation into that workspace. It is enabled
             as soon as favoriting has produced/reused an application record
             (the favorite response and the inbox detail both surface
             application_id via server-side ownership). -->
        <a-card
          v-if="hasApplication"
          class="detail-card cta-card"
          style="margin-top: 16px"
        >
          <div class="cta-content">
            <div>
              <h3 style="margin: 0 0 4px">{{ ctaTitle }}</h3>
              <p class="muted" style="margin: 0">{{ ctaSubtitle }}</p>
            </div>
            <a-button
              v-if="job.application_id"
              type="primary"
              size="large"
              :loading="navigating"
              @click="goToWorkspace"
            >
              {{ ctaButtonLabel }}
              <template #icon><ArrowRightOutlined /></template>
            </a-button>
            <a-button v-else type="primary" size="large" disabled>
              准备申请
              <template #icon><ArrowRightOutlined /></template>
            </a-button>
          </div>
          <a-alert
            v-if="!job.application_id"
            type="info"
            show-icon
            message="申请记录尚未就绪，请稍后刷新或在投递列表中查看。"
            style="margin-top: 12px"
          />
        </a-card>
      </a-col>
    </a-row>

    <!-- Snooze modal -->
    <a-modal
      v-model:open="snoozeModalVisible"
      title="稍后处理"
      :confirm-loading="snoozing"
      ok-text="确认"
      cancel-text="取消"
      @ok="confirmSnooze"
    >
      <a-form layout="vertical">
        <a-form-item label="隐藏至">
          <a-radio-group v-model:value="snoozePreset" @change="onSnoozePresetChange">
            <a-radio-button value="1h">1 小时</a-radio-button>
            <a-radio-button value="4h">4 小时</a-radio-button>
            <a-radio-button value="1d">明天</a-radio-button>
            <a-radio-button value="7d">下周</a-radio-button>
            <a-radio-button value="custom">自定义</a-radio-button>
          </a-radio-group>
        </a-form-item>
        <a-form-item v-if="snoozePreset === 'custom'" label="自定义时间">
          <a-date-picker
            v-model:value="snoozeCustomDate"
            show-time
            format="YYYY-MM-DD HH:mm"
            style="width: 100%"
            :disabled-date="disabledSnoozeDate"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>

  <!-- Loading state -->
  <div v-else-if="loading" class="page-loading">
    <a-spin size="large" />
    <span>加载中...</span>
  </div>

  <!-- Error state -->
  <a-result v-else-if="error" status="error" title="加载失败" :sub-title="error">
    <template #extra>
      <a-space>
        <a-button type="primary" @click="fetchDetail">重新加载</a-button>
        <a-button @click="router.push('/inbox')">返回收件箱</a-button>
      </a-space>
    </template>
  </a-result>

  <!-- 404 state -->
  <a-result v-else status="404" title="职位未找到" sub-title="该职位可能已被移除或不存在。">
    <template #extra>
      <a-button type="primary" @click="router.push('/inbox')">返回收件箱</a-button>
    </template>
  </a-result>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  StarFilled,
  StarOutlined,
} from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { api, formatApiError, parseApiError } from '../api/client.js'

const route = useRoute()
const router = useRouter()

// --- state ---
const job = ref(null)
const excludedReasons = ref([])
const matchResults = ref([])
const loading = ref(true)
const error = ref('')
const actionLoading = ref('')

// Snooze
const snoozeModalVisible = ref(false)
const snoozing = ref(false)
const snoozePreset = ref('1d')
const snoozeCustomDate = ref(null)

// --- computed ---
const jobId = computed(() => route.params.id)

const matchColumns = [
  { title: '要求', key: 'requirement', dataIndex: 'requirement', ellipsis: true },
  { title: '匹配', key: 'level', dataIndex: 'level', width: 100 },
  { title: '说明', key: 'reason', dataIndex: 'reason', ellipsis: true },
  { title: '证据', key: 'evidence', width: 160 },
]

// --- fetch ---
async function fetchDetail() {
  loading.value = true
  error.value = ''
  job.value = null
  excludedReasons.value = []
  matchResults.value = []

  try {
    const data = await api.getInboxJobDetail(jobId.value)
    job.value = data

    // Extract match results from the detail response
    matchResults.value = data.match_results || data.requirement_matches || []

    // If excluded, fetch excluded reasons
    if (data.verdict === 'excluded') {
      try {
        const reasonsData = await api.getInboxExcludedReasons(jobId.value)
        excludedReasons.value = reasonsData.reasons || reasonsData.blocking_reasons || []
      } catch {
        // Reasons fetch failed; not critical
        if (data.blocking_reasons?.length) {
          excludedReasons.value = data.blocking_reasons.map(r =>
            typeof r === 'string' ? { message: r } : r
          )
        }
      }
    }
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      error.value = '收件箱服务未就绪，请稍后重试。'
    } else if (parsed.status === 404) {
      // Will show 404 result
    } else {
      error.value = formatApiError(err, '加载职位详情失败，请稍后重试。')
    }
  } finally {
    loading.value = false
  }
}

// --- actions ---
async function onFavorite() {
  actionLoading.value = 'fav'
  error.value = ''
  try {
    const result = await api.favoriteInboxJob(jobId.value)
    message.success('已收藏')
    if (job.value) {
      // Capture the application_id returned by the idempotent favorite so the
      // "prepare" CTA can navigate straight into the application workspace
      // without a second round-trip (task 14.3).
      job.value = {
        ...job.value,
        application_state: 'favorited',
        application_id: result?.application_id || job.value?.application_id || '',
      }
    }
  } catch (err) {
    const parsed = parseApiError(err)
    error.value = formatApiError(err, '操作失败，请稍后重试。')
  } finally {
    actionLoading.value = ''
  }
}

// --- action path (task 14.3) ---
const navigating = ref(false)

// Any state past "ignored" implies an application record exists and the user
// should be able to (re)enter the workspace to continue the path.
const ACTIVE_APPLICATION_STATES = new Set([
  'favorited',
  'preparing',
  'submitted',
  'interviewing',
  'offer',
  'rejected',
  'withdrawn',
  'on_hold',
])

const hasApplication = computed(
  () => !!job.value && ACTIVE_APPLICATION_STATES.has(job.value.application_state),
)

const ctaTitle = computed(() =>
  job.value?.application_state === 'favorited'
    ? '准备申请材料'
    : '继续申请流程',
)

const ctaSubtitle = computed(() =>
  job.value?.application_state === 'favorited'
    ? '进入申请工作区，准备简历、求职信与申请包。'
    : '进入申请工作区，查看申请包、投递通道与事件时间线。',
)

const ctaButtonLabel = computed(() =>
  job.value?.application_state === 'favorited' ? '准备申请' : '进入工作区',
)

function goToWorkspace() {
  const appId = job.value?.application_id
  if (!appId) return
  navigating.value = true
  router.push(`/applications/${appId}`)
}

async function onIgnore() {
  actionLoading.value = 'ign'
  error.value = ''
  try {
    await api.ignoreInboxJob(jobId.value)
    message.success('已忽略')
    if (job.value) {
      job.value = { ...job.value, application_state: 'ignored' }
    }
  } catch (err) {
    const parsed = parseApiError(err)
    error.value = formatApiError(err, '操作失败，请稍后重试。')
  } finally {
    actionLoading.value = ''
  }
}

function onSnooze() {
  snoozePreset.value = '1d'
  snoozeCustomDate.value = null
  snoozeModalVisible.value = true
}

function onSnoozePresetChange() {
  snoozeCustomDate.value = null
}

function disabledSnoozeDate(current) {
  return current && current < Date.now()
}

async function confirmSnooze() {
  let snoozedUntil
  const now = new Date()
  switch (snoozePreset.value) {
    case '1h':
      snoozedUntil = new Date(now.getTime() + 60 * 60 * 1000)
      break
    case '4h':
      snoozedUntil = new Date(now.getTime() + 4 * 60 * 60 * 1000)
      break
    case '1d':
      snoozedUntil = new Date(now.getTime() + 24 * 60 * 60 * 1000)
      break
    case '7d':
      snoozedUntil = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000)
      break
    case 'custom':
      if (!snoozeCustomDate.value) {
        message.warning('请选择自定义时间')
        return
      }
      snoozedUntil = snoozeCustomDate.value
      break
    default:
      snoozedUntil = new Date(now.getTime() + 24 * 60 * 60 * 1000)
  }

  snoozing.value = true
  try {
    await api.snoozeInboxJob(jobId.value, {
      snoozed_until: snoozedUntil.toISOString(),
    })
    message.success('已隐藏至指定时间')
    snoozeModalVisible.value = false
    router.push('/inbox')
  } catch (err) {
    const parsed = parseApiError(err)
    error.value = formatApiError(err, '操作失败，请稍后重试。')
  } finally {
    snoozing.value = false
  }
}

function goToEvidence(refId) {
  router.push(`/evidence?highlight=${refId}`)
}

// --- display helpers ---
function verdictColor(verdict) {
  return verdict === 'recommended' ? 'green' : 'red'
}

function verdictLabel(verdict) {
  return verdict === 'recommended' ? '推荐' : '已排除'
}

function appStateColor(state) {
  return {
    favorited: 'gold',
    preparing: 'blue',
    submitted: 'green',
    ignored: 'default',
  }[state] || 'default'
}

function appStateLabel(state) {
  return {
    favorited: '已收藏',
    preparing: '准备中',
    submitted: '已投递',
    ignored: '已忽略',
  }[state] || state || ''
}

function remoteLabel(type) {
  return {
    remote: '远程',
    hybrid: '混合',
    onsite: '驻场',
  }[type] || type || '未知'
}

function matchLevelColor(level) {
  return {
    strong: 'green',
    partial: 'gold',
    unsupported: 'red',
  }[level] || 'default'
}

function matchLevelLabel(level) {
  return {
    strong: '完全匹配',
    partial: '部分匹配',
    unsupported: '不满足',
  }[level] || level || '未知'
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

// --- lifecycle ---
onMounted(fetchDetail)
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
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}

.detail-card {
  border: 1px solid #eaecf0;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
}

.excluded-card {
  border-color: #ffccc7;
  background: #fff2f0;
}

.excluded-reason {
  margin-bottom: 12px;
}
.excluded-reason:last-child {
  margin-bottom: 0;
}
.excluded-reason__text {
  color: #344054;
  font-size: 13px;
  margin: 4px 0 2px;
}
.excluded-reason__evidence {
  margin-top: 4px;
}

.match-notice {
  margin-bottom: 12px;
}

.evidence-tag {
  cursor: pointer;
}

/* CTA card */
.cta-card {
  border-color: #b7eb8f;
  background: #f6ffed;
}
.cta-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
}

/* Description */
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

/* Loading / utility */
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
.muted {
  color: #999;
  font-size: 12px;
}
</style>
