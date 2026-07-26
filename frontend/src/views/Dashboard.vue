<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>工作台总览</h1>
        <p>求职管线概览与数据洞察</p>
      </div>
      <a-button :loading="loading" @click="fetchDashboard">
        <template #icon><ReloadOutlined /></template>
        刷新
      </a-button>
    </div>

    <a-alert v-if="error" type="error" show-icon :message="error" closable @close="error = ''" />

    <!-- Stat cards -->
    <a-row :gutter="[16, 16]" class="dashboard-grid">
      <a-col :xs="24" :sm="12" :lg="6">
        <a-card class="stat-card" hoverable @click="$router.push('/jobs')">
          <a-statistic title="职位总数" :value="metrics.jobs">
            <template #prefix><InboxOutlined /></template>
          </a-statistic>
        </a-card>
      </a-col>
      <a-col :xs="24" :sm="12" :lg="6">
        <a-card class="stat-card" hoverable @click="$router.push('/jobs')">
          <a-statistic title="活跃职位" :value="metrics.activeJobs">
            <template #prefix><ThunderboltOutlined /></template>
          </a-statistic>
        </a-card>
      </a-col>
      <a-col :xs="24" :sm="12" :lg="6">
        <a-card class="stat-card" hoverable @click="$router.push('/applications')">
          <a-statistic title="投递总数" :value="metrics.applications">
            <template #prefix><FileTextOutlined /></template>
          </a-statistic>
        </a-card>
      </a-col>
      <a-col :xs="24" :sm="12" :lg="6">
        <a-card class="stat-card" hoverable @click="$router.push('/applications')">
          <a-statistic title="已投递" :value="metrics.submitted">
            <template #prefix><CheckCircleOutlined /></template>
          </a-statistic>
        </a-card>
      </a-col>
    </a-row>

    <!-- Charts row 1 -->
    <a-row :gutter="[16, 16]" class="dashboard-grid">
      <a-col :xs="24" :lg="12">
        <a-card class="chart-card">
          <template #title>
            <div class="card-title-row">
              <h3>职位状态分布</h3>
              <a-tag color="blue" class="chart-tag" @click="$router.push('/jobs')">查看职位</a-tag>
            </div>
          </template>
          <div class="chart-wrapper">
            <a-spin v-if="loading" class="chart-spin" tip="加载中..." />
            <div v-else-if="chartError.status" class="chart-error">
              <a-result status="warning" title="图表加载失败" sub-title="请稍后重试">
                <template #extra>
                  <a-button size="small" @click="renderStatusChart">重试</a-button>
                </template>
              </a-result>
            </div>
            <a-empty v-else-if="!chartData.statusDist.length" description="暂无职位数据" />
            <div v-show="chartData.statusDist.length && !loading && !chartError.status" ref="statusChartEl" class="chart-container" />
          </div>
        </a-card>
      </a-col>

      <a-col :xs="24" :lg="12">
        <a-card class="chart-card">
          <template #title>
            <div class="card-title-row">
              <h3>职位新增趋势</h3>
              <a-tag color="cyan" class="chart-tag">近30天</a-tag>
            </div>
          </template>
          <div class="chart-wrapper">
            <a-spin v-if="loading" class="chart-spin" tip="加载中..." />
            <div v-else-if="chartError.trend" class="chart-error">
              <a-result status="warning" title="图表加载失败" sub-title="请稍后重试">
                <template #extra>
                  <a-button size="small" @click="renderTrendChart">重试</a-button>
                </template>
              </a-result>
            </div>
            <a-empty v-else-if="!chartData.trend.length" description="暂无趋势数据" />
            <div v-show="chartData.trend.length && !loading && !chartError.trend" ref="trendChartEl" class="chart-container" />
          </div>
        </a-card>
      </a-col>
    </a-row>

    <!-- Charts row 2 -->
    <a-row :gutter="[16, 16]" class="dashboard-grid">
      <a-col :xs="24" :lg="12">
        <a-card class="chart-card">
          <template #title>
            <div class="card-title-row">
              <h3>投递状态漏斗</h3>
              <a-tag color="purple" class="chart-tag" @click="$router.push('/applications')">查看投递</a-tag>
            </div>
          </template>
          <div class="chart-wrapper">
            <a-spin v-if="loading" class="chart-spin" tip="加载中..." />
            <div v-else-if="chartError.funnel" class="chart-error">
              <a-result status="warning" title="图表加载失败" sub-title="请稍后重试">
                <template #extra>
                  <a-button size="small" @click="renderFunnelChart">重试</a-button>
                </template>
              </a-result>
            </div>
            <a-empty v-else-if="!chartData.funnel.length" description="暂无投递数据" />
            <div v-show="chartData.funnel.length && !loading && !chartError.funnel" ref="funnelChartEl" class="chart-container" />
          </div>
        </a-card>
      </a-col>

      <a-col :xs="24" :lg="12">
        <a-card class="chart-card">
          <template #title>
            <div class="card-title-row">
              <h3>近期投递时间线</h3>
              <a-tag color="green" class="chart-tag" @click="$router.push('/applications')">查看投递</a-tag>
            </div>
          </template>
          <div class="chart-wrapper">
            <a-spin v-if="loading" class="chart-spin" tip="加载中..." />
            <div v-else-if="chartError.timeline" class="chart-error">
              <a-result status="warning" title="图表加载失败" sub-title="请稍后重试">
                <template #extra>
                  <a-button size="small" @click="renderTimelineChart">重试</a-button>
                </template>
              </a-result>
            </div>
            <a-empty v-else-if="!chartData.timeline.length" description="暂无投递记录" />
            <div v-show="chartData.timeline.length && !loading && !chartError.timeline" ref="timelineChartEl" class="chart-container" />
          </div>
        </a-card>
      </a-col>
    </a-row>

    <!-- Recent applications -->
    <a-row :gutter="[16, 16]" class="dashboard-grid">
      <a-col :xs="24">
        <a-card class="chart-card" title="最近投递">
          <a-list v-if="recentApplications.length" :data-source="recentApplications" size="small">
            <template #renderItem="{ item }">
              <a-list-item>
                <a-list-item-meta>
                  <template #title>
                    <router-link :to="`/jobs/${item.canonical_job_id}`" class="table-link">
                      {{ item.canonical_job_id.slice(0, 8) }}...
                    </router-link>
                  </template>
                  <template #description>{{ formatDate(item.submitted_at) }}</template>
                </a-list-item-meta>
                <a-tag :color="applicationColor(item.state)">{{ stateLabel(item.state) }}</a-tag>
              </a-list-item>
            </template>
          </a-list>
          <a-empty v-else description="暂无投递记录" />
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  CheckCircleOutlined,
  FileTextOutlined,
  InboxOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import { api } from '../api/client.js'

const router = useRouter()

const jobs = ref([])
const applications = ref([])
const loading = ref(false)
const error = ref('')

// Chart refs
const statusChartEl = ref(null)
const trendChartEl = ref(null)
const funnelChartEl = ref(null)
const timelineChartEl = ref(null)

// Chart instances (one per chart)
const charts = { status: null, trend: null, funnel: null, timeline: null }

// Per-chart error flags
const chartError = reactive({ status: false, trend: false, funnel: false, timeline: false })

// ---------- Computed ----------

const metrics = computed(() => ({
  jobs: jobs.value.length,
  activeJobs: jobs.value.filter((j) => j.aggregate_state === 'active').length,
  applications: applications.value.length,
  submitted: applications.value.filter((a) => a.state === 'submitted').length,
}))

const chartData = computed(() => {
  // 1. Status distribution
  const stateMap = new Map()
  for (const job of jobs.value) {
    const s = job.aggregate_state || 'unknown'
    stateMap.set(s, (stateMap.get(s) || 0) + 1)
  }
  const statusDist = Array.from(stateMap, ([state, count]) => ({ state, count }))

  // 2. Trend (jobs added per day, last 30 days)
  const now = new Date()
  const dayMap = new Map()
  for (let i = 29; i >= 0; i--) {
    const d = new Date(now)
    d.setDate(d.getDate() - i)
    dayMap.set(fmtDay(d), 0)
  }
  for (const job of jobs.value) {
    const raw = job.created_at || job.crawled_at
    if (!raw) continue
    const day = fmtDay(new Date(raw))
    if (dayMap.has(day)) dayMap.set(day, dayMap.get(day) + 1)
  }
  const trend = Array.from(dayMap, ([date, count]) => ({ date, count }))

  // 3. Funnel (application states)
  const funnelOrder = ['favorited', 'preparing', 'submitted', 'interviewing', 'offered', 'rejected', 'ignored']
  const funnelMap = new Map()
  for (const a of applications.value) {
    const s = a.state || 'unknown'
    funnelMap.set(s, (funnelMap.get(s) || 0) + 1)
  }
  const funnel = funnelOrder
    .filter((s) => funnelMap.has(s))
    .map((s) => ({ state: stateLabel(s), count: funnelMap.get(s) }))
  // append any states not in the predefined order
  for (const [s, c] of funnelMap) {
    if (!funnelOrder.includes(s)) funnel.push({ state: stateLabel(s), count: c })
  }

  // 4. Timeline (recent applications by date)
  const sorted = [...applications.value]
    .filter((a) => a.submitted_at)
    .sort((a, b) => new Date(a.submitted_at) - new Date(b.submitted_at))
    .slice(-20)
  const timeline = sorted.map((a) => ({
    date: fmtDay(new Date(a.submitted_at)),
    state: stateLabel(a.state),
    id: a.id || a.canonical_job_id,
  }))

  return { statusDist, trend, funnel, timeline }
})

const recentApplications = computed(() =>
  [...applications.value]
    .sort((a, b) => new Date(b.submitted_at || 0) - new Date(a.submitted_at || 0))
    .slice(0, 8),
)

// ---------- Helpers ----------

function fmtDay(d) {
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

function formatDate(date) {
  if (!date) return '未记录'
  return new Date(date).toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
}

const STATE_LABELS = {
  favorited: '已收藏',
  preparing: '准备中',
  submitted: '已投递',
  interviewing: '面试中',
  offered: '已录用',
  rejected: '已拒绝',
  ignored: '已忽略',
  active: '活跃',
  closed: '已关闭',
  expired: '已过期',
  unknown: '未知',
}

function stateLabel(state) {
  return STATE_LABELS[state] || state
}

function applicationColor(state) {
  return {
    favorited: 'gold',
    preparing: 'blue',
    submitted: 'green',
    interviewing: 'purple',
    offered: 'cyan',
    rejected: 'red',
    ignored: 'default',
  }[state] || 'default'
}

async function loadG2() {
  const mod = await import('@antv/g2')
  return mod.Chart
}

// ---------- Chart renderers ----------

async function renderStatusChart() {
  chartError.status = false
  const data = chartData.value.statusDist
  if (!statusChartEl.value || !data.length) return
  try {
    const Chart = await loadG2()
    charts.status?.destroy()
    const c = new Chart({ container: statusChartEl.value, autoFit: true, height: 280 })
    c.interval()
      .data(data)
      .encode('x', 'state')
      .encode('y', 'count')
      .encode('color', 'state')
      .scale('y', { nice: true })
      .axis('x', { title: false })
      .axis('y', { title: false })
      .legend('color', { position: 'bottom' })
      .tooltip((d) => ({ name: stateLabel(d.state), value: d.count }))
      .interaction('elementHighlight')
      .style('cursor', 'pointer')
    c.on('element:click', (evt) => {
      const state = evt.data?.data?.state
      if (state && state !== 'unknown') router.push({ path: '/jobs', query: { state } })
    })
    await c.render()
    charts.status = c
  } catch (e) {
    console.error('[Dashboard] status chart error:', e)
    chartError.status = true
  }
}

async function renderTrendChart() {
  chartError.trend = false
  const data = chartData.value.trend
  if (!trendChartEl.value || !data.length) return
  try {
    const Chart = await loadG2()
    charts.trend?.destroy()
    const c = new Chart({ container: trendChartEl.value, autoFit: true, height: 280 })
    c.area()
      .data(data)
      .encode('x', 'date')
      .encode('y', 'count')
      .encode('shape', 'smooth')
      .scale('y', { nice: true })
      .axis('x', { title: false, labelAutoRotate: true })
      .axis('y', { title: false })
      .style('fill', 'linear-gradient(-90deg, #69b1ff 0%, #f0f5ff 100%)')
      .style('stroke', '#4096ff')
      .tooltip((d) => ({ name: '新增职位', value: d.count }))
    c.line()
      .data(data)
      .encode('x', 'date')
      .encode('y', 'count')
      .encode('shape', 'smooth')
      .style('stroke', '#4096ff')
      .style('lineWidth', 2)
      .tooltip(false)
    c.point()
      .data(data.filter((d) => d.count > 0))
      .encode('x', 'date')
      .encode('y', 'count')
      .encode('shape', 'point')
      .style('fill', '#fff')
      .style('stroke', '#4096ff')
      .style('lineWidth', 2)
      .style('r', 3)
      .tooltip((d) => ({ name: '新增职位', value: d.count }))
    c.interaction('tooltip')
    await c.render()
    charts.trend = c
  } catch (e) {
    console.error('[Dashboard] trend chart error:', e)
    chartError.trend = true
  }
}

async function renderFunnelChart() {
  chartError.funnel = false
  const data = chartData.value.funnel
  if (!funnelChartEl.value || !data.length) return
  try {
    const Chart = await loadG2()
    charts.funnel?.destroy()
    const c = new Chart({ container: funnelChartEl.value, autoFit: true, height: 280 })
    c.interval()
      .data(data)
      .encode('x', 'state')
      .encode('y', 'count')
      .encode('color', 'state')
      .scale('y', { nice: true })
      .axis('x', { title: false })
      .axis('y', { title: false })
      .legend('color', { position: 'bottom' })
      .coordinate('transpose')
      .tooltip((d) => ({ name: d.state, value: d.count }))
      .interaction('elementHighlight')
      .style('cursor', 'pointer')
    c.on('element:click', (evt) => {
      router.push('/applications')
    })
    await c.render()
    charts.funnel = c
  } catch (e) {
    console.error('[Dashboard] funnel chart error:', e)
    chartError.funnel = true
  }
}

async function renderTimelineChart() {
  chartError.timeline = false
  const data = chartData.value.timeline
  if (!timelineChartEl.value || !data.length) return
  try {
    const Chart = await loadG2()
    charts.timeline?.destroy()
    const c = new Chart({ container: timelineChartEl.value, autoFit: true, height: 280 })
    c.point()
      .data(data)
      .encode('x', 'date')
      .encode('y', 'state')
      .encode('color', 'state')
      .encode('shape', 'point')
      .scale('color', { range: ['#faad14', '#1677ff', '#52c41a', '#722ed1', '#13c2c2', '#f5222d', '#8c8c8c'] })
      .axis('x', { title: false, labelAutoRotate: true })
      .axis('y', { title: false })
      .legend('color', { position: 'bottom' })
      .style('r', 6)
      .style('lineWidth', 2)
      .style('stroke', '#fff')
      .tooltip((d) => ({ name: d.state, value: d.date }))
      .interaction('elementHighlight')
    c.on('element:click', (evt) => {
      router.push('/applications')
    })
    await c.render()
    charts.timeline = c
  } catch (e) {
    console.error('[Dashboard] timeline chart error:', e)
    chartError.timeline = true
  }
}

// ---------- Data fetching ----------

async function fetchDashboard() {
  loading.value = true
  error.value = ''
  // reset chart errors
  chartError.status = false
  chartError.trend = false
  chartError.funnel = false
  chartError.timeline = false
  try {
    const [jobData, appData] = await Promise.all([
      api.listJobs({ limit: 200 }),
      api.listApplications({ limit: 200 }),
    ])
    jobs.value = jobData.items || []
    applications.value = appData.items || []
    // render charts sequentially to avoid G2 container conflicts
    await nextTick()
    await renderStatusChart()
    await renderTrendChart()
    await renderFunnelChart()
    await renderTimelineChart()
  } catch (err) {
    error.value = err.message || '无法加载总览数据，请稍后重试'
  } finally {
    loading.value = false
  }
}

// ---------- Lifecycle ----------

onMounted(fetchDashboard)
onUnmounted(() => {
  Object.values(charts).forEach((c) => c?.destroy())
})
</script>

<style scoped>
.dashboard-grid {
  margin-bottom: 16px;
}

.stat-card {
  cursor: pointer;
  transition: box-shadow 0.2s, border-color 0.2s;
}

.stat-card:hover {
  border-color: #91caff;
  box-shadow: 0 2px 8px rgb(22 119 255 / 12%);
}

.chart-tag {
  cursor: pointer;
}

.chart-wrapper {
  position: relative;
  min-height: 280px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.chart-spin {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 280px;
}

.chart-error {
  width: 100%;
  min-height: 280px;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
