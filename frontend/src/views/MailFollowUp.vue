<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>邮件跟进</h1>
        <p>查看招聘邮件线程、同步状态与待解析的关联。</p>
      </div>
      <a-space>
        <a-tag v-if="account.connected" color="green">已连接</a-tag>
        <a-tag v-else-if="syncDenied" color="orange">同步未启用</a-tag>
        <a-tag v-else color="default">未连接</a-tag>
        <a-button :loading="syncing" :disabled="!account.sync_available" @click="syncNow">
          立即同步
        </a-button>
        <a-button :loading="loading" @click="refreshAll">刷新</a-button>
      </a-space>
    </div>

    <!-- Dependency not ready (503) -->
    <a-alert
      v-if="unavailable"
      type="warning"
      show-icon
      message="邮件同步服务暂不可用"
      description="后端依赖未就绪（503）。请稍后重试。"
      closable
      @close="unavailable = false"
    />

    <!-- Default-deny: GMAIL_READ capability denied (Iron Rule 7). The 403 is
         the "sync unavailable" signal — never present old data as current. -->
    <a-alert
      v-if="syncDenied"
      type="info"
      show-icon
      message="邮件同步尚未启用"
    >
      <template #description>
        Gmail 只读同步默认保持关闭（能力门 DENIED）。连接专用账号并完成资格审核后，
        同步将在此处显示最新线程与跟进状态。在此之前，仍可使用手动追踪与应用工作区。
      </template>
    </a-alert>

    <a-alert
      v-if="error"
      type="error"
      show-icon
      :message="error"
      closable
      @close="error = ''"
    />

    <!-- Stale sync status (task 11.8): when connected but the last sync is old
         or the account is in ERROR, surface the stale time without claiming
         the data is current. -->
    <a-alert
      v-if="account.connected && staleNotice"
      type="warning"
      show-icon
      :message="staleNotice"
    />

    <a-row :gutter="16">
      <a-col :xs="24" :lg="14">
        <a-card title="线程摘要" class="section-card">
          <a-spin :spinning="loading && threads.length === 0">
            <a-empty v-if="!threads.length && !loading" description="暂无线程" />
            <a-list :data-source="threads" :locale="{ emptyText: ' ' }">
              <template #renderItem="{ item }">
                <a-list-item>
                  <a-list-item-meta>
                    <template #title>
                      <span class="thread-subject">{{ item.subject || '(无主题)' }}</span>
                    </template>
                    <template #description>
                      <span class="mono small">{{ item.provider_thread_id }}</span>
                      <span v-if="item.application_id" class="link-badge">
                        <router-link :to="`/applications/${item.application_id}`">已关联申请</router-link>
                      </span>
                    </template>
                  </a-list-item-meta>
                </a-list-item>
              </template>
            </a-list>
            <div v-if="threadsNextCursor" class="load-more">
              <a-button :loading="loadingMore" @click="loadMoreThreads">加载更多</a-button>
            </div>
          </a-spin>
        </a-card>
      </a-col>

      <a-col :xs="24" :lg="10">
        <a-card title="待解析关联" class="section-card">
          <template #extra>
            <a-tag color="orange">{{ unresolved.length }}</a-tag>
          </template>
          <a-spin :spinning="loading && unresolved.length === 0">
            <a-empty
              v-if="!unresolved.length && !loading"
              description="没有待解析的线程"
            />
            <a-list :data-source="unresolved" :locale="{ emptyText: ' ' }">
              <template #renderItem="{ item }">
                <a-list-item>
                  <a-list-item-meta>
                    <template #title>
                      <span>{{ item.subject || '(无主题)' }}</span>
                    </template>
                    <template #description>
                      <div class="small">
                        匹配 {{ item.candidate_application_ids?.length || 0 }} 个申请
                      </div>
                      <div class="evidence-refs">
                        <a-tag
                          v-for="(value, key) in item.evidence_refs || {}"
                          :key="key"
                          size="small"
                        >
                          {{ key }}: {{ value }}
                        </a-tag>
                      </div>
                    </template>
                  </a-list-item-meta>
                  <template #actions>
                    <a-select
                      v-if="item.candidate_application_ids?.length"
                      :placeholder="'选择申请'"
                      style="width: 220px"
                      :value="pendingChoice[item.link_id]"
                      @change="(val) => (pendingChoice[item.link_id] = val)"
                    >
                      <a-select-option
                        v-for="appId in item.candidate_application_ids"
                        :key="appId"
                        :value="appId"
                      >
                        {{ appId.slice(0, 8) }}…
                      </a-select-option>
                    </a-select>
                    <a-button
                      type="primary"
                      size="small"
                      :loading="confirming === item.link_id"
                      @click="confirmLink(item.link_id)"
                    >
                      确认
                    </a-button>
                  </template>
                </a-list-item>
              </template>
            </a-list>
          </a-spin>
        </a-card>

        <a-card title="同步历史" class="section-card">
          <a-spin :spinning="loading && runs.length === 0">
            <a-empty v-if="!runs.length && !loading" description="暂无同步记录" />
            <a-list :data-source="runs" :locale="{ emptyText: ' ' }">
              <template #renderItem="{ item }">
                <a-list-item>
                  <a-list-item-meta>
                    <template #title>
                      <a-tag :color="runColor(item.status)">{{ item.status }}</a-tag>
                      <span class="small">处理 {{ item.messages_processed }} · 跳过 {{ item.messages_skipped }}</span>
                    </template>
                    <template #description>
                      <span v-if="item.error_code" class="small error-text">{{ item.error_code }}</span>
                    </template>
                  </a-list-item-meta>
                </a-list-item>
              </template>
            </a-list>
          </a-spin>
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { api, parseApiError } from '../api/client.js'

const loading = ref(false)
const loadingMore = ref(false)
const syncing = ref(false)
const confirming = ref(null)
const error = ref('')
const unavailable = ref(false)

// A 403 DENIED_POLICY from the GMAIL_READ gate means sync is not enabled. This
// is the default-deny surface (Iron Rule 7); the view shows the unavailable
// state rather than empty data masquerading as current.
const syncDenied = ref(false)

const account = reactive({
  connected: false,
  connection_state: 'disconnected',
  sync_available: false,
  email_address: '',
  last_sync_at: null,
  last_error_code: '',
})

const threads = ref([])
const threadsNextCursor = ref(null)
const unresolved = ref([])
const runs = ref([])
const pendingChoice = reactive({})

const STALE_AFTER_MS = 1000 * 60 * 60 * 6 // 6h — show a stale notice beyond this

const staleNotice = computed(() => {
  if (account.connection_state === 'error') {
    return `同步出错：${account.last_error_code || '未知错误'}。数据可能不是最新。`
  }
  if (account.last_sync_at) {
    const age = Date.now() - new Date(account.last_sync_at).getTime()
    if (age > STALE_AFTER_MS) {
      return `上次同步于 ${formatTime(account.last_sync_at)}，数据可能已过时。`
    }
  }
  return ''
})

function formatTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function runColor(status) {
  if (status === 'completed') return 'green'
  if (status === 'failed') return 'red'
  if (status === 'partial') return 'orange'
  return 'blue'
}

async function fetchAccount() {
  try {
    const data = await api.getMailAccountStatus()
    Object.assign(account, data)
    syncDenied.value = false
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else if (info.status === 403) {
      syncDenied.value = true
      account.connected = false
      account.sync_available = false
    } else {
      error.value = info.message || String(err)
    }
  }
}

async function fetchThreads(cursor) {
  try {
    const data = await api.listMailThreads({ cursor: cursor || null, limit: 20 })
    if (cursor) {
      threads.value = threads.value.concat(data.items || [])
    } else {
      threads.value = data.items || []
    }
    threadsNextCursor.value = data.next_cursor || null
  } catch (err) {
    const info = parseApiError(err)
    if (!info.isDependencyNotReady && info.status !== 403) {
      error.value = info.message || String(err)
    }
  }
}

async function fetchUnresolved() {
  try {
    const data = await api.listMailUnresolvedLinks({ limit: 20 })
    unresolved.value = data.items || []
  } catch (err) {
    const info = parseApiError(err)
    if (!info.isDependencyNotReady && info.status !== 403) {
      error.value = info.message || String(err)
    }
  }
}

async function fetchRuns() {
  try {
    const data = await api.listMailSyncHistory({ limit: 10 })
    runs.value = data.items || []
  } catch (err) {
    const info = parseApiError(err)
    if (!info.isDependencyNotReady && info.status !== 403) {
      error.value = info.message || String(err)
    }
  }
}

async function loadMoreThreads() {
  if (!threadsNextCursor.value) return
  loadingMore.value = true
  try {
    await fetchThreads(threadsNextCursor.value)
  } finally {
    loadingMore.value = false
  }
}

async function refreshAll() {
  loading.value = true
  error.value = ''
  try {
    await Promise.all([fetchAccount(), fetchThreads(null), fetchUnresolved(), fetchRuns()])
  } finally {
    loading.value = false
  }
}

async function syncNow() {
  syncing.value = true
  error.value = ''
  try {
    await api.syncMailNow({ direction: 'incremental' })
    await refreshAll()
  } catch (err) {
    const info = parseApiError(err)
    error.value = info.message || String(err)
  } finally {
    syncing.value = false
  }
}

async function confirmLink(linkId) {
  confirming.value = linkId
  error.value = ''
  try {
    const choice = pendingChoice[linkId] || null
    await api.confirmMailLink(linkId, { application_id: choice })
    delete pendingChoice[linkId]
    await Promise.all([fetchUnresolved(), fetchThreads(null)])
  } catch (err) {
    const info = parseApiError(err)
    error.value = info.message || String(err)
  } finally {
    confirming.value = null
  }
}

onMounted(() => {
  refreshAll()
})
</script>

<style scoped>
.page-shell {
  padding: 24px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 16px;
  gap: 16px;
  flex-wrap: wrap;
}
.section-card {
  margin-bottom: 16px;
}
.thread-subject {
  font-weight: 600;
}
.link-badge {
  margin-left: 8px;
}
.evidence-refs {
  margin-top: 4px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.load-more {
  text-align: center;
  margin-top: 12px;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.small {
  font-size: 12px;
  color: #666;
}
.error-text {
  color: #cf1322;
}
</style>
