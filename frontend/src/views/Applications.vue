<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>投递管理</h1>
        <p>追踪每一份申请的流转状态，掌控求职进度。</p>
      </div>
      <a-tag color="blue">{{ total }} 条记录</a-tag>
    </div>

    <a-alert v-if="error" type="error" show-icon :message="error" closable @close="error = ''" />

    <a-card class="table-card">
      <div class="toolbar">
        <div class="toolbar-controls">
          <a-select v-model:value="stateFilter" style="width: 160px" @change="resetAndFetch">
            <template #suffixIcon><FilterOutlined /></template>
            <a-select-option value="">全部状态</a-select-option>
            <a-select-option value="favorited">已收藏</a-select-option>
            <a-select-option value="preparing">准备中</a-select-option>
            <a-select-option value="submitted">已投递</a-select-option>
            <a-select-option value="ignored">已忽略</a-select-option>
          </a-select>
        </div>
        <a-button :loading="loading" @click="resetAndFetch">
          <template #icon><ReloadOutlined /></template>
          刷新
        </a-button>
      </div>

      <a-spin :spinning="loading && apps.length === 0">
        <a-table
          v-if="apps.length > 0 || !loading"
          :columns="columns"
          :data-source="apps"
          :loading="false"
          :pagination="false"
          row-key="id"
          :scroll="{ x: 900 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'job'">
              <router-link :to="`/jobs/${record.canonical_job_id}`" class="table-link">
                {{ record.canonical_job_id.slice(0, 8) }}...
              </router-link>
            </template>
            <template v-else-if="column.key === 'state'">
              <div class="state-cell">
                <a-tag :color="stateColor(record.state)">{{ stateLabel(record.state) }}</a-tag>
                <a-steps
                  :current="stateIndex(record.state)"
                  :items="stateSteps"
                  size="small"
                  :progress-dot="true"
                  class="state-steps"
                />
              </div>
            </template>
            <template v-else-if="column.key === 'url'">
              <a v-if="record.apply_url" :href="record.apply_url" target="_blank" rel="noreferrer" class="table-link">
                {{ truncateUrl(record.apply_url) }}
              </a>
              <span v-else class="muted">-</span>
            </template>
            <template v-else-if="column.key === 'submitted'">
              <span v-if="record.submitted_at">{{ formatDate(record.submitted_at) }}</span>
              <span v-else class="muted">-</span>
            </template>
            <template v-else-if="column.key === 'action'">
              <a-space>
                <a-popconfirm
                  v-if="['favorited', 'preparing'].includes(record.state)"
                  title="确认将该申请标记为已投递？"
                  ok-text="确认"
                  cancel-text="取消"
                  @confirm="submit(record.id)"
                >
                  <a-button type="link" size="small" :loading="submittingId === record.id">
                    标记投递
                  </a-button>
                </a-popconfirm>
                <span v-else-if="record.state === 'submitted'" class="muted">已完成</span>
                <span v-else class="muted">-</span>
              </a-space>
            </template>
          </template>
          <template #emptyText>
            <a-empty :description="emptyText" />
          </template>
        </a-table>
      </a-spin>

      <div v-if="hasMore" class="load-more">
        <a-button :loading="loadingMore" @click="loadMore">加载更多</a-button>
      </div>
    </a-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { FilterOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { api } from '../api/client.js'

const apps = ref([])
const stateFilter = ref('')
const loading = ref(false)
const loadingMore = ref(false)
const submittingId = ref('')
const error = ref('')
const total = ref(0)
const nextCursor = ref(null)

const columns = [
  { title: '职位', key: 'job', dataIndex: 'canonical_job_id', width: 180 },
  { title: '状态', key: 'state', dataIndex: 'state', width: 240 },
  { title: '投递链接', key: 'url', dataIndex: 'apply_url' },
  { title: '投递时间', key: 'submitted', dataIndex: 'submitted_at', width: 140 },
  { title: '操作', key: 'action', width: 120 },
]

const stateSteps = [
  { title: '已收藏' },
  { title: '准备中' },
  { title: '已投递' },
]

const stateFlow = ['favorited', 'preparing', 'submitted']

const hasMore = computed(() => nextCursor.value !== null)

const emptyText = computed(() => {
  if (stateFilter.value) return '该状态下暂无申请记录'
  return '暂无投递记录，去职位列表收藏一份吧'
})

function stateIndex(state) {
  const idx = stateFlow.indexOf(state)
  return idx >= 0 ? idx : 0
}

function stateColor(state) {
  return {
    favorited: 'gold',
    preparing: 'blue',
    submitted: 'green',
    ignored: 'default',
  }[state] || 'default'
}

function stateLabel(state) {
  return {
    favorited: '已收藏',
    preparing: '准备中',
    submitted: '已投递',
    ignored: '已忽略',
  }[state] || state || '未知'
}

function truncateUrl(url) {
  if (!url) return '-'
  try {
    const { hostname, pathname } = new URL(url)
    return hostname + (pathname.length > 20 ? pathname.slice(0, 20) + '...' : pathname)
  } catch {
    return url.length > 40 ? url.slice(0, 40) + '...' : url
  }
}

function formatDate(date) {
  if (!date) return '-'
  return new Date(date).toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })
}

async function fetchApps(cursor) {
  const isLoadMore = !!cursor
  if (isLoadMore) {
    loadingMore.value = true
  } else {
    loading.value = true
    error.value = ''
  }

  try {
    const params = { limit: 50 }
    if (cursor) params.cursor = cursor
    if (stateFilter.value) params.state = stateFilter.value

    const data = await api.listApplications(params)

    if (isLoadMore) {
      apps.value = [...apps.value, ...(data.items || [])]
    } else {
      apps.value = data.items || []
    }
    total.value = data.total || 0
    nextCursor.value = data.next_cursor ?? null
  } catch (err) {
    error.value = err.message || '加载申请列表失败，请稍后重试。'
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function resetAndFetch() {
  nextCursor.value = null
  fetchApps(null)
}

function loadMore() {
  if (nextCursor.value) {
    fetchApps(nextCursor.value)
  }
}

async function submit(id) {
  submittingId.value = id
  error.value = ''
  try {
    await api.submitApplication(id)
    await resetAndFetch()
  } catch (err) {
    error.value = err.message || '操作失败，请稍后重试。'
  } finally {
    submittingId.value = ''
  }
}

onMounted(() => fetchApps(null))
</script>

<style scoped>
.load-more {
  text-align: center;
  padding: 16px 0;
}

.state-cell {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.state-steps {
  margin-top: 2px;
}

.state-steps :deep(.ant-steps-item-title) {
  font-size: 12px !important;
}

.state-steps :deep(.ant-steps-item-icon) {
  width: 14px;
  height: 14px;
  font-size: 10px;
  line-height: 14px;
}

.state-steps :deep(.ant-steps-item-tail) {
  padding: 0;
}
</style>
