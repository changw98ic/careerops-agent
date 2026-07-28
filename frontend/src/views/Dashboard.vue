<template>
  <div ref="dashboardEl" class="dashboard">
    <!-- 页面头部 -->
    <header class="dashboard-header">
      <div class="header-content">
        <div class="header-left">
          <div class="page-eyebrow">工作台</div>
          <h1 class="page-title">总览</h1>
          <p class="page-subtitle">求职管线概览与数据洞察</p>
        </div>
        <div class="header-right">
          <button
            class="refresh-btn"
            :class="{ 'is-loading': loading }"
            :disabled="loading"
            aria-label="刷新数据"
            @click="fetchDashboard"
          >
            <div class="btn-outer">
              <div class="btn-inner">
                <ReloadOutlined class="btn-icon" />
                <span class="btn-text">刷新</span>
              </div>
            </div>
          </button>
        </div>
      </div>
    </header>

    <!-- 错误提示 -->
    <div v-if="error" class="error-banner" role="alert">
      <div class="error-outer">
        <div class="error-inner">
          <ExclamationCircleOutlined class="error-icon" />
          <span class="error-text">{{ error }}</span>
          <button class="error-close" aria-label="关闭错误提示" @click="error = ''">
            <CloseOutlined />
          </button>
        </div>
      </div>
    </div>

    <section v-if="!loading && !error && jobTotal === 0 && applicationTotal === 0" class="onboarding-banner">
      <div>
        <div class="onboarding-eyebrow">开始使用</div>
        <h2>先配置画像，再让管线为你工作</h2>
        <p>创建职业画像、登记一份确认后的简历，随后就可以开始爬取和审查职位。</p>
      </div>
      <div class="onboarding-actions">
        <button type="button" class="onboarding-primary" @click="$router.push('/profile')">创建职业画像</button>
        <button type="button" class="onboarding-secondary" @click="$router.push('/ai-workbench')">查看智能工作台</button>
      </div>
    </section>

    <!-- 统计卡片 - Apple 风格 Bento 布局 -->
    <section class="stats-section" data-reveal="fade-up">
      <div class="stats-bento">
        <button
          v-for="(stat, index) in stats"
          :key="stat.key"
          type="button"
          class="stat-cell"
          :class="getCellClass(index)"
          data-reveal="fade-up"
          :data-stagger="index"
          :aria-label="`${stat.label}：${stat.value}`"
          @click="$router.push(stat.route)"
        >
          <div class="stat-cell-outer">
            <div class="stat-cell-inner">
              <div class="stat-icon-wrapper" :style="{ background: stat.gradient }">
                <component :is="stat.icon" class="stat-icon" />
              </div>
              <div class="stat-content">
                <div class="stat-label">{{ stat.label }}</div>
                <div class="stat-value">{{ stat.value }}</div>
                <div class="stat-trend" v-if="stat.trend">
                  <span class="trend-value" :class="stat.trend.type">
                    {{ stat.trend.value }}
                  </span>
                  <span class="trend-label">{{ stat.trend.label }}</span>
                </div>
              </div>
            </div>
          </div>
        </button>
      </div>
    </section>

    <!-- 图表区域 - 不对称网格 -->
    <section class="charts-section" data-reveal="fade-up">
      <div class="charts-grid">
        <!-- 职位状态分布 -->
        <div class="chart-cell chart-cell-large" data-reveal="scale" data-stagger="0">
          <div class="chart-cell-outer">
            <div class="chart-cell-inner">
              <div class="chart-header">
                <div class="chart-title-group">
                  <div class="chart-eyebrow">数据洞察</div>
                  <h3 class="chart-title">职位状态分布</h3>
                </div>
                <div class="chart-actions">
                  <button class="chart-action-btn" aria-label="查看职位列表" @click="$router.push('/jobs')">
                    <span>查看职位</span>
                    <ArrowRightOutlined />
                  </button>
                </div>
              </div>
              <div class="chart-container">
                <div v-if="loading" class="chart-skeleton">
                  <div class="skeleton-shimmer"></div>
                </div>
                <div v-else-if="chartError.status" class="chart-error">
                  <div class="error-state">
                    <WarningOutlined class="error-state-icon" />
                    <p class="error-state-text">图表加载失败</p>
                    <button class="error-state-btn" @click="retryChart('status')">重试</button>
                  </div>
                </div>
                <div v-else-if="!chartData.statusDist.length" class="chart-empty">
                  <div class="empty-state">
                    <InboxOutlined class="empty-state-icon" />
                    <p class="empty-state-text">暂无职位数据</p>
                    <p class="empty-state-hint">添加职位后即可查看分布</p>
                  </div>
                </div>
                <DashboardChart
                  v-show="chartData.statusDist.length && !loading && !chartError.status"
                  type="status"
                  :data="chartData.statusDist"
                  @select="openJobsByState"
                />
              </div>
            </div>
          </div>
        </div>

        <!-- 职位新增趋势 -->
        <div class="chart-cell chart-cell-large" data-reveal="scale" data-stagger="1">
          <div class="chart-cell-outer">
            <div class="chart-cell-inner">
              <div class="chart-header">
                <div class="chart-title-group">
                  <div class="chart-eyebrow">趋势分析</div>
                  <h3 class="chart-title">职位新增趋势</h3>
                </div>
                <div class="chart-actions">
                  <span class="chart-tag">近30天</span>
                </div>
              </div>
              <div class="chart-container">
                <div v-if="loading" class="chart-skeleton">
                  <div class="skeleton-shimmer"></div>
                </div>
                <div v-else-if="chartError.trend" class="chart-error">
                  <div class="error-state">
                    <WarningOutlined class="error-state-icon" />
                    <p class="error-state-text">图表加载失败</p>
                    <button class="error-state-btn" @click="retryChart('trend')">重试</button>
                  </div>
                </div>
                <div v-else-if="!chartData.trend.length" class="chart-empty">
                  <div class="empty-state">
                    <LineChartOutlined class="empty-state-icon" />
                    <p class="empty-state-text">暂无趋势数据</p>
                    <p class="empty-state-hint">职位数据积累后显示趋势</p>
                  </div>
                </div>
                <DashboardChart
                  v-show="chartData.trend.length && !loading && !chartError.trend"
                  type="trend"
                  :data="chartData.trend"
                  @select="openJobs"
                />
              </div>
            </div>
          </div>
        </div>

        <!-- 投递状态漏斗 -->
        <div class="chart-cell chart-cell-medium" data-reveal="scale" data-stagger="2">
          <div class="chart-cell-outer">
            <div class="chart-cell-inner">
              <div class="chart-header">
                <div class="chart-title-group">
                  <div class="chart-eyebrow">投递分析</div>
                  <h3 class="chart-title">投递状态漏斗</h3>
                </div>
                <div class="chart-actions">
                  <button class="chart-action-btn" aria-label="查看投递记录" @click="$router.push('/applications')">
                    <span>查看投递</span>
                    <ArrowRightOutlined />
                  </button>
                </div>
              </div>
              <div class="chart-container">
                <div v-if="loading" class="chart-skeleton">
                  <div class="skeleton-shimmer"></div>
                </div>
                <div v-else-if="chartError.funnel" class="chart-error">
                  <div class="error-state">
                    <WarningOutlined class="error-state-icon" />
                    <p class="error-state-text">图表加载失败</p>
                    <button class="error-state-btn" @click="retryChart('funnel')">重试</button>
                  </div>
                </div>
                <div v-else-if="!chartData.funnel.length" class="chart-empty">
                  <div class="empty-state">
                    <FunnelPlotOutlined class="empty-state-icon" />
                    <p class="empty-state-text">暂无投递数据</p>
                    <p class="empty-state-hint">开始投递后查看状态分布</p>
                  </div>
                </div>
                <DashboardChart
                  v-show="chartData.funnel.length && !loading && !chartError.funnel"
                  type="funnel"
                  :data="chartData.funnel"
                  @select="openApplications"
                />
              </div>
            </div>
          </div>
        </div>

        <!-- 近期投递时间线 -->
        <div class="chart-cell chart-cell-medium" data-reveal="scale" data-stagger="3">
          <div class="chart-cell-outer">
            <div class="chart-cell-inner">
              <div class="chart-header">
                <div class="chart-title-group">
                  <div class="chart-eyebrow">时间线</div>
                  <h3 class="chart-title">近期投递</h3>
                </div>
                <div class="chart-actions">
                  <button class="chart-action-btn" aria-label="查看全部投递" @click="$router.push('/applications')">
                    <span>查看全部</span>
                    <ArrowRightOutlined />
                  </button>
                </div>
              </div>
              <div class="chart-container">
                <div v-if="loading" class="chart-skeleton">
                  <div class="skeleton-shimmer"></div>
                </div>
                <div v-else-if="chartError.timeline" class="chart-error">
                  <div class="error-state">
                    <WarningOutlined class="error-state-icon" />
                    <p class="error-state-text">图表加载失败</p>
                    <button class="error-state-btn" @click="retryChart('timeline')">重试</button>
                  </div>
                </div>
                <div v-else-if="!chartData.timeline.length" class="chart-empty">
                  <div class="empty-state">
                    <ClockCircleOutlined class="empty-state-icon" />
                    <p class="empty-state-text">暂无投递记录</p>
                    <p class="empty-state-hint">投递后即可查看时间线</p>
                  </div>
                </div>
                <DashboardChart
                  v-show="chartData.timeline.length && !loading && !chartError.timeline"
                  type="timeline"
                  :data="chartData.timeline"
                  @select="openApplications"
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- 最近投递列表 -->
    <section class="recent-section" data-reveal="fade-up">
      <div class="recent-card">
        <div class="recent-card-outer">
          <div class="recent-card-inner">
            <div class="recent-header">
              <div class="chart-title-group">
                <div class="chart-eyebrow">快速访问</div>
                <h3 class="chart-title">最近投递</h3>
              </div>
              <button class="view-all-btn" aria-label="查看全部投递记录" @click="$router.push('/applications')">
                <span>查看全部</span>
                <ArrowRightOutlined />
              </button>
            </div>

            <div class="recent-list" v-if="recentApplications.length">
              <button
                v-for="(app, index) in recentApplications"
                :key="app.id || index"
                type="button"
                class="recent-item"
                data-reveal="fade-up"
                :data-stagger="index"
                @click="$router.push(`/jobs/${app.canonical_job_id}`)"
              >
                <div class="recent-item-content">
                  <div class="recent-item-left">
                    <div class="recent-item-id">{{ app.canonical_job_id?.slice(0, 8) || '未知职位' }}...</div>
                    <div class="recent-item-date">{{ formatDate(app.submitted_at) }}</div>
                  </div>
                  <div class="recent-item-right">
                    <span class="recent-item-tag" :class="`tag-${app.state}`">
                      {{ stateLabel(app.state) }}
                    </span>
                    <ArrowRightOutlined class="recent-item-arrow" />
                  </div>
                </div>
              </button>
            </div>

            <div v-else class="recent-empty">
              <div class="empty-state">
                <FileTextOutlined class="empty-state-icon" />
                <p class="empty-state-text">暂无投递记录</p>
                <p class="empty-state-hint">开始投递后即可查看</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowRightOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseOutlined,
  ExclamationCircleOutlined,
  FileTextOutlined,
  FunnelPlotOutlined,
  InboxOutlined,
  LineChartOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import { api, formatApiError } from '../api/client.js'
import DashboardChart from '../components/DashboardChart.vue'

const router = useRouter()
const dashboardEl = ref(null)

const jobs = ref([])
const applications = ref([])
const jobTotal = ref(0)
const applicationTotal = ref(0)
const loading = ref(false)
const error = ref('')

// Per-chart error flags
const chartError = reactive({ status: false, trend: false, funnel: false, timeline: false })

// ---------- 统计数据配置 ----------

const stats = computed(() => [
  {
    key: 'jobs',
    label: '职位总数',
    value: jobTotal.value,
    icon: InboxOutlined,
    route: '/jobs',
    gradient: 'var(--gradient-orange)',
  },
  {
    key: 'active',
    label: '活跃职位',
    value: jobs.value.filter((j) => j.aggregate_state === 'active').length,
    icon: ThunderboltOutlined,
    route: '/jobs',
    gradient: 'var(--gradient-amber)',
  },
  {
    key: 'apps',
    label: '投递总数',
    value: applicationTotal.value,
    icon: FileTextOutlined,
    route: '/applications',
    gradient: 'var(--gradient-green)',
  },
  {
    key: 'submitted',
    label: '已投递',
    value: applications.value.filter((a) => a.state === 'submitted').length,
    icon: CheckCircleOutlined,
    route: '/applications',
    gradient: 'var(--gradient-blue)',
  },
])

// Keep a compact metrics surface for callers/tests that consume the
// dashboard as a data view, while the template renders the richer stat cards.
const metrics = computed(() => ({
  jobs: jobTotal.value,
  activeJobs: jobs.value.filter((job) => job.aggregate_state === 'active').length,
  applications: applicationTotal.value,
  submitted: applications.value.filter((application) => application.state === 'submitted').length,
}))

// ---------- Computed ----------

const chartData = computed(() => {
  // 1. Status distribution
  const stateMap = new Map()
  for (const job of jobs.value) {
    const s = job.aggregate_state || 'unknown'
    stateMap.set(s, (stateMap.get(s) || 0) + 1)
  }
  const statusDist = Array.from(stateMap, ([state, count]) => ({ state, count }))

  // 2. Trend (jobs first seen per day, last 30 days). The canonical job
  // response stores this timestamp on its postings, not at the top level.
  const now = new Date()
  const dayMap = new Map()
  for (let i = 29; i >= 0; i--) {
    const d = new Date(now)
    d.setDate(d.getDate() - i)
    dayMap.set(fmtDay(d), 0)
  }
  for (const job of jobs.value) {
    const raw = firstJobSeenAt(job)
    if (!raw) continue
    const date = new Date(raw)
    if (Number.isNaN(date.getTime())) continue
    const day = fmtDay(date)
    if (dayMap.has(day)) dayMap.set(day, dayMap.get(day) + 1)
  }
  const trend = jobs.value.some((job) => firstJobSeenAt(job))
    ? Array.from(dayMap, ([date, count]) => ({ date, count }))
    : []

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
    .slice(0, 6),
)

// ---------- Helpers ----------

function getCellClass(index) {
  return index === 0 ? 'stat-cell-wide' : ''
}

function fmtDay(d) {
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

function firstJobSeenAt(job) {
  const postings = Array.isArray(job.postings) ? job.postings : []
  const candidates = [
    job.first_seen_at,
    job.created_at,
    job.crawled_at,
    ...postings.map((posting) => posting.first_seen_at),
  ].filter(Boolean)
  return candidates.reduce((earliest, candidate) => {
    const candidateTime = Date.parse(candidate)
    if (Number.isNaN(candidateTime)) return earliest
    if (!earliest) return candidate
    return candidateTime < Date.parse(earliest) ? candidate : earliest
  }, null)
}

function formatDate(date) {
  if (!date) return '未记录'
  const parsed = new Date(date)
  if (Number.isNaN(parsed.getTime())) return '未记录'
  return parsed.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
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
    submitted: 'green',
    rejected: 'red',
    interviewing: 'blue',
    offered: 'purple',
    preparing: 'gold',
  }[state] || 'default'
}

function openJobsByState(item) {
  if (item.state && item.state !== 'unknown') {
    router.push({ path: '/jobs', query: { state: item.state } })
    return
  }
  openJobs()
}

function openJobs() {
  router.push('/jobs')
}

function openApplications() {
  router.push('/applications')
}

function retryChart(type) {
  chartError[type] = false
}

// ---------- Data fetching ----------

async function loadAllPages(listPage) {
  const items = []
  const seenCursors = new Set()
  let cursor = null
  let total = null

  for (let page = 0; page < 10000; page += 1) {
    const params = { limit: 200 }
    if (cursor) params.cursor = cursor
    const response = await listPage(params)
    const pageItems = Array.isArray(response?.items) ? response.items : []
    items.push(...pageItems)

    const responseTotal = Number(response?.total)
    if (Number.isFinite(responseTotal) && responseTotal >= 0) total = responseTotal

    const nextCursor = response?.next_cursor || null
    if (!nextCursor) {
      if (response?.has_more) {
        throw new Error('分页响应缺少 next_cursor，无法完整加载数据')
      }
      return { items, total: total ?? items.length }
    }
    if (seenCursors.has(nextCursor)) {
      throw new Error('分页响应返回重复游标，已停止加载以避免死循环')
    }
    seenCursors.add(nextCursor)
    cursor = nextCursor
  }

  throw new Error('分页页数超过安全上限，已停止加载')
}

async function fetchDashboard() {
  const requestId = ++dashboardRequestId
  loading.value = true
  error.value = ''
  chartError.status = false
  chartError.trend = false
  chartError.funnel = false
  chartError.timeline = false
  try {
    const [jobData, appData] = await Promise.all([
      loadAllPages(api.listJobs),
      loadAllPages(api.listApplications),
    ])
    if (requestId !== dashboardRequestId) return
    jobs.value = jobData.items
    applications.value = appData.items
    jobTotal.value = jobData.total
    applicationTotal.value = appData.total
    await nextTick()
    observeNewReveals()
  } catch (err) {
    if (requestId !== dashboardRequestId) return
    error.value = formatApiError(err, '无法加载总览数据，请稍后重试。')
  } finally {
    if (requestId === dashboardRequestId) loading.value = false
  }
}

// ---------- Scroll-reveal animation ----------

const prefersReducedMotion = ref(false)
let revealObserver = null
let motionQuery = null
let motionChangeHandler = null
let dashboardRequestId = 0

function revealElement(el) {
  const stagger = el.dataset.stagger
  if (stagger !== undefined) {
    el.style.transitionDelay = `${Number(stagger) * 80}ms`
  }
  el.classList.add('is-revealed')
}

function revealElements() {
  const root = dashboardEl.value || document
  root.querySelectorAll('[data-reveal]').forEach((el) => {
    revealElement(el)
  })
}

function revealVisibleElements() {
  const root = dashboardEl.value || document
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0
  root.querySelectorAll('[data-reveal]:not(.is-revealed)').forEach((el) => {
    const rect = el.getBoundingClientRect()
    if (rect.top <= viewportHeight && rect.bottom >= 0) {
      revealElement(el)
      revealObserver?.unobserve(el)
    }
  })
}

function setupRevealObserver() {
  // Respect user preference
  motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  prefersReducedMotion.value = motionQuery.matches
  motionChangeHandler = (e) => {
    prefersReducedMotion.value = e.matches
    if (e.matches && revealObserver) {
      // Immediately show all hidden elements
      revealElements()
    }
  }
  motionQuery.addEventListener('change', motionChangeHandler)

  if (motionQuery.matches) {
    // No animation — mark everything revealed immediately
    revealElements()
    return
  }

  revealObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const el = entry.target
          revealElement(el)
          revealObserver.unobserve(el)
        }
      })
    },
    { threshold: 0.1, rootMargin: '0px 0px -40px 0px' },
  )

  dashboardEl.value?.querySelectorAll('[data-reveal]').forEach((el) => {
    revealObserver.observe(el)
  })
  // IntersectionObserver callbacks are not guaranteed to run before the
  // first paint. Reveal the initial viewport synchronously so the dashboard
  // never appears blank until the user scrolls.
  revealVisibleElements()
}

/** Observe any [data-reveal] elements not yet tracked. */
function observeNewReveals() {
  if (!revealObserver) return
  dashboardEl.value?.querySelectorAll('[data-reveal]:not(.is-revealed)').forEach((el) => {
    revealObserver.observe(el)
  })
}

// ---------- Lifecycle ----------

onMounted(async () => {
  await fetchDashboard()
  await nextTick()
  setupRevealObserver()
})
onUnmounted(() => {
  dashboardRequestId += 1
  revealObserver?.disconnect()
  if (motionQuery && motionChangeHandler) {
    motionQuery.removeEventListener('change', motionChangeHandler)
  }
  motionQuery = null
  motionChangeHandler = null
})
</script>

<style scoped>
/* ============================================
   Dashboard 组件样式
   ============================================ */

/* 使用 styles.css 中定义的全局变量 */

/* 基础布局 */
.dashboard {
  max-width: 1400px;
  margin: 0 auto;
  padding: 40px 48px;
  background: var(--surface-secondary);
  min-height: 100vh;
  font-family: 'Geist', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', sans-serif;
}

/* ============================================
   页面头部
   ============================================ */

.dashboard-header {
  margin-bottom: 48px;
}

.header-content {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.page-eyebrow {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--text-tertiary);
  margin-bottom: 8px;
}

.page-title {
  font-size: 48px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.03em;
  line-height: 1.1;
  margin: 0 0 12px 0;
}

.page-subtitle {
  font-size: 17px;
  font-weight: 500;
  color: var(--text-secondary);
  margin: 0;
}

.onboarding-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 32px;
  padding: 24px 28px;
  border: 1px solid rgba(194, 65, 12, 0.18);
  border-radius: var(--radius-xl);
  background: linear-gradient(135deg, rgba(255, 247, 237, 0.96), rgba(255, 255, 255, 0.96));
  box-shadow: var(--shadow-sm);
}

.onboarding-eyebrow {
  color: var(--accent-primary);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.onboarding-banner h2 {
  margin: 6px 0 8px;
  color: var(--text-primary);
  font-size: 22px;
  letter-spacing: -0.02em;
}

.onboarding-banner p {
  margin: 0;
  color: var(--text-secondary);
  font-size: 14px;
}

.onboarding-actions {
  display: flex;
  flex-shrink: 0;
  gap: 10px;
}

.onboarding-primary,
.onboarding-secondary {
  border-radius: var(--radius-sm);
  cursor: pointer;
  padding: 10px 14px;
  font: inherit;
  font-size: 13px;
  font-weight: 650;
  transition: transform 0.2s ease, box-shadow 0.2s ease, background 0.2s ease;
}

.onboarding-primary {
  border: 1px solid var(--accent-primary);
  background: var(--accent-primary);
  color: var(--color-inverse);
  box-shadow: 0 4px 12px rgba(194, 65, 12, 0.18);
}

.onboarding-secondary {
  border: 1px solid var(--border-light);
  background: var(--surface-primary);
  color: var(--text-primary);
}

.onboarding-primary:hover,
.onboarding-secondary:hover {
  transform: translateY(-1px);
}

.onboarding-primary:hover {
  background: var(--accent-primary-hover);
}

.onboarding-secondary:hover {
  background: var(--surface-tertiary);
}

/* 刷新按钮 */
.refresh-btn {
  position: relative;
  cursor: pointer;
  background: none;
  border: none;
  padding: 0;
  transition: transform 0.6s cubic-bezier(0.32, 0.72, 0, 1);
}

.refresh-btn:hover {
  transform: translateY(-2px);
}

.refresh-btn:active {
  transform: scale(0.98);
}

.refresh-btn:disabled {
  cursor: wait;
  transform: none;
}

.refresh-btn.is-loading .btn-inner {
  opacity: 0.7;
}

.btn-outer {
  background: rgba(0, 0, 0, 0.03);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-lg);
  padding: 6px;
}

.btn-inner {
  background: var(--surface-primary);
  border-radius: calc(var(--radius-lg) - 6px);
  padding: 12px 24px;
  display: flex;
  align-items: center;
  gap: 8px;
  box-shadow:
    inset 0 1px 1px rgba(255, 255, 255, 0.8),
    0 4px 16px rgba(0, 0, 0, 0.04);
  transition: all 0.4s cubic-bezier(0.32, 0.72, 0, 1);
}

.btn-icon {
  font-size: 16px;
  color: var(--text-secondary);
}

.btn-text {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

/* ============================================
   错误提示
   ============================================ */

.error-banner {
  margin-bottom: 32px;
}

.error-outer {
  background: rgba(255, 59, 48, 0.05);
  border: 1px solid rgba(255, 59, 48, 0.1);
  border-radius: var(--radius-lg);
  padding: 6px;
}

.error-inner {
  background: var(--surface-primary);
  border-radius: calc(var(--radius-lg) - 6px);
  padding: 16px 20px;
  display: flex;
  align-items: center;
  gap: 12px;
  box-shadow:
    inset 0 1px 1px rgba(255, 255, 255, 0.8),
    0 4px 16px rgba(255, 59, 48, 0.08);
}

.error-icon {
  font-size: 18px;
  color: var(--accent-red);
  flex-shrink: 0;
}

.error-text {
  flex: 1;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
}

.error-close {
  width: 28px;
  height: 28px;
  border-radius: var(--radius-sm);
  border: none;
  background: rgba(0, 0, 0, 0.04);
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.4s cubic-bezier(0.32, 0.72, 0, 1);
}

.error-close:hover {
  background: rgba(0, 0, 0, 0.08);
  color: var(--text-primary);
}

/* ============================================
   统计卡片 - Apple 风格 Bento
   ============================================ */

.stats-section {
  margin-bottom: 48px;
}

.stats-bento {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.stat-cell {
  width: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition-property: opacity, transform;
  transition-duration: 0.65s;
  transition-timing-function: cubic-bezier(0.32, 0.72, 0, 1);
}

.stat-cell.is-revealed:hover {
  transform: translateY(-4px);
}

.stat-cell.is-revealed:active {
  transform: scale(0.98);
}

.stat-cell-wide {
  grid-column: span 2;
}

.stat-cell-outer {
  background: var(--surface-secondary);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-xl);
  padding: 8px;
  height: 100%;
}

.stat-cell-inner {
  background: var(--surface-primary);
  border-radius: calc(var(--radius-xl) - 8px);
  padding: 28px;
  display: flex;
  align-items: flex-start;
  gap: 20px;
  height: 100%;
  box-shadow: var(--shadow-md);
}

.stat-icon-wrapper {
  width: 56px;
  height: 56px;
  border-radius: var(--radius-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.stat-icon {
  font-size: 24px;
  color: white;
}

.stat-content {
  flex: 1;
  min-width: 0;
}

.stat-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: 0.02em;
  margin-bottom: 8px;
}

.stat-value {
  font-size: 40px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.03em;
  line-height: 1;
  margin-bottom: 8px;
}

.stat-trend {
  display: flex;
  align-items: center;
  gap: 6px;
}

.trend-value {
  font-size: 13px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: var(--radius-xs);
}

.trend-value.positive {
  background: rgba(52, 199, 89, 0.1);
  color: var(--accent-green);
}

.trend-value.negative {
  background: rgba(255, 59, 48, 0.1);
  color: var(--accent-red);
}

.trend-label {
  font-size: 12px;
  color: var(--text-tertiary);
}

/* ============================================
   图表区域
   ============================================ */

.charts-section {
  margin-bottom: 48px;
}

.charts-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

.chart-cell {
  transition-property: opacity, transform;
  transition-duration: 0.7s;
  transition-timing-function: cubic-bezier(0.32, 0.72, 0, 1);
}

.chart-cell.is-revealed:hover {
  transform: translateY(-2px);
}

.chart-cell-outer {
  background: var(--surface-secondary);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-xl);
  padding: 10px;
  height: 100%;
}

.chart-cell-inner {
  background: var(--surface-primary);
  border-radius: calc(var(--radius-xl) - 10px);
  padding: 32px;
  height: 100%;
  box-shadow: var(--shadow-md);
}

.chart-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 28px;
}

.chart-eyebrow {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--text-tertiary);
  margin-bottom: 8px;
}

.chart-title {
  font-size: 20px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.02em;
  margin: 0;
}

.chart-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.chart-action-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  border: none;
  background: rgba(0, 0, 0, 0.04);
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.4s cubic-bezier(0.32, 0.72, 0, 1);
}

.chart-action-btn:hover {
  background: rgba(0, 0, 0, 0.08);
  color: var(--text-primary);
  transform: translateX(2px);
}

.chart-tag {
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  background: rgba(0, 113, 227, 0.08);
  color: var(--accent-blue);
  font-size: 12px;
  font-weight: 600;
}

.chart-container {
  min-height: 280px;
  position: relative;
}

/* 图表骨架屏 */
.chart-skeleton {
  width: 100%;
  height: 280px;
  background: var(--surface-secondary);
  border-radius: var(--radius-md);
  position: relative;
  overflow: hidden;
}

.skeleton-shimmer {
  position: absolute;
  top: 0;
  left: -100%;
  width: 100%;
  height: 100%;
  background: linear-gradient(
    90deg,
    transparent 0%,
    rgba(255, 255, 255, 0.4) 50%,
    transparent 100%
  );
  animation: shimmer 2s infinite;
}

@keyframes shimmer {
  0% { left: -100%; }
  100% { left: 100%; }
}

/* 图表空状态 */
.chart-empty,
.chart-error {
  width: 100%;
  height: 280px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.empty-state,
.error-state {
  text-align: center;
  padding: 32px;
}

.empty-state-icon,
.error-state-icon {
  font-size: 48px;
  color: var(--text-tertiary);
  margin-bottom: 16px;
}

.empty-state-text,
.error-state-text {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-secondary);
  margin: 0 0 8px 0;
}

.empty-state-hint {
  font-size: 13px;
  color: var(--text-tertiary);
  margin: 0;
}

.error-state-btn {
  margin-top: 16px;
  padding: 10px 20px;
  border-radius: var(--radius-sm);
  border: none;
  background: var(--accent-blue);
  color: white;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.4s cubic-bezier(0.32, 0.72, 0, 1);
}

.error-state-btn:hover {
  background: #0077ed;
  transform: translateY(-1px);
}

/* ============================================
   最近投递
   ============================================ */

.recent-section {
  margin-bottom: 48px;
}

.recent-card-outer {
  background: var(--surface-secondary);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-xl);
  padding: 10px;
}

.recent-card-inner {
  background: var(--surface-primary);
  border-radius: calc(var(--radius-xl) - 10px);
  padding: 32px;
  box-shadow: var(--shadow-md);
}

.recent-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 28px;
}

.view-all-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  border: none;
  background: rgba(0, 0, 0, 0.04);
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.4s cubic-bezier(0.32, 0.72, 0, 1);
}

.view-all-btn:hover {
  background: rgba(0, 0, 0, 0.08);
  color: var(--text-primary);
  transform: translateX(2px);
}

.recent-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.recent-item {
  width: 100%;
  border: 0;
  font: inherit;
  text-align: left;
  padding: 16px 20px;
  border-radius: var(--radius-md);
  background: var(--surface-secondary);
  cursor: pointer;
  transition: background 0.4s cubic-bezier(0.32, 0.72, 0, 1);
}

/* Kept for any recent-item without data-reveal (defensive fallback) */
.recent-item:not([data-reveal]) {
  animation: fadeInUp 0.6s cubic-bezier(0.32, 0.72, 0, 1) forwards;
  opacity: 0;
}

@keyframes fadeInUp {
  from {
    opacity: 0;
    transform: translateY(16px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.recent-item:hover {
  background: var(--surface-tertiary);
}

.recent-item.is-revealed:hover {
  transform: translateX(4px);
}

.recent-item-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.recent-item-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.recent-item-id {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  font-family: 'Geist Mono', 'SF Mono', 'Monaco', 'Inconsolata', 'Fira Code', monospace;
}

.recent-item-date {
  font-size: 13px;
  color: var(--text-secondary);
}

.recent-item-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.recent-item-tag {
  padding: 4px 10px;
  border-radius: var(--radius-xs);
  font-size: 12px;
  font-weight: 600;
}

.tag-favorited { background: var(--tag-favorited-bg); color: var(--tag-favorited); }
.tag-preparing { background: var(--tag-preparing-bg); color: var(--tag-preparing); }
.tag-submitted { background: var(--tag-submitted-bg); color: var(--tag-submitted); }
.tag-interviewing { background: var(--tag-interviewing-bg); color: var(--tag-interviewing); }
.tag-offered { background: var(--tag-offered-bg); color: var(--tag-offered); }
.tag-rejected { background: var(--tag-rejected-bg); color: var(--tag-rejected); }
.tag-ignored { background: var(--tag-ignored-bg); color: var(--tag-ignored); }

.recent-item-arrow {
  font-size: 12px;
  color: var(--text-tertiary);
  transition: transform 0.4s cubic-bezier(0.32, 0.72, 0, 1);
}

.recent-item:hover .recent-item-arrow {
  transform: translateX(4px);
  color: var(--text-secondary);
}

.recent-empty {
  padding: 48px 0;
}

/* ============================================
   Scroll-reveal animations
   ============================================ */

/* Base hidden state for all reveal targets */
[data-reveal] {
  opacity: 0;
  transition-property: opacity, transform;
  transition-duration: 0.7s;
  transition-timing-function: cubic-bezier(0.32, 0.72, 0, 1);
  will-change: opacity, transform;
}

/* Fade-up reveal: translate upward into place */
[data-reveal='fade-up'] {
  transform: translateY(24px);
}

[data-reveal='fade-up'].is-revealed {
  opacity: 1;
  transform: translateY(0);
}

/* Scale reveal: subtle zoom-in for chart cards */
[data-reveal='scale'] {
  transform: scale(0.96) translateY(12px);
}

[data-reveal='scale'].is-revealed {
  opacity: 1;
  transform: scale(1) translateY(0);
}

/* Stagger children: each element with data-stagger gets a delay via JS.
   The parent section itself has no stagger delay. */
.stats-section[data-reveal] .stat-cell[data-reveal] {
  transition-duration: 0.65s;
}

/* Recent items inherit stagger from IntersectionObserver.
   Override the old fadeInUp keyframes — the reveal system handles opacity/transform now. */
.recent-item[data-reveal] {
  animation: none;
  transition-duration: 0.55s;
}

.recent-item[data-reveal]:not(.is-revealed) {
  opacity: 0;
  transform: translateY(16px);
}

.recent-item[data-reveal].is-revealed {
  opacity: 1;
  transform: translateY(0);
}

/* Respect prefers-reduced-motion: disable all scroll animations */
@media (prefers-reduced-motion: reduce) {
  [data-reveal] {
    opacity: 1 !important;
    transform: none !important;
    transition: none !important;
    animation: none !important;
  }

  .recent-item {
    opacity: 1 !important;
    transform: none !important;
    animation: none !important;
  }
}

/* ============================================
   响应式设计
   ============================================ */

@media (max-width: 1200px) {
  .dashboard {
    padding: 32px 32px;
  }

  .stats-bento {
    grid-template-columns: repeat(2, 1fr);
  }

  .stat-cell-wide {
    grid-column: span 1;
  }

  .charts-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 768px) {
  .dashboard {
    padding: 24px 16px;
  }

  .header-content {
    flex-direction: column;
    gap: 24px;
  }

  .page-title {
    font-size: 36px;
  }

  .onboarding-banner {
    align-items: stretch;
    flex-direction: column;
    padding: 20px;
  }

  .onboarding-actions {
    width: 100%;
  }

  .onboarding-primary,
  .onboarding-secondary {
    flex: 1;
  }

  .stats-bento {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .stat-cell-inner {
    padding: 20px;
  }

  .stat-value {
    font-size: 32px;
  }

  .chart-cell-inner {
    padding: 24px;
  }

  .chart-header {
    flex-direction: column;
    gap: 16px;
  }

  .recent-item-content {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }

  .recent-item-left {
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
  }
}

@media (max-width: 480px) {
  .page-title {
    font-size: 28px;
  }

  .stat-icon-wrapper {
    width: 48px;
    height: 48px;
    border-radius: var(--radius-md);
  }

  .stat-icon {
    font-size: 20px;
  }
}
</style>
