<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>采集计划</h1>
        <p>管理职位来源、配置采集计划、查看版本历史与运行记录。</p>
      </div>
      <a-space>
        <a-tag :color="planStateColor">{{ planStateLabel }}</a-tag>
        <a-tag v-if="head.active" color="blue">v{{ head.active.version }}</a-tag>
      </a-space>
    </div>

    <!-- Dependency not ready (503) -->
    <a-alert
      v-if="unavailable"
      type="warning"
      show-icon
      message="采集服务暂不可用"
      description="后端仓储或依赖未就绪（503）。请稍后重试，或检查服务是否启动。"
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
      <span>正在加载采集计划…</span>
    </div>

    <!-- Unavailable + nothing on screen yet -->
    <a-result
      v-else-if="unavailable && !head.active && sources.length === 0"
      status="warning"
      title="服务暂不可用"
      sub-title="采集计划依赖未就绪，无法读取或操作。"
    >
      <template #extra>
        <a-button type="primary" :loading="loading" @click="loadAll">重试</a-button>
      </template>
    </a-result>

    <template v-else>
      <!-- Sources + Plan overview side by side -->
      <a-row :gutter="16">
        <!-- Sources list -->
        <a-col :xs="24" :lg="14">
          <a-card class="detail-card" title="职位来源">
            <template #extra>
              <a-button :loading="loading" size="small" @click="loadSources">
                刷新
              </a-button>
            </template>
            <a-spin :spinning="loading && sources.length === 0">
              <a-table
                v-if="sources.length > 0 || !loading"
                :columns="sourceColumns"
                :data-source="sources"
                :pagination="false"
                row-key="id"
                size="small"
                :scroll="{ x: 640 }"
              >
                <template #bodyCell="{ column, record }">
                  <template v-if="column.key === 'type'">
                    <a-tag>{{ record.source_type }}</a-tag>
                  </template>
                  <template v-else-if="column.key === 'identifier'">
                    <span class="mono">{{ record.source_identifier }}</span>
                  </template>
                  <template v-else-if="column.key === 'state'">
                    <a-tag :color="sourceStateColor(record.state)">{{ sourceStateLabel(record.state) }}</a-tag>
                  </template>
                  <template v-else-if="column.key === 'policy'">
                    <a-space :size="4" wrap>
                      <a-tag :color="policyColor(record.trust_status)" size="small">信{{ policyLabel(record.trust_status) }}</a-tag>
                      <a-tag :color="policyColor(record.terms_status)" size="small">条{{ policyLabel(record.terms_status) }}</a-tag>
                      <a-tag :color="policyColor(record.robots_status)" size="small">机{{ policyLabel(record.robots_status) }}</a-tag>
                    </a-space>
                  </template>
                  <template v-else-if="column.key === 'permission'">
                    <template v-if="getPermissionForSource(record.id)">
                      <a-space :size="4">
                        <a-tag :color="permStateColor(getPermissionForSource(record.id).state)" size="small">
                          {{ permStateLabel(getPermissionForSource(record.id).state) }}
                        </a-tag>
                        <template v-if="getPermissionForSource(record.id).state === 'pending'">
                          <a-button type="link" size="small" :loading="permActionId === getPermissionForSource(record.id).id" @click="grantPerm(getPermissionForSource(record.id).id)">授权</a-button>
                          <a-button type="link" size="small" danger :loading="permActionId === getPermissionForSource(record.id).id" @click="denyPerm(getPermissionForSource(record.id).id)">拒绝</a-button>
                        </template>
                        <template v-else-if="getPermissionForSource(record.id).state === 'granted'">
                          <span class="muted" style="font-size: 11px">
                            {{ getPermissionForSource(record.id).expires_at ? '至 ' + formatDate(getPermissionForSource(record.id).expires_at) : '永久' }}
                          </span>
                        </template>
                      </a-space>
                    </template>
                    <span v-else class="muted">-</span>
                  </template>
                  <template v-else-if="column.key === 'action'">
                    <a-space>
                      <a-popconfirm
                        v-if="record.enabled"
                        title="暂停该来源？（保留历史记录）"
                        ok-text="暂停"
                        cancel-text="取消"
                        @confirm="pauseSource(record.id)"
                      >
                        <a-button type="link" size="small" :loading="sourceActionId === record.id">暂停</a-button>
                      </a-popconfirm>
                      <a-popconfirm
                        v-else
                        title="启用该来源？"
                        ok-text="启用"
                        cancel-text="取消"
                        @confirm="resumeSource(record.id)"
                      >
                        <a-button type="link" size="small" :loading="sourceActionId === record.id">启用</a-button>
                      </a-popconfirm>
                    </a-space>
                  </template>
                </template>
                <template #emptyText>
                  <a-empty description="尚未注册任何职位来源" />
                </template>
              </a-table>
            </a-spin>
          </a-card>
        </a-col>

        <!-- Plan overview -->
        <a-col :xs="24" :lg="10">
          <a-card class="detail-card" title="采集计划概览">
            <template #extra>
              <a-space>
                <a-button
                  v-if="head.state === 'active'"
                  size="small"
                  :loading="planActionLoading"
                  @click="pausePlan"
                >暂停计划</a-button>
                <a-button
                  v-if="head.state === 'paused'"
                  type="primary"
                  size="small"
                  :loading="planActionLoading"
                  @click="resumePlan"
                >恢复计划</a-button>
                <a-button
                  v-if="head.active"
                  type="primary"
                  size="small"
                  :loading="runNowLoading"
                  @click="runNow"
                >立即运行</a-button>
              </a-space>
            </template>

            <a-descriptions
              v-if="head.active"
              :column="1"
              size="small"
              bordered
            >
              <a-descriptions-item label="计划版本">v{{ head.active.version }}</a-descriptions-item>
              <a-descriptions-item label="状态">
                <a-tag :color="planStateColor">{{ planStateLabel }}</a-tag>
              </a-descriptions-item>
              <a-descriptions-item label="调度间隔">
                {{ formatInterval(head.active.interval_seconds) }}
              </a-descriptions-item>
              <a-descriptions-item label="时区">{{ head.active.timezone }}</a-descriptions-item>
              <a-descriptions-item label="下次运行">
                <span v-if="head.next_run_at">{{ formatDate(head.next_run_at) }}</span>
                <span v-else class="muted">-</span>
              </a-descriptions-item>
              <a-descriptions-item label="来源数">{{ head.active.sources?.length || 0 }}</a-descriptions-item>
              <a-descriptions-item label="规则版本">
                <span class="mono">{{ head.active.rules_version || '-' }}</span>
              </a-descriptions-item>
            </a-descriptions>

            <a-empty v-else description="尚无活跃计划版本，请创建一个新版本" />

            <!-- Effective filters (read-only) -->
            <template v-if="head.active">
              <a-divider orientation="left" plain>生效筛选条件</a-divider>
              <div class="filter-section">
                <div v-if="head.active.themes?.length" class="filter-row">
                  <span class="filter-label">主题</span>
                  <a-space wrap :size="4">
                    <a-tag v-for="t in head.active.themes" :key="t" size="small">{{ t }}</a-tag>
                  </a-space>
                </div>
                <div v-if="head.active.include_keywords?.length" class="filter-row">
                  <span class="filter-label">包含关键词</span>
                  <a-space wrap :size="4">
                    <a-tag v-for="k in head.active.include_keywords" :key="k" size="small" color="green">{{ k }}</a-tag>
                  </a-space>
                </div>
                <div v-if="head.active.exclude_keywords?.length" class="filter-row">
                  <span class="filter-label">排除关键词</span>
                  <a-space wrap :size="4">
                    <a-tag v-for="k in head.active.exclude_keywords" :key="k" size="small" color="red">{{ k }}</a-tag>
                  </a-space>
                </div>
                <div v-if="head.active.role_families?.length" class="filter-row">
                  <span class="filter-label">职能族</span>
                  <a-space wrap :size="4">
                    <a-tag v-for="r in head.active.role_families" :key="r" size="small">{{ r }}</a-tag>
                  </a-space>
                </div>
                <div v-if="head.active.seniority?.length" class="filter-row">
                  <span class="filter-label">级别</span>
                  <a-space wrap :size="4">
                    <a-tag v-for="s in head.active.seniority" :key="s" size="small">{{ s }}</a-tag>
                  </a-space>
                </div>
                <div v-if="head.active.locations?.length" class="filter-row">
                  <span class="filter-label">地点</span>
                  <a-space wrap :size="4">
                    <a-tag v-for="loc in head.active.locations" :key="loc.name" size="small">
                      {{ loc.name }}<span v-if="loc.kind !== 'preferred'"> ({{ loc.kind }})</span>
                    </a-tag>
                  </a-space>
                </div>
                <div v-if="head.active.remote_rules" class="filter-row">
                  <span class="filter-label">远程</span>
                  <a-space wrap :size="4">
                    <a-tag v-if="head.active.remote_rules.remote_allowed" size="small" color="cyan">允许远程</a-tag>
                    <a-tag v-if="head.active.remote_rules.hybrid_allowed" size="small" color="cyan">允许混合</a-tag>
                    <a-tag v-if="head.active.remote_rules.onsite_required" size="small" color="orange">必须到场</a-tag>
                  </a-space>
                </div>
                <div v-if="head.active.compensation?.currency" class="filter-row">
                  <span class="filter-label">薪酬</span>
                  <span>
                    {{ head.active.compensation.currency }}
                    <template v-if="head.active.compensation.amount_min != null">{{ head.active.compensation.amount_min }}</template>
                    <template v-if="head.active.compensation.amount_max != null"> - {{ head.active.compensation.amount_max }}</template>
                    <template v-if="head.active.compensation.period"> / {{ head.active.compensation.period }}</template>
                  </span>
                </div>
                <div
                  v-if="!head.active.themes?.length && !head.active.include_keywords?.length && !head.active.role_families?.length && !head.active.locations?.length"
                  class="muted"
                >
                  尚无自定义筛选条件
                </div>
              </div>
            </template>
          </a-card>
        </a-col>
      </a-row>

      <!-- Version history -->
      <a-card class="table-card" title="计划版本历史" style="margin-top: 16px">
        <a-spin :spinning="historyLoading && versions.length === 0">
          <a-table
            v-if="versions.length > 0 || !historyLoading"
            :columns="versionColumns"
            :data-source="versions"
            :pagination="false"
            row-key="id"
            :scroll="{ x: 720 }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'version'">
                <strong>v{{ record.version }}</strong>
                <a-tag v-if="record.is_active" color="green" style="margin-left: 8px">激活</a-tag>
              </template>
              <template v-else-if="column.key === 'interval'">
                {{ formatInterval(record.interval_seconds) }}
              </template>
              <template v-else-if="column.key === 'sources'">
                {{ record.sources?.length || 0 }}
              </template>
              <template v-else-if="column.key === 'rules'">
                <span class="mono">{{ record.rules_version || '-' }}</span>
              </template>
              <template v-else-if="column.key === 'created'">
                <span v-if="record.created_at">{{ formatDate(record.created_at) }}</span>
                <span v-else class="muted">-</span>
              </template>
              <template v-else-if="column.key === 'action'">
                <a-space>
                  <a-popconfirm
                    v-if="!record.is_active"
                    title="激活该版本？"
                    ok-text="激活"
                    cancel-text="取消"
                    @confirm="activateVersion(record.id)"
                  >
                    <a-button type="link" size="small" :loading="activatingId === record.id">激活</a-button>
                  </a-popconfirm>
                  <span v-else class="muted">当前</span>
                  <router-link :to="{ name: 'crawl-runs', query: { plan_version_id: record.id } }">
                    <a-button type="link" size="small">查看运行</a-button>
                  </router-link>
                </a-space>
              </template>
            </template>
            <template #emptyText>
              <a-empty description="尚无计划版本，创建第一个采集计划" />
            </template>
          </a-table>
        </a-spin>
      </a-card>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { api, formatApiError, parseApiError } from '../api/client.js'

const head = ref({ active: null, next_run_at: null, state: 'draft' })
const sources = ref([])
const versions = ref([])
const permissions = ref([])

const loading = ref(false)
const initialLoading = ref(true)
const historyLoading = ref(false)
const planActionLoading = ref(false)
const runNowLoading = ref(false)
const sourceActionId = ref('')
const permActionId = ref('')
const activatingId = ref('')
const error = ref('')
const unavailable = ref(false)

// --- Columns ---

const sourceColumns = [
  { title: '类型', key: 'type', dataIndex: 'source_type', width: 100 },
  { title: '标识', key: 'identifier', dataIndex: 'source_identifier', ellipsis: true },
  { title: '状态', key: 'state', dataIndex: 'state', width: 100 },
  { title: '策略', key: 'policy', width: 180 },
  { title: '权限', key: 'permission', width: 200 },
  { title: '操作', key: 'action', width: 140 },
]

const versionColumns = [
  { title: '版本', key: 'version', dataIndex: 'version', width: 120 },
  { title: '调度间隔', key: 'interval', dataIndex: 'interval_seconds', width: 140 },
  { title: '来源数', key: 'sources', dataIndex: 'sources', width: 80 },
  { title: '规则版本', key: 'rules', dataIndex: 'rules_version', width: 160 },
  { title: '创建时间', key: 'created', dataIndex: 'created_at', width: 140 },
  { title: '操作', key: 'action', width: 160 },
]

// --- State labels/colors ---

const planStateColor = computed(() => {
  return { active: 'green', paused: 'orange', draft: 'default', archived: 'default' }[head.value.state] || 'default'
})

const planStateLabel = computed(() => {
  return { active: '运行中', paused: '已暂停', draft: '草稿', archived: '已归档' }[head.value.state] || head.value.state
})

function sourceStateColor(state) {
  return { active: 'green', paused: 'orange', pending_review: 'blue', blocked: 'red' }[state] || 'default'
}

function sourceStateLabel(state) {
  return { active: '活跃', paused: '已暂停', pending_review: '待审核', blocked: '已封禁' }[state] || state
}

function policyColor(status) {
  return { allowed: 'green', blocked: 'red', unknown: 'default' }[status] || 'default'
}

function policyLabel(status) {
  return { allowed: '允许', blocked: '封禁', unknown: '未知' }[status] || status
}

function permStateColor(state) {
  return { pending: 'orange', granted: 'green', denied: 'red', revoked: 'red', expired: 'default' }[state] || 'default'
}

function permStateLabel(state) {
  return { pending: '待授权', granted: '已授权', denied: '已拒绝', revoked: '已撤销', expired: '已过期' }[state] || state
}

function getPermissionForSource(sourceId) {
  return permissions.value.find((p) => p.source_id === sourceId) || null
}

// --- Formatting ---

function formatInterval(seconds) {
  if (!seconds) return '-'
  if (seconds < 60) return `${seconds}秒`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分钟`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}小时`
  return `${Math.floor(seconds / 86400)}天`
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

// --- Data loading ---

async function loadSources() {
  try {
    const data = await api.listCrawlSources({ limit: 200 })
    sources.value = data.items || []
    unavailable.value = false
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else {
      error.value = formatApiError(err, '加载来源列表失败。')
    }
  }
}

async function loadHead() {
  try {
    const data = await api.getCrawlPlanHead()
    head.value = data
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else {
      error.value = formatApiError(err, '加载采集计划失败。')
    }
  }
}

async function loadHistory() {
  historyLoading.value = true
  try {
    const data = await api.listCrawlPlanVersions({ limit: 50 })
    versions.value = data.items || []
  } catch (err) {
    const info = parseApiError(err)
    if (!info.isDependencyNotReady) {
      error.value = formatApiError(err, '加载版本历史失败。')
    }
  } finally {
    historyLoading.value = false
  }
}

async function loadPermissions() {
  try {
    const data = await api.listCrawlPermissions({ limit: 200 })
    permissions.value = data.items || data || []
  } catch {
    // Permissions endpoint may not be available yet; fail silently.
    permissions.value = []
  }
}

async function loadAll() {
  loading.value = true
  error.value = ''
  unavailable.value = false
  await Promise.all([loadSources(), loadHead(), loadHistory(), loadPermissions()])
  loading.value = false
  initialLoading.value = false
}

// --- Source actions ---

async function pauseSource(id) {
  sourceActionId.value = id
  error.value = ''
  try {
    await api.pauseCrawlSource(id)
    message.success('已暂停')
    await Promise.all([loadSources(), loadPermissions()])
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) unavailable.value = true
    else error.value = formatApiError(err, '暂停失败。')
  } finally {
    sourceActionId.value = ''
  }
}

async function resumeSource(id) {
  sourceActionId.value = id
  error.value = ''
  try {
    await api.resumeCrawlSource(id)
    message.success('已启用')
    await Promise.all([loadSources(), loadPermissions()])
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) unavailable.value = true
    else if (info.status === 409 || info.code === 'INVALID_STATE') {
      error.value = formatApiError(err, '该来源无法启用（策略封禁或状态不允许）。')
    } else {
      error.value = formatApiError(err, '启用失败。')
    }
  } finally {
    sourceActionId.value = ''
  }
}

async function grantPerm(id) {
  permActionId.value = id
  error.value = ''
  try {
    await api.grantCrawlPermission(id)
    message.success('已授权')
    await Promise.all([loadPermissions(), loadSources()])
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) unavailable.value = true
    else error.value = formatApiError(err, '授权失败。')
  } finally {
    permActionId.value = ''
  }
}

async function denyPerm(id) {
  permActionId.value = id
  error.value = ''
  try {
    await api.denyCrawlPermission(id)
    message.success('已拒绝')
    await loadPermissions()
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) unavailable.value = true
    else error.value = formatApiError(err, '拒绝失败。')
  } finally {
    permActionId.value = ''
  }
}

// --- Plan actions ---

async function pausePlan() {
  planActionLoading.value = true
  error.value = ''
  try {
    const data = await api.pauseCrawlPlan()
    head.value = data
    message.success('计划已暂停')
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) unavailable.value = true
    else error.value = formatApiError(err, '暂停计划失败。')
  } finally {
    planActionLoading.value = false
  }
}

async function resumePlan() {
  planActionLoading.value = true
  error.value = ''
  try {
    const data = await api.resumeCrawlPlan()
    head.value = data
    message.success('计划已恢复')
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) unavailable.value = true
    else if (info.status === 409 || info.code === 'INVALID_STATE') {
      error.value = formatApiError(err, '无法恢复：无可用版本或调度无效。')
    } else {
      error.value = formatApiError(err, '恢复计划失败。')
    }
  } finally {
    planActionLoading.value = false
  }
}

async function runNow() {
  runNowLoading.value = true
  error.value = ''
  try {
    const run = await api.runCrawlPlanNow()
    message.success(`已创建运行 ${run.run_identity.slice(0, 8)}…（状态: ${run.state}）`)
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) unavailable.value = true
    else if (info.status === 409 || info.code === 'INVALID_STATE') {
      error.value = formatApiError(err, '无法运行：需要活跃计划和至少一个启用来源。')
    } else {
      error.value = formatApiError(err, '创建运行失败。')
    }
  } finally {
    runNowLoading.value = false
  }
}

async function activateVersion(id) {
  activatingId.value = id
  error.value = ''
  try {
    await api.activateCrawlPlanVersion(id)
    message.success('版本已激活')
    await Promise.all([loadHead(), loadHistory()])
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) unavailable.value = true
    else if (info.status === 409 || info.code === 'INVALID_STATE') {
      error.value = formatApiError(err, '版本激活失败（调度无效或校验未通过）。')
    } else {
      error.value = formatApiError(err, '激活失败。')
    }
  } finally {
    activatingId.value = ''
  }
}

onMounted(loadAll)
</script>

<style scoped>
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #667085;
}
.filter-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.filter-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.filter-label {
  min-width: 80px;
  color: #667085;
  font-size: 13px;
  flex-shrink: 0;
}
</style>
