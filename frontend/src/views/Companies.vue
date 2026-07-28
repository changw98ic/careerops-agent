<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>公司列表</h1>
        <p>浏览已收录的公司及其抓取状态。</p>
      </div>
      <a-tag color="blue">{{ total }} 家公司</a-tag>
    </div>

    <a-alert v-if="error" type="error" show-icon :message="error" closable @close="error = ''" />

    <a-card class="table-card">
      <div class="toolbar">
        <div class="toolbar-controls">
          <a-input-search
            v-model:value="searchInput"
            allow-clear
            placeholder="搜索公司名称"
            style="width: 280px"
            @search="onSearch"
            @change="onSearchInputChange"
          >
            <template #prefix><SearchOutlined /></template>
          </a-input-search>
        </div>
        <a-button :loading="loading" @click="resetAndFetch">
          <template #icon><ReloadOutlined /></template>
          刷新
        </a-button>
      </div>

      <a-spin :spinning="loading && companies.length === 0">
        <a-table
          v-if="companies.length > 0 || !loading"
          :columns="columns"
          :data-source="companies"
          :loading="false"
          :pagination="false"
          row-key="id"
          :scroll="{ x: 760 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'name'">
              <strong>{{ record.name }}</strong>
              <div class="muted">{{ record.normalized_name }}</div>
            </template>
            <template v-else-if="column.key === 'domains'">
              <a-space wrap>
                <a-tag v-for="domain in record.official_domains" :key="domain">{{ domain }}</a-tag>
                <span v-if="!record.official_domains?.length" class="muted">暂无域名</span>
              </a-space>
            </template>
            <template v-else-if="column.key === 'terms'">
              <a-tag :color="termsColor(record.terms_status)">{{ termsLabel(record.terms_status) }}</a-tag>
            </template>
            <template v-else-if="column.key === 'action'">
              <router-link :to="{ name: 'jobs', query: { company_id: record.id } }">
                <a-button type="link" size="small">查看职位</a-button>
              </router-link>
            </template>
            <template v-else-if="column.key === 'created'">
              {{ formatDate(record.created_at) }}
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
import {
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons-vue'
import { api, formatApiError } from '../api/client.js'

const companies = ref([])
const searchInput = ref('')
const searchQuery = ref('')
const loading = ref(false)
const loadingMore = ref(false)
const error = ref('')
const total = ref(0)
const nextCursor = ref(null)

let debounceTimer = null

const columns = [
  { title: '公司名称', key: 'name', dataIndex: 'name' },
  { title: '官方域名', key: 'domains', dataIndex: 'official_domains' },
  { title: '抓取状态', key: 'terms', dataIndex: 'terms_status', width: 140 },
  { title: '操作', key: 'action', width: 120 },
  { title: '收录时间', key: 'created', dataIndex: 'created_at', width: 140 },
]

const hasMore = computed(() => nextCursor.value !== null)

const emptyText = computed(() => {
  if (searchQuery.value) return `未找到匹配「${searchQuery.value}」的公司`
  return '暂无公司数据'
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

async function fetchCompanies(cursor) {
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
    if (searchQuery.value) params.q = searchQuery.value

    const data = await api.listCompanies(params)

    if (isLoadMore) {
      companies.value = [...companies.value, ...(data.items || [])]
    } else {
      companies.value = data.items || []
    }
    total.value = data.total || 0
    nextCursor.value = data.next_cursor ?? null
  } catch (err) {
    error.value = formatApiError(err, '加载公司列表失败，请稍后重试。')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function resetAndFetch() {
  nextCursor.value = null
  fetchCompanies(null)
}

function loadMore() {
  if (nextCursor.value) {
    fetchCompanies(nextCursor.value)
  }
}

function termsColor(status) {
  if (status === 'allowed') return 'green'
  if (status === 'blocked') return 'red'
  return 'gold'
}

function termsLabel(status) {
  if (status === 'allowed') return '允许抓取'
  if (status === 'blocked') return '已屏蔽'
  return status || '未知'
}

function formatDate(date) {
  if (!date) return '—'
  return new Date(date).toLocaleDateString('zh-CN')
}

onMounted(() => fetchCompanies(null))
</script>

<style scoped>
.load-more {
  text-align: center;
  padding: 16px 0;
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
