<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>运行详情</h1>
        <p>查看单次采集运行的状态、计数和错误信息。</p>
      </div>
      <a-space>
        <a-tag :color="runStateColor">{{ runStateLabel }}</a-tag>
        <router-link :to="{ name: 'crawl-runs' }">
          <a-button size="small">返回列表</a-button>
        </router-link>
      </a-space>
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

    <!-- Loading -->
    <div v-if="initialLoading" class="page-loading">
      <a-spin size="large" />
      <span>正在加载运行详情…</span>
    </div>

    <!-- Unavailable + nothing loaded -->
    <a-result
      v-else-if="unavailable && !run"
      status="warning"
      title="服务暂不可用"
      sub-title="采集服务依赖未就绪，无法加载运行详情。"
    >
      <template #extra>
        <a-button type="primary" :loading="loading" @click="loadRun">重试</a-button>
      </template>
    </a-result>

    <!-- Not found -->
    <a-result
      v-else-if="notFound"
      status="404"
      title="运行记录不存在"
      sub-title="该运行记录可能已被删除或不属于当前用户。"
    >
      <template #extra>
        <router-link :to="{ name: 'crawl-runs' }">
          <a-button type="primary">返回运行列表</a-button>
        </router-link>
      </template>
    </a-result>

    <template v-else-if="run">
      <!-- Run overview -->
      <a-row :gutter="16">
        <a-col :xs="24" :lg="12">
          <a-card class="detail-card" title="运行信息">
            <a-descriptions :column="1" size="small" bordered>
              <a-descriptions-item label="运行标识">
                <span class="mono">{{ run.run_identity }}</span>
              </a-descriptions-item>
              <a-descriptions-item label="状态">
                <a-tag :color="runStateColor">{{ runStateLabel }}</a-tag>
              </a-descriptions-item>
              <a-descriptions-item label="计划版本">
                <router-link :to="{ name: 'crawl-runs', query: { plan_version_id: run.plan_version_id } }">
                  <span class="mono">{{ run.plan_version_id.slice(0, 8) }}…</span>
                </router-link>
              </a-descriptions-item>
              <a-descriptions-item label="来源数">{{ run.source_set?.length || 0 }}</a-descriptions-item>
              <a-descriptions-item label="创建时间">
                <span v-if="run.created_at">{{ formatDateTime(run.created_at) }}</span>
                <span v-else class="muted">-</span>
              </a-descriptions-item>
              <a-descriptions-item label="开始时间">
                <span v-if="run.started_at">{{ formatDateTime(run.started_at) }}</span>
                <span v-else class="muted">-</span>
              </a-descriptions-item>
              <a-descriptions-item label="结束时间">
                <span v-if="run.ended_at">{{ formatDateTime(run.ended_at) }}</span>
                <span v-else class="muted">-</span>
              </a-descriptions-item>
            </a-descriptions>
          </a-card>
        </a-col>

        <a-col :xs="24" :lg="12">
          <a-card class="detail-card" title="运行结果">
            <!-- Counter cards -->
            <a-row :gutter="[12, 12]">
              <a-col :span="12">
                <a-card size="small" class="counter-card">
                  <a-statistic title="发现" :value="run.counters?.discovered || 0" :value-style="{ color: '#1890ff' }" />
                </a-card>
              </a-col>
              <a-col :span="12">
                <a-card size="small" class="counter-card">
                  <a-statistic title="更新" :value="run.counters?.updated || 0" :value-style="{ color: '#13c2c2' }" />
                </a-card>
              </a-col>
              <a-col :span="12">
                <a-card size="small" class="counter-card">
                  <a-statistic title="关闭" :value="run.counters?.closed || 0" />
                </a-card>
              </a-col>
              <a-col :span="12">
                <a-card size="small" class="counter-card">
                  <a-statistic title="失败" :value="run.counters?.failed || 0" :value-style="{ color: run.counters?.failed > 0 ? '#ff4d4f' : undefined }" />
                </a-card>
              </a-col>
            </a-row>

            <!-- Error category -->
            <div v-if="run.error_category" style="margin-top: 16px">
              <a-alert type="error" show-icon>
                <template #message>
                  <span>错误类型: <a-tag color="red">{{ run.error_category }}</a-tag></span>
                </template>
              </a-alert>
            </div>

            <!-- Backoff / next eligible -->
            <div v-if="run.next_eligible_at" style="margin-top: 16px">
              <a-alert type="info" show-icon>
                <template #message>
                  <span>下次可运行时间: <strong>{{ formatDateTime(run.next_eligible_at) }}</strong></span>
                </template>
                <template #description>
                  <span>系统退避策略要求在此时间之后才能再次运行。</span>
                </template>
              </a-alert>
            </div>
          </a-card>
        </a-col>
      </a-row>

      <!-- Per-run limits -->
      <a-card class="detail-card" title="运行限制" style="margin-top: 16px">
        <a-descriptions :column="3" size="small" bordered>
          <a-descriptions-item label="每来源最大投递数">
            <span v-if="run.limits?.max_postings_per_source != null">{{ run.limits.max_postings_per_source }}</span>
            <span v-else class="muted">默认</span>
          </a-descriptions-item>
          <a-descriptions-item label="最大来源数">
            <span v-if="run.limits?.max_sources != null">{{ run.limits.max_sources }}</span>
            <span v-else class="muted">默认</span>
          </a-descriptions-item>
          <a-descriptions-item label="超时秒数">
            <span v-if="run.limits?.timeout_seconds != null">{{ run.limits.timeout_seconds }}s</span>
            <span v-else class="muted">默认</span>
          </a-descriptions-item>
        </a-descriptions>
      </a-card>

      <!-- Source set -->
      <a-card class="detail-card" title="来源集合" style="margin-top: 16px">
        <div v-if="run.source_set?.length" class="source-set">
          <a-tag v-for="sid in run.source_set" :key="sid" class="mono" style="margin-bottom: 4px">
            {{ sid.slice(0, 8) }}…
          </a-tag>
        </div>
        <a-empty v-else description="无来源" />
      </a-card>

      <!-- Link to resulting inbox items -->
      <a-card class="detail-card" title="关联职位" style="margin-top: 16px">
        <a-space>
          <router-link :to="{ name: 'jobs', query: { crawl_run_id: run.id } }">
            <a-button type="primary">查看该运行发现的职位</a-button>
          </router-link>
          <router-link :to="{ name: 'jobs', query: { source_run_id: run.id } }">
            <a-button>按来源运行筛选</a-button>
          </router-link>
        </a-space>
        <p class="muted" style="margin-top: 8px; font-size: 12px">
          跳转到职位列表并按该运行 ID 筛选，查看本次运行发现的职位。
        </p>
      </a-card>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, parseApiError } from '../api/client.js'

const route = useRoute()

const run = ref(null)
const loading = ref(false)
const initialLoading = ref(true)
const error = ref('')
const unavailable = ref(false)
const notFound = ref(false)

const runStateColor = computed(() => {
  const s = run.value?.state
  return {
    pending: 'default',
    running: 'processing',
    succeeded: 'success',
    failed: 'error',
    cancelled: 'warning',
    timeout: 'error',
  }[s] || 'default'
})

const runStateLabel = computed(() => {
  const s = run.value?.state
  return {
    pending: '待运行',
    running: '运行中',
    succeeded: '成功',
    failed: '失败',
    cancelled: '已取消',
    timeout: '超时',
  }[s] || s || '未知'
})

function formatDateTime(date) {
  if (!date) return '-'
  return new Date(date).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

async function loadRun() {
  loading.value = true
  error.value = ''
  unavailable.value = false
  notFound.value = false
  try {
    const data = await api.getCrawlRun(route.params.id)
    run.value = data
    unavailable.value = false
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else if (info.status === 404) {
      notFound.value = true
    } else {
      error.value = info.message || '加载运行详情失败。'
    }
  } finally {
    loading.value = false
    initialLoading.value = false
  }
}

onMounted(loadRun)
</script>

<style scoped>
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #667085;
}
.counter-card {
  text-align: center;
}
.source-set {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
</style>
