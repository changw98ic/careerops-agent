<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>职位收件箱</h1>
        <p>浏览并筛选职位机会，点击查看详情。</p>
      </div>
      <a-tag color="blue">{{ total }} 条记录</a-tag>
    </div>

    <a-alert v-if="error" type="error" show-icon :message="error" closable @close="error = ''" />

    <a-card class="table-card">
      <div class="toolbar">
        <div class="toolbar-controls">
          <a-input-search
            v-model:value="searchInput"
            allow-clear
            placeholder="搜索职位名称"
            style="width: 280px"
            @search="onSearch"
            @change="onSearchInputChange"
          >
            <template #prefix><SearchOutlined /></template>
          </a-input-search>
          <a-select v-model:value="stateFilter" style="width: 160px" @change="resetAndFetch">
            <template #suffixIcon><FilterOutlined /></template>
            <a-select-option value="">全部状态</a-select-option>
            <a-select-option value="active">进行中</a-select-option>
            <a-select-option value="closed">已关闭</a-select-option>
          </a-select>
        </div>
        <a-button :loading="loading" @click="resetAndFetch">
          <template #icon><ReloadOutlined /></template>
          刷新
        </a-button>
      </div>

      <a-spin :spinning="loading && jobs.length === 0">
        <a-table
          v-if="jobs.length > 0 || !loading"
          :columns="columns"
          :data-source="jobs"
          :loading="false"
          :pagination="false"
          row-key="id"
          :scroll="{ x: 720 }"
          :custom-row="onRowClick"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'title'">
              <div class="table-link">{{ record.canonical_title }}</div>
              <div class="muted">{{ record.company_id.slice(0, 8) }}...</div>
            </template>
            <template v-else-if="column.key === 'state'">
              <a-tag :color="stateColor(record.aggregate_state)">
                {{ stateLabel(record.aggregate_state) }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'action'">
              <router-link :to="`/jobs/${record.id}`" @click.stop>
                <a-button type="link" size="small">查看详情</a-button>
              </router-link>
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
import { useRouter } from 'vue-router'
import {
  FilterOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons-vue'
import { api } from '../api/client.js'

const router = useRouter()

const jobs = ref([])
const searchInput = ref('')
const searchQuery = ref('')
const stateFilter = ref('')
const loading = ref(false)
const loadingMore = ref(false)
const error = ref('')
const total = ref(0)
const nextCursor = ref(null)

let debounceTimer = null

const columns = [
  { title: '职位名称', key: 'title', dataIndex: 'canonical_title' },
  { title: '状态', key: 'state', dataIndex: 'aggregate_state', width: 140 },
  { title: '操作', key: 'action', width: 140 },
]

const hasMore = computed(() => nextCursor.value !== null)

const emptyText = computed(() => {
  if (searchQuery.value) return `未找到匹配「${searchQuery.value}」的职位`
  if (stateFilter.value) return '该状态下暂无职位'
  return '暂无职位数据'
})

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

async function fetchJobs(cursor) {
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
    if (searchQuery.value) params.q = searchQuery.value

    const data = await api.listJobs(params)

    if (isLoadMore) {
      jobs.value = [...jobs.value, ...(data.items || [])]
    } else {
      jobs.value = data.items || []
    }
    total.value = data.total || 0
    nextCursor.value = data.next_cursor ?? null
  } catch (err) {
    error.value = err.message || '加载职位列表失败，请稍后重试。'
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function resetAndFetch() {
  nextCursor.value = null
  fetchJobs(null)
}

function loadMore() {
  if (nextCursor.value) {
    fetchJobs(nextCursor.value)
  }
}

function onRowClick(record) {
  return {
    onClick: () => {
      router.push(`/jobs/${record.id}`)
    },
    style: { cursor: 'pointer' },
  }
}

function stateColor(state) {
  if (state === 'active') return 'green'
  if (state === 'closed') return 'default'
  return 'gold'
}

function stateLabel(state) {
  if (state === 'active') return '进行中'
  if (state === 'closed') return '已关闭'
  return state || '未知'
}

onMounted(() => fetchJobs(null))
</script>

<style scoped>
.load-more {
  text-align: center;
  padding: 16px 0;
}
.table-link {
  color: #1677ff;
  font-weight: 500;
}
.muted {
  color: #999;
  font-size: 12px;
}
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
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
</style>
