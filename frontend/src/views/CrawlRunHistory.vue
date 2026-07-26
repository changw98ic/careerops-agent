<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>运行历史</h1>
        <p>查看采集计划的运行记录、状态和结果计数。</p>
      </div>
      <a-tag color="blue">{{ total }} 条运行</a-tag>
    </div>

    <!-- Dependency not ready (503) -->
    <a-alert
      v-if="unavailable"
      type="warning"
      show-icon
      message="采集服务暂不可用"
      description="后端仓储或依赖未就绪（503）。请稍后重试。"
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

    <a-card class="table-card">
      <div class="toolbar">
        <div class="toolbar-controls">
          <a-select v-model:value="stateFilter" style="width: 160px" @change="resetAndFetch">
            <a-select-option value="">全部状态</a-select-option>
            <a-select-option value="pending">待运行</a-select-option>
            <a-select-option value="running">运行中</a-select-option>
            <a-select-option value="succeeded">成功</a-select-option>
            <a-select-option value="failed">失败</a-select-option>
            <a-select-option value="cancelled">已取消</a-select-option>
            <a-select-option value="timeout">超时</a-select-option>
          </a-select>
          <span v-if="planVersionId" class="filter-badge">
            已筛选计划版本
            <a-button type="link" size="small" @click="clearPlanFilter">清除</a-button>
          </span>
        </div>
        <a-button :loading="loading" @click="resetAndFetch">
          刷新
        </a-button>
      </div>

      <a-spin :spinning="loading && runs.length === 0">
        <a-table
          v-if="runs.length > 0 || !loading"
          :columns="columns"
          :data-source="runs"
          :pagination="false"
          row-key="id"
          :scroll="{ x: 900 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'identity'">
              <router-link :to="`/crawl-runs/${record.id}`" class="table-link">
                <span class="mono">{{ record.run_identity.slice(0, 12) }}…</span>
              </router-link>
            </template>
            <template v-else-if="column.key === 'state'">
              <a-tag :color="runStateColor(record.state)">{{ runStateLabel(record.state) }}</a-tag>
            </template>
            <template v-else-if="column.key === 'counters'">
              <a-space :size="8" wrap>
                <span title="发现">
                  <a-tag size="small" color="blue">{{ record.counters?.discovered || 0 }}</a-tag>
                  <span class="counter-label">发现</span>
                </span>
                <span title="更新">
                  <a-tag size="small" color="cyan">{{ record.counters?.updated || 0 }}</a-tag>
                  <span class="counter-label">更新</span>
                </span>
                <span title="关闭">
                  <a-tag size="small">{{ record.counters?.closed || 0 }}</a-tag>
                  <span class="counter-label">关闭</span>
                </span>
                <span title="失败">
                  <a-tag size="small" :color="record.counters?.failed > 0 ? 'red' : 'default'">{{ record.counters?.failed || 0 }}</a-tag>
                  <span class="counter-label">失败</span>
                </span>
              </a-space>
            </template>
            <template v-else-if="column.key === 'error'">
              <a-tag v-if="record.error_category" color="red" size="small">{{ record.error_category }}</a-tag>
              <span v-else class="muted">-</span>
            </template>
            <template v-else-if="column.key === 'next_eligible'">
              <span v-if="record.next_eligible_at">{{ formatDate(record.next_eligible_at) }}</span>
              <span v-else class="muted">-</span>
            </template>
            <template v-else-if="column.key === 'created'">
              <span v-if="record.created_at">{{ formatDate(record.created_at) }}</span>
              <span v-else class="muted">-</span>
            </template>
            <template v-else-if="column.key === 'action'">
              <a-space>
                <router-link :to="`/crawl-runs/${record.id}`">
                  <a-button type="link" size="small">详情</a-button>
                </router-link>
                <router-link :to="{ name: 'jobs', query: { crawl_run_id: record.id } }">
                  <a-button type="link" size="small">查看职位</a-button>
                </router-link>
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
import { useRoute, useRouter } from 'vue-router'
import { api, parseApiError } from '../api/client.js'

const route = useRoute()
const router = useRouter()

const runs = ref([])
const stateFilter = ref('')
const planVersionId = ref('')
const loading = ref(false)
const loadingMore = ref(false)
const error = ref('')
const unavailable = ref(false)
const total = ref(0)
const nextCursor = ref(null)

const columns = [
  { title: '运行标识', key: 'identity', dataIndex: 'run_identity', width: 160 },
  { title: '状态', key: 'state', dataIndex: 'state', width: 100 },
  { title: '计数', key: 'counters', width: 260 },
  { title: '错误类型', key: 'error', dataIndex: 'error_category', width: 120 },
  { title: '下次可运行', key: 'next_eligible', dataIndex: 'next_eligible_at', width: 160 },
  { title: '创建时间', key: 'created', dataIndex: 'created_at', width: 160 },
  { title: '操作', key: 'action', width: 160 },
]

const hasMore = computed(() => nextCursor.value !== null)

const emptyText = computed(() => {
  if (stateFilter.value) return '该状态下暂无运行记录'
  if (planVersionId.value) return '该计划版本暂无运行记录'
  return '暂无运行记录'
})

function runStateColor(state) {
  return {
    pending: 'default',
    running: 'processing',
    succeeded: 'success',
    failed: 'error',
    cancelled: 'warning',
    timeout: 'error',
  }[state] || 'default'
}

function runStateLabel(state) {
  return {
    pending: '待运行',
    running: '运行中',
    succeeded: '成功',
    failed: '失败',
    cancelled: '已取消',
    timeout: '超时',
  }[state] || state
}

function formatDate(date) {
  if (!date) return '-'
  return new Date(date).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function clearPlanFilter() {
  planVersionId.value = ''
  router.replace({ name: 'crawl-runs' })
  resetAndFetch()
}

async function fetchRuns(cursor) {
  const isLoadMore = !!cursor
  if (isLoadMore) {
    loadingMore.value = true
  } else {
    loading.value = true
    error.value = ''
    unavailable.value = false
  }

  try {
    const params = { limit: 50 }
    if (cursor) params.cursor = cursor
    if (planVersionId.value) params.plan_version_id = planVersionId.value

    const data = await api.listCrawlRuns(params)

    if (isLoadMore) {
      runs.value = [...runs.value, ...(data.items || [])]
    } else {
      runs.value = data.items || []
    }
    total.value = data.total || 0
    nextCursor.value = data.next_cursor ?? null
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else {
      error.value = info.message || '加载运行列表失败，请稍后重试。'
    }
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function resetAndFetch() {
  nextCursor.value = null
  fetchRuns(null)
}

function loadMore() {
  if (nextCursor.value) {
    fetchRuns(nextCursor.value)
  }
}

onMounted(() => {
  // Read plan_version_id from query if present
  const qid = route.query.plan_version_id
  if (qid) planVersionId.value = String(qid)
  fetchRuns(null)
})
</script>

<style scoped>
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #667085;
}
.load-more {
  text-align: center;
  padding: 16px 0;
}
.counter-label {
  font-size: 11px;
  color: #667085;
  margin-left: 2px;
}
.filter-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: #667085;
}
</style>
