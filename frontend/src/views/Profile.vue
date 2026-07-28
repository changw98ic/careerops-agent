<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h1>求职画像</h1>
        <p>维护目标职位、地点、远程/授权与薪酬偏好。每次保存生成不可变版本，激活版本用于筛选与匹配。</p>
      </div>
      <a-space>
        <a-tag v-if="activeVersion" color="green">激活版本 v{{ activeVersion.version }}</a-tag>
        <a-tag v-else color="orange">尚无激活版本</a-tag>
      </a-space>
    </div>

    <!-- Dependency not ready (503) -->
    <a-alert
      v-if="unavailable"
      type="warning"
      show-icon
      message="画像服务暂不可用"
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

    <!-- Validation error from 409 INVALID_STATE -->
    <a-alert
      v-if="validationError"
      type="error"
      show-icon
      banner
      :message="`保存被拒绝：${validationError}`"
      closable
      @close="validationError = ''"
    />

    <!-- Loading -->
    <div v-if="initialLoading" class="page-loading">
      <a-spin size="large" />
      <span>正在加载画像…</span>
    </div>

    <!-- Unavailable + nothing on screen yet -->
    <a-result
      v-else-if="unavailable && !activeVersion"
      status="warning"
      title="服务暂不可用"
      sub-title="画像依赖未就绪，无法读取或保存。"
    >
      <template #extra>
        <a-button type="primary" :loading="loading" @click="loadAll">重试</a-button>
      </template>
    </a-result>

    <template v-else>
      <!-- Editor -->
      <a-card class="detail-card" title="编辑偏好">
        <SmartIntakePanel
          target="profile"
          title="用自然语言预填求职画像"
          :context="{ profile_version_id: activeVersion?.id || null }"
          :base-snapshot="form"
          :current-snapshot="form"
          :feature-enabled="smartIntakeUiEnabled"
          @applied="applySmartProfilePatch"
        />
        <a-form layout="vertical" :model="form">
          <a-divider orientation="left" plain>目标职位</a-divider>
          <div v-if="form.target_roles.length === 0" class="muted row-hint">
            尚未添加目标职位（至少一个才能激活版本）
          </div>
          <div
            v-for="(role, idx) in form.target_roles"
            :key="'role-' + idx"
            class="repeat-row"
          >
            <a-input
              v-model:value="role.title"
              placeholder="职位标题，如 Backend Engineer"
              style="flex: 2"
            />
            <a-input
              v-model:value="role.seniority"
              placeholder="级别 (可选)"
              style="flex: 1"
            />
            <a-button danger type="text" @click="removeRole(idx)">删除</a-button>
          </div>
          <a-button type="dashed" size="small" @click="addRole">
            <PlusOutlined /> 添加目标职位
          </a-button>

          <a-divider orientation="left" plain>综合级别</a-divider>
          <a-input
            v-model:value="form.seniority"
            placeholder="如 Senior / Mid / Lead (可选)"
            style="max-width: 320px"
          />

          <a-divider orientation="left" plain>地点偏好</a-divider>
          <div v-if="form.locations.length === 0" class="muted row-hint">
            尚未添加地点
          </div>
          <div
            v-for="(loc, idx) in form.locations"
            :key="'loc-' + idx"
            class="repeat-row"
          >
            <a-input v-model:value="loc.name" placeholder="地点，如 Shanghai" style="flex: 2" />
            <a-select v-model:value="loc.kind" style="flex: 1">
              <a-select-option value="preferred">优先 (preferred)</a-select-option>
              <a-select-option value="required">必须 (required)</a-select-option>
              <a-select-option value="excluded">排除 (excluded)</a-select-option>
            </a-select>
            <a-input-number
              v-model:value="loc.radius_km"
              placeholder="半径 km"
              :min="0"
              style="flex: 1"
            />
            <a-button danger type="text" @click="removeLocation(idx)">删除</a-button>
          </div>
          <a-button type="dashed" size="small" @click="addLocation">
            <PlusOutlined /> 添加地点
          </a-button>

          <a-divider orientation="left" plain>远程规则</a-divider>
          <a-space wrap>
            <a-checkbox v-model:checked="form.remote_rules.remote_allowed">允许远程</a-checkbox>
            <a-checkbox v-model:checked="form.remote_rules.hybrid_allowed">允许混合</a-checkbox>
            <a-checkbox v-model:checked="form.remote_rules.onsite_required">必须 onsite</a-checkbox>
            <a-input
              v-model:value="form.remote_rules.timezone"
              placeholder="时区 (如 Asia/Shanghai)"
              style="width: 200px"
            />
          </a-space>

          <a-divider orientation="left" plain>薪酬偏好</a-divider>
          <a-space wrap>
            <a-input
              v-model:value="form.compensation.currency"
              placeholder="币种 (如 CNY)"
              style="width: 110px"
            />
            <a-input-number
              v-model:value="form.compensation.amount_min"
              placeholder="下限"
              :min="0"
              style="width: 140px"
            />
            <a-input-number
              v-model:value="form.compensation.amount_max"
              placeholder="上限"
              :min="0"
              style="width: 140px"
            />
            <a-select v-model:value="form.compensation.period" style="width: 130px">
              <a-select-option value="">周期</a-select-option>
              <a-select-option value="annual">annual</a-select-option>
              <a-select-option value="monthly">monthly</a-select-option>
              <a-select-option value="hourly">hourly</a-select-option>
            </a-select>
            <a-checkbox v-model:checked="form.compensation.equity">接受期权</a-checkbox>
          </a-space>

          <a-divider orientation="left" plain>工作授权</a-divider>
          <a-space wrap align="start">
            <a-input
              v-model:value="form.authorization.work_authorization"
              placeholder="授权状态 (如 citizen / h1b)"
              style="width: 240px"
            />
            <a-checkbox v-model:checked="form.authorization.visa_sponsorship_required">
              需要 visa sponsorship
            </a-checkbox>
            <a-select
              v-model:value="form.authorization.locale_restrictions"
              mode="tags"
              placeholder="地区限制 (回车添加)"
              style="min-width: 240px"
            />
          </a-space>

          <a-divider orientation="left" plain>关键词</a-divider>
          <div class="keyword-row">
            <div class="keyword-block">
              <div class="muted">包含关键词 (include)</div>
              <a-select
                v-model:value="form.include_keywords"
                mode="tags"
                placeholder="如 distributed systems"
                style="width: 100%"
              />
            </div>
            <div class="keyword-block">
              <div class="muted">排除关键词 (exclude)</div>
              <a-select
                v-model:value="form.exclude_keywords"
                mode="tags"
                placeholder="如 crypto"
                style="width: 100%"
              />
            </div>
          </div>

          <a-divider orientation="left" plain>硬性排除</a-divider>
          <div class="keyword-row">
            <div class="keyword-block">
              <div class="muted">排除公司</div>
              <a-select
                v-model:value="form.hard_exclusions.companies"
                mode="tags"
                placeholder="回车添加"
                style="width: 100%"
              />
            </div>
            <div class="keyword-block">
              <div class="muted">排除职位名</div>
              <a-select
                v-model:value="form.hard_exclusions.titles"
                mode="tags"
                placeholder="回车添加"
                style="width: 100%"
              />
            </div>
            <div class="keyword-block">
              <div class="muted">排除关键词</div>
              <a-select
                v-model:value="form.hard_exclusions.keywords"
                mode="tags"
                placeholder="回车添加"
                style="width: 100%"
              />
            </div>
          </div>

          <div class="save-bar">
            <a-space>
              <a-button
                type="primary"
                :loading="saving"
                :disabled="!canSave"
                @click="save"
              >
                保存并激活新版本
              </a-button>
              <a-button :disabled="saving || !activeVersion" @click="resetForm">重置为当前激活版本</a-button>
            </a-space>
            <span v-if="!canSave" class="muted">至少需要一个目标职位才能保存激活版本</span>
          </div>
        </a-form>
      </a-card>

      <!-- History -->
      <a-card class="table-card" title="版本历史" style="margin-top: 16px">
        <a-spin :spinning="historyLoading && versions.length === 0">
          <a-table
            v-if="versions.length > 0 || !historyLoading"
            :columns="historyColumns"
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
              <template v-else-if="column.key === 'roles'">
                <span v-if="record.target_roles?.length">
                  {{ record.target_roles.map((r) => r.title).join(' / ') }}
                </span>
                <span v-else class="muted">-</span>
              </template>
              <template v-else-if="column.key === 'rules'">
                <span class="mono">{{ record.rules_version || '-' }}</span>
              </template>
              <template v-else-if="column.key === 'action'">
                <a-popconfirm
                  v-if="!record.is_active"
                  title="激活该版本？（会重新校验）"
                  ok-text="激活"
                  cancel-text="取消"
                  @confirm="activate(record.id)"
                >
                  <a-button type="link" size="small" :loading="activatingId === record.id">
                    激活
                  </a-button>
                </a-popconfirm>
                <span v-else class="muted">当前</span>
              </template>
            </template>
            <template #emptyText>
              <a-empty description="尚无历史版本，保存第一份偏好即可" />
            </template>
          </a-table>
        </a-spin>
      </a-card>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { PlusOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { api, formatApiError, parseApiError, smartIntakeUiEnabled } from '../api/client.js'
import SmartIntakePanel from '../components/SmartIntakePanel.vue'

const activeVersion = ref(null)
const versions = ref([])
const loading = ref(false)
const initialLoading = ref(true)
const historyLoading = ref(false)
const saving = ref(false)
const activatingId = ref('')
const error = ref('')
const validationError = ref('')
const unavailable = ref(false)

const historyColumns = [
  { title: '版本', key: 'version', dataIndex: 'version', width: 140 },
  { title: '目标职位', key: 'roles', dataIndex: 'target_roles' },
  { title: '规则版本', key: 'rules', dataIndex: 'rules_version', width: 200 },
  { title: '操作', key: 'action', width: 120 },
]

function emptyForm() {
  return {
    target_roles: [],
    seniority: '',
    locations: [],
    remote_rules: { remote_allowed: false, hybrid_allowed: false, onsite_required: false, timezone: '' },
    compensation: { currency: '', amount_min: null, amount_max: null, period: '', equity: false },
    authorization: { work_authorization: '', visa_sponsorship_required: false, locale_restrictions: [] },
    include_keywords: [],
    exclude_keywords: [],
    hard_exclusions: { companies: [], titles: [], keywords: [] },
  }
}

const form = reactive(emptyForm())

const canSave = computed(() =>
  form.target_roles.some((r) => (r.title || '').trim() !== ''),
)

function addRole() {
  form.target_roles.push({ title: '', seniority: '', notes: '' })
}
function removeRole(idx) {
  form.target_roles.splice(idx, 1)
}
function addLocation() {
  form.locations.push({ name: '', kind: 'preferred', radius_km: null })
}
function removeLocation(idx) {
  form.locations.splice(idx, 1)
}

function fillFormFrom(v) {
  if (!v) {
    Object.assign(form, emptyForm())
    return
  }
  form.target_roles = (v.target_roles || []).map((r) => ({
    title: r.title || '',
    seniority: r.seniority || '',
    notes: r.notes || '',
  }))
  form.seniority = v.seniority || ''
  form.locations = (v.locations || []).map((l) => ({
    name: l.name || '',
    kind: l.kind || 'preferred',
    radius_km: l.radius_km ?? null,
  }))
  form.remote_rules = {
    remote_allowed: !!v.remote_rules?.remote_allowed,
    hybrid_allowed: !!v.remote_rules?.hybrid_allowed,
    onsite_required: !!v.remote_rules?.onsite_required,
    timezone: v.remote_rules?.timezone || '',
  }
  form.compensation = {
    currency: v.compensation?.currency || '',
    amount_min: v.compensation?.amount_min ?? null,
    amount_max: v.compensation?.amount_max ?? null,
    period: v.compensation?.period || '',
    equity: !!v.compensation?.equity,
  }
  form.authorization = {
    work_authorization: v.authorization?.work_authorization || '',
    visa_sponsorship_required: !!v.authorization?.visa_sponsorship_required,
    locale_restrictions: [...(v.authorization?.locale_restrictions || [])],
  }
  form.include_keywords = [...(v.include_keywords || [])]
  form.exclude_keywords = [...(v.exclude_keywords || [])]
  form.hard_exclusions = {
    companies: [...(v.hard_exclusions?.companies || [])],
    titles: [...(v.hard_exclusions?.titles || [])],
    keywords: [...(v.hard_exclusions?.keywords || [])],
  }
}

function resetForm() {
  fillFormFrom(activeVersion.value)
  validationError.value = ''
}

function applySmartProfilePatch({ patch, baseline }) {
  const fields = patch || {}
  let conflicts = 0
  for (const [path, value] of Object.entries(fields)) {
    const locationMatch = /^locations\[(\d+)\]\./.exec(path)
    if (locationMatch) {
      const locationIndex = Number(locationMatch[1])
      const baselineLocation = baseline?.locations?.[locationIndex]
      const currentLocation = form.locations?.[locationIndex]
      // Smart intake emits preferred locations only. Never let a low-risk
      // name/radius patch inherit or overwrite a required/excluded location.
      if (
        (baselineLocation && baselineLocation.kind !== 'preferred') ||
        (currentLocation && currentLocation.kind !== 'preferred')
      ) {
        conflicts += 1
        continue
      }
      if (!currentLocation) {
        form.locations[locationIndex] = { name: '', kind: 'preferred', radius_km: null }
      }
    }
    if (!sameValue(getAt(baseline, path), getAt(form, path))) {
      conflicts += 1
      continue
    }
    setAt(form, path, clone(value))
  }
  if (conflicts) {
    message.warning(`${conflicts} 项表单在预览后已被修改，已保留当前值。`)
  } else if (Object.keys(fields).length) {
    message.success('智能建议已写入当前草稿；请检查后手工保存。')
  }
}

function pathParts(path) {
  return path.replaceAll('[', '.').replaceAll(']', '').split('.').filter(Boolean)
}

function getAt(root, path) {
  return pathParts(path).reduce((current, part) => current?.[part], root)
}

function setAt(root, path, value) {
  const parts = pathParts(path)
  let current = root
  for (let index = 0; index < parts.length - 1; index += 1) {
    const part = parts[index]
    const next = parts[index + 1]
    if (current[part] == null) current[part] = /^\d+$/.test(next) ? [] : {}
    current = current[part]
  }
  current[parts.at(-1)] = value
}

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value))
}

function sameValue(left, right) {
  return JSON.stringify(left) === JSON.stringify(right)
}

function buildPayload() {
  return {
    target_roles: form.target_roles
      .filter((r) => (r.title || '').trim() !== '')
      .map((r) => ({ title: r.title.trim(), seniority: (r.seniority || '').trim(), notes: r.notes || '' })),
    seniority: (form.seniority || '').trim(),
    locations: form.locations
      .filter((l) => (l.name || '').trim() !== '')
      .map((l) => ({
        name: l.name.trim(),
        kind: l.kind || 'preferred',
        radius_km: l.radius_km ?? null,
      })),
    remote_rules: { ...form.remote_rules },
    compensation: { ...form.compensation },
    authorization: { ...form.authorization },
    include_keywords: [...form.include_keywords],
    exclude_keywords: [...form.exclude_keywords],
    hard_exclusions: { ...form.hard_exclusions },
    activate: true,
  }
}

async function loadActive() {
  try {
    const data = await api.getActiveProfile()
    activeVersion.value = data
    fillFormFrom(data)
    unavailable.value = false
  } catch (err) {
    const info = parseApiError(err)
    if (info.status === 404) {
      activeVersion.value = null
      fillFormFrom(null)
    } else if (info.isDependencyNotReady) {
      unavailable.value = true
    } else {
      error.value = formatApiError(err, '加载画像失败，请稍后重试。')
    }
  }
}

async function loadHistory() {
  historyLoading.value = true
  try {
    const data = await api.listProfileVersions({ limit: 50 })
    versions.value = data.items || []
  } catch (err) {
    const info = parseApiError(err)
    if (!info.isDependencyNotReady) {
      error.value = formatApiError(err, '加载历史版本失败。')
    }
  } finally {
    historyLoading.value = false
  }
}

async function loadAll() {
  loading.value = true
  error.value = ''
  unavailable.value = false
  await Promise.all([loadActive(), loadHistory()])
  loading.value = false
  initialLoading.value = false
}

async function save() {
  if (!canSave.value) return
  saving.value = true
  validationError.value = ''
  error.value = ''
  try {
    const created = await api.createProfileVersion(buildPayload())
    activeVersion.value = created
    message.success('已保存并激活新版本')
    await loadHistory()
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else if (info.status === 409 || info.code === 'INVALID_STATE') {
      validationError.value = formatApiError(err, '偏好校验未通过。')
    } else {
      error.value = formatApiError(err, '保存失败，请稍后重试。')
    }
  } finally {
    saving.value = false
  }
}

async function activate(id) {
  activatingId.value = id
  validationError.value = ''
  try {
    const v = await api.activateProfileVersion(id)
    activeVersion.value = v
    fillFormFrom(v)
    message.success(`已激活 v${v.version}`)
    await loadHistory()
  } catch (err) {
    const info = parseApiError(err)
    if (info.isDependencyNotReady) {
      unavailable.value = true
    } else if (info.status === 409 || info.code === 'INVALID_STATE') {
      validationError.value = formatApiError(err, '该版本无法被激活（校验失败）。')
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
.repeat-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.row-hint {
  margin-bottom: 8px;
}
.keyword-row {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}
.keyword-block {
  flex: 1;
  min-width: 220px;
}
.save-bar {
  margin-top: 20px;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #667085;
}
</style>
