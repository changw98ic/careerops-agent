<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>职位收件箱</h1>
        <p>经过筛选和匹配的职位推荐，收藏感兴趣的职位进入申请流程。</p>
      </div>
      <a-tag color="blue">{{ total }} 条记录</a-tag>
    </div>

    <a-alert v-if="error" type="error" show-icon :message="error" closable @close="error = ''" />

    <a-card class="table-card">
      <!-- Tab bar: recommended / excluded -->
      <a-tabs v-model:activeKey="activeTab" @change="onTabChange">
        <a-tab-pane key="recommended" tab="推荐职位" />
        <a-tab-pane key="excluded" tab="已排除" />
      </a-tabs>

      <!-- Toolbar: search + filters + refresh -->
      <div class="toolbar">
        <div class="toolbar-controls">
          <a-input-search
            v-model:value="searchInput"
            allow-clear
            placeholder="搜索职位名称或公司"
            style="width: 260px"
            @search="onSearch"
            @change="onSearchInputChange"
          >
            <template #prefix><SearchOutlined /></template>
          </a-input-search>

          <a-select
            v-model:value="filterRole"
            allow-clear
            placeholder="职位类型"
            style="width: 150px"
            @change="resetAndFetch"
          >
            <a-select-option value="">全部职位</a-select-option>
            <a-select-option v-for="r in profileRoles" :key="r" :value="r">{{ r }}</a-select-option>
          </a-select>

          <a-select
            v-model:value="filterLocation"
            allow-clear
            placeholder="地点"
            style="width: 140px"
            @change="resetAndFetch"
          >
            <a-select-option value="">全部地点</a-select-option>
            <a-select-option v-for="loc in profileLocations" :key="loc" :value="loc">{{ loc }}</a-select-option>
          </a-select>

          <a-select
            v-model:value="filterRemote"
            allow-clear
            placeholder="远程"
            style="width: 120px"
            @change="resetAndFetch"
          >
            <a-select-option value="">不限</a-select-option>
            <a-select-option value="remote">远程</a-select-option>
            <a-select-option value="hybrid">混合</a-select-option>
            <a-select-option value="onsite">驻场</a-select-option>
          </a-select>

          <a-select
            v-model:value="filterSeniority"
            allow-clear
            placeholder="职级"
            style="width: 120px"
            @change="resetAndFetch"
          >
            <a-select-option value="">全部职级</a-select-option>
            <a-select-option value="junior">初级</a-select-option>
            <a-select-option value="mid">中级</a-select-option>
            <a-select-option value="senior">高级</a-select-option>
            <a-select-option value="lead">主管</a-select-option>
            <a-select-option value="principal">首席</a-select-option>
          </a-select>
        </div>

        <a-button :loading="loading" @click="resetAndFetch">
          <template #icon><ReloadOutlined /></template>
          刷新
        </a-button>
      </div>

      <!-- Content -->
      <a-spin :spinning="loading && items.length === 0">
        <template v-if="items.length > 0">
          <div class="inbox-cards">
            <div
              v-for="item in items"
              :key="item.canonical_job_id"
              class="inbox-card"
              :class="{ 'inbox-card--excluded': item.verdict === 'excluded' }"
              @click="goToDetail(item.canonical_job_id)"
            >
              <div class="inbox-card__header">
                <div class="inbox-card__title-group">
                  <span class="inbox-card__title">{{ item.title || 'Untitled' }}</span>
                  <a-tag
                    :color="verdictColor(item.verdict)"
                    size="small"
                  >
                    {{ verdictLabel(item.verdict) }}
                  </a-tag>
                </div>
                <div class="inbox-card__actions" @click.stop>
                  <a-tooltip title="收藏">
                    <a-button
                      type="text"
                      size="small"
                      :class="{ 'action-active': item.application_state === 'favorited' }"
                      :loading="actionLoading === item.canonical_job_id + '-fav'"
                      @click="onFavorite(item.canonical_job_id)"
                    >
                      <template #icon><StarOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip title="忽略">
                    <a-button
                      type="text"
                      size="small"
                      danger
                      :class="{ 'action-ignored': item.application_state === 'ignored' }"
                      :loading="actionLoading === item.canonical_job_id + '-ign'"
                      @click="onIgnore(item.canonical_job_id)"
                    >
                      <template #icon><CloseCircleOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip title="稍后处理">
                    <a-button
                      type="text"
                      size="small"
                      :loading="actionLoading === item.canonical_job_id + '-snz'"
                      @click="onSnooze(item.canonical_job_id)"
                    >
                      <template #icon><ClockCircleOutlined /></template>
                    </a-button>
                  </a-tooltip>
                </div>
              </div>

              <div class="inbox-card__meta">
                <span v-if="item.company" class="inbox-card__company">{{ item.company }}</span>
                <span v-if="item.location" class="inbox-card__location">
                  <EnvironmentOutlined /> {{ item.location }}
                </span>
              </div>

              <!-- Blocking reasons for excluded items -->
              <div v-if="item.verdict === 'excluded' && item.blocking_reasons?.length" class="inbox-card__reasons">
                <a-tag
                  v-for="(reason, idx) in item.blocking_reasons.slice(0, 3)"
                  :key="idx"
                  color="red"
                  size="small"
                >
                  {{ reason }}
                </a-tag>
                <a-tag v-if="item.blocking_reasons.length > 3" size="small" color="default">
                  +{{ item.blocking_reasons.length - 3 }}
                </a-tag>
              </div>

              <!-- Provenance: source + crawl run -->
              <div class="inbox-card__provenance">
                <span v-if="item.source_name" class="provenance-item">
                  <LinkOutlined /> {{ item.source_name }}
                </span>
                <span v-if="item.crawl_run_id" class="provenance-item">
                  <ThunderboltOutlined /> Run {{ item.crawl_run_id.slice(0, 8) }}
                </span>
                <span v-if="item.captured_at" class="provenance-item provenance-time">
                  {{ formatRelativeTime(item.captured_at) }}
                </span>
              </div>

              <!-- Application state badge if already acted on -->
              <div v-if="item.application_state && item.application_state !== 'none'" class="inbox-card__app-state">
                <a-tag :color="appStateColor(item.application_state)" size="small">
                  {{ appStateLabel(item.application_state) }}
                </a-tag>
              </div>
            </div>
          </div>
        </template>

        <a-empty
          v-else-if="!loading"
          :description="emptyText"
        >
          <template #image>
            <InboxOutlined style="font-size: 48px; color: #d9d9d9" />
          </template>
        </a-empty>
      </a-spin>

      <div v-if="hasMore" class="load-more">
        <a-button :loading="loadingMore" @click="loadMore">加载更多</a-button>
      </div>
    </a-card>

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
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ClockCircleOutlined,
  CloseCircleOutlined,
  EnvironmentOutlined,
  InboxOutlined,
  LinkOutlined,
  ReloadOutlined,
  SearchOutlined,
  StarOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { api, parseApiError } from '../api/client.js'

const router = useRouter()

// --- state ---
const items = ref([])
const activeTab = ref('recommended')
const searchInput = ref('')
const searchQuery = ref('')
const filterRole = ref('')
const filterLocation = ref('')
const filterRemote = ref('')
const filterSeniority = ref('')
const loading = ref(false)
const loadingMore = ref(false)
const error = ref('')
const total = ref(0)
const nextCursor = ref(null)
const actionLoading = ref('')

// Profile data for filter options
const profileRoles = ref([])
const profileLocations = ref([])

// Snooze modal
const snoozeModalVisible = ref(false)
const snoozing = ref(false)
const snoozeJobId = ref('')
const snoozePreset = ref('1d')
const snoozeCustomDate = ref(null)

let debounceTimer = null

// --- computed ---
const hasMore = computed(() => nextCursor.value !== null)

const emptyText = computed(() => {
  if (searchQuery.value) return `未找到匹配「${searchQuery.value}」的职位`
  if (activeTab.value === 'recommended') return '暂无推荐职位，请先配置采集计划和职业画像'
  if (activeTab.value === 'excluded') return '暂无被排除的职位'
  return '暂无职位数据'
})

// --- filters build ---
function buildParams(cursor) {
  const params = { limit: 50, tab: activeTab.value }
  if (cursor) params.cursor = cursor
  if (searchQuery.value) params.q = searchQuery.value
  // Additional filters sent as query params (backend may ignore unknown ones;
  // the client sends them for future server-side filtering support)
  if (filterRole.value) params.role = filterRole.value
  if (filterLocation.value) params.location = filterLocation.value
  if (filterRemote.value) params.remote = filterRemote.value
  if (filterSeniority.value) params.seniority = filterSeniority.value
  return params
}

// --- fetch ---
async function fetchInbox(cursor) {
  const isLoadMore = !!cursor
  if (isLoadMore) {
    loadingMore.value = true
  } else {
    loading.value = true
    error.value = ''
  }

  try {
    const data = await api.listInbox(buildParams(cursor))

    if (isLoadMore) {
      items.value = [...items.value, ...(data.items || [])]
    } else {
      items.value = data.items || []
    }
    total.value = data.total || 0
    nextCursor.value = data.next_cursor ?? null
  } catch (err) {
    const parsed = parseApiError(err)
    if (parsed.isDependencyNotReady) {
      error.value = '收件箱服务未就绪，请稍后重试。'
    } else {
      error.value = err.message || '加载收件箱失败，请稍后重试。'
    }
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function resetAndFetch() {
  nextCursor.value = null
  fetchInbox(null)
}

function loadMore() {
  if (nextCursor.value) {
    fetchInbox(nextCursor.value)
  }
}

// --- search ---
function onSearch(value) {
  searchQuery.value = value.trim()
  resetAndFetch()
}

function onSearchInputChange() {
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    searchQuery.value = searchInput.value.trim()
    resetAndFetch()
  }, 400)
}

// --- tab ---
function onTabChange() {
  resetAndFetch()
}

// --- navigation ---
function goToDetail(jobId) {
  router.push(`/inbox/${jobId}`)
}

// --- actions ---
async function onFavorite(jobId) {
  actionLoading.value = jobId + '-fav'
  error.value = ''
  try {
    await api.favoriteInboxJob(jobId)
    message.success('已收藏')
    // Update local state optimistically
    const idx = items.value.findIndex(i => i.canonical_job_id === jobId)
    if (idx >= 0) {
      items.value[idx] = { ...items.value[idx], application_state: 'favorited' }
    }
  } catch (err) {
    const parsed = parseApiError(err)
    error.value = parsed.message || '操作失败，请稍后重试。'
  } finally {
    actionLoading.value = ''
  }
}

async function onIgnore(jobId) {
  actionLoading.value = jobId + '-ign'
  error.value = ''
  try {
    await api.ignoreInboxJob(jobId)
    message.success('已忽略')
    // Remove from current list view
    items.value = items.value.filter(i => i.canonical_job_id !== jobId)
    total.value = Math.max(0, total.value - 1)
  } catch (err) {
    const parsed = parseApiError(err)
    error.value = parsed.message || '操作失败，请稍后重试。'
  } finally {
    actionLoading.value = ''
  }
}

function onSnooze(jobId) {
  snoozeJobId.value = jobId
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
    await api.snoozeInboxJob(snoozeJobId.value, {
      snoozed_until: snoozedUntil.toISOString(),
    })
    message.success('已隐藏至指定时间')
    snoozeModalVisible.value = false
    // Remove from current list
    items.value = items.value.filter(i => i.canonical_job_id !== snoozeJobId.value)
    total.value = Math.max(0, total.value - 1)
  } catch (err) {
    const parsed = parseApiError(err)
    error.value = parsed.message || '操作失败，请稍后重试。'
  } finally {
    snoozing.value = false
  }
}

// --- profile data for filters ---
async function loadProfileFilters() {
  try {
    const profile = await api.getActiveProfile()
    const prefs = profile.preferences || {}
    profileRoles.value = prefs.target_roles || []
    profileLocations.value = (prefs.locations || []).map(l => l.name || l)
  } catch {
    // Profile not configured yet; filters remain empty
  }
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

function formatRelativeTime(dateStr) {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  const now = new Date()
  const diffMs = now - date
  const diffMin = Math.floor(diffMs / 60000)
  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin} 分钟前`
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return `${diffHour} 小时前`
  const diffDay = Math.floor(diffHour / 24)
  if (diffDay < 30) return `${diffDay} 天前`
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}

// --- lifecycle ---
onMounted(() => {
  fetchInbox(null)
  loadProfileFilters()
})
</script>

<style scoped>
/* Tab bar */
:deep(.ant-tabs) {
  margin-bottom: 4px;
}
:deep(.ant-tabs-nav) {
  margin-bottom: 8px;
}

/* Toolbar */
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
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}

/* Inbox cards */
.inbox-cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.inbox-card {
  border: 1px solid #eaecf0;
  border-radius: 8px;
  padding: 16px;
  cursor: pointer;
  transition: border-color 0.2s, box-shadow 0.2s;
  background: #fff;
}
.inbox-card:hover {
  border-color: #1677ff;
  box-shadow: 0 1px 4px rgba(22, 119, 255, 0.12);
}
.inbox-card--excluded {
  background: #fafafa;
  border-color: #f0f0f0;
}
.inbox-card--excluded:hover {
  border-color: #ff7875;
  box-shadow: 0 1px 4px rgba(255, 77, 79, 0.1);
}

.inbox-card__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.inbox-card__title-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.inbox-card__title {
  font-size: 16px;
  font-weight: 600;
  color: #101828;
}

.inbox-card__actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}
.action-active {
  color: #faad14 !important;
}
.action-ignored {
  color: #ff4d4f !important;
}

.inbox-card__meta {
  display: flex;
  gap: 16px;
  align-items: center;
  margin-top: 8px;
  color: #667085;
  font-size: 13px;
}
.inbox-card__company {
  font-weight: 500;
  color: #344054;
}
.inbox-card__location {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.inbox-card__reasons {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 10px;
}

.inbox-card__provenance {
  display: flex;
  gap: 14px;
  align-items: center;
  margin-top: 10px;
  font-size: 12px;
  color: #98a2b3;
}
.provenance-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.provenance-time {
  margin-left: auto;
}

.inbox-card__app-state {
  margin-top: 8px;
}

/* Load more */
.load-more {
  text-align: center;
  padding: 16px 0;
}
</style>
