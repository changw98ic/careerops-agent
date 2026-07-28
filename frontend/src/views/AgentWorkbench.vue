<template>
  <div class="page-shell agent-workbench">
    <div class="page-header">
      <div>
        <h1>智能工作台</h1>
        <p>在人工确认边界内运行职位匹配、简历审查和面试准备。</p>
      </div>
      <a-tag color="purple">仅审查，不自动发送</a-tag>
    </div>

    <a-alert
      type="info"
      show-icon
      message="AI 结果必须人工确认"
      description="页面接入真实 agent/matching 接口；模型能力由后端发布开关决定，关闭或不可用时只返回安全降级结果，不会伪装成模型结论。"
    />

    <CandidateSetupNotice :visible="candidateProfileRequired" />
    <a-alert
      v-if="error"
      type="error"
      show-icon
      :message="error"
      closable
      @close="error = ''"
    />

    <a-tabs v-model:activeKey="activeTab" class="workbench-tabs">
      <a-tab-pane key="matching" tab="职位匹配">
        <a-card title="运行确定性匹配" class="workbench-card">
          <p class="card-hint">匹配由规则和确认后的候选人证据驱动，不调用大模型。</p>
          <a-form layout="vertical">
            <a-form-item label="目标职位">
              <a-select
                v-model:value="selectedJobId"
                show-search
                allow-clear
                placeholder="选择一个职位"
                :loading="loading.jobs"
                :disabled="jobs.length === 0"
                option-filter-prop="label"
                @change="onJobChange"
              >
                <a-select-option v-for="job in jobs" :key="job.id" :value="job.id" :label="job.canonical_title">
                  {{ job.canonical_title }}
                </a-select-option>
              </a-select>
            </a-form-item>
          </a-form>
          <a-space>
            <a-button type="primary" :loading="loading.match" :disabled="!canRunMatch" @click="runMatching">
              运行匹配
            </a-button>
            <a-button :loading="loading.matches" @click="loadMatches">刷新结果</a-button>
          </a-space>

          <a-divider />
          <div v-if="matches.length" class="result-list">
            <button
              v-for="match in matches"
              :key="match.id"
              class="result-row"
              type="button"
              @click="selectMatch(match)"
            >
              <span>
                <strong>{{ jobTitle(match.canonical_job_id) }}</strong>
                <small>{{ match.tier }} · {{ formatScore(match.overall_score) }}</small>
              </span>
              <a-tag :color="match.geographic_blocked ? 'red' : 'green'">
                {{ match.geographic_blocked ? '地域受限' : match.remote_verdict || '可评估' }}
              </a-tag>
            </button>
          </div>
          <a-empty v-else-if="!loading.matches" description="暂无匹配结果；选择职位后运行一次匹配。" />
          <a-spin v-else />
        </a-card>

        <a-card v-if="selectedMatch" title="匹配明细" class="workbench-card result-card">
          <div class="score-line">
            <span class="score-value">{{ formatScore(selectedMatch.overall_score) }}</span>
            <span class="muted">{{ selectedMatch.tier }}</span>
          </div>
          <a-list
            v-if="selectedMatch.requirement_matches?.length"
            size="small"
            :data-source="selectedMatch.requirement_matches"
          >
            <template #renderItem="{ item }">
              <a-list-item>
                <a-list-item-meta :title="item.requirement_name" :description="item.reason || '无附加说明'" />
                <a-tag>{{ item.level }} · {{ formatScore(item.confidence) }}</a-tag>
              </a-list-item>
            </template>
          </a-list>
          <a-empty v-else description="该结果没有逐项要求明细。" />
        </a-card>
      </a-tab-pane>

      <a-tab-pane key="resume" tab="简历审查">
        <a-card title="基于确认资料审查简历" class="workbench-card">
          <p class="card-hint">输入必须来自已解析、已确认的简历和确认后的证据；结果仍需人工审核。</p>
          <AgentInputForm
            v-model:job-id="selectedJobId"
            v-model:job-version-id="selectedJobVersionId"
            v-model:resume-id="selectedResumeId"
            v-model:profile-version-id="selectedProfileVersionId"
            v-model:evidence-ids="selectedEvidenceIds"
            :jobs="jobs"
            :job-versions="jobVersions"
            :resumes="resumes"
            :profile-versions="profileVersions"
            :evidence="evidence"
            :loading="loading.inputs"
            @job-change="onJobChange"
          />
          <a-button type="primary" :loading="loading.resume" :disabled="!canStartAgent" @click="startResumeReview">
            开始简历审查
          </a-button>
        </a-card>
        <AgentResultCard :run="latestRunFor('resume_review')" @review="reviewRun" />
      </a-tab-pane>

      <a-tab-pane key="interview" tab="面试准备">
        <a-card title="生成面试练习材料" class="workbench-card">
          <p class="card-hint">材料只用于准备和练习，页面不会替用户联系任何外部对象。</p>
          <AgentInputForm
            v-model:job-id="selectedJobId"
            v-model:job-version-id="selectedJobVersionId"
            v-model:resume-id="selectedResumeId"
            v-model:profile-version-id="selectedProfileVersionId"
            v-model:evidence-ids="selectedEvidenceIds"
            :jobs="jobs"
            :job-versions="jobVersions"
            :resumes="resumes"
            :profile-versions="profileVersions"
            :evidence="evidence"
            :loading="loading.inputs"
            :show-context="true"
            v-model:user-context="userContext"
            @job-change="onJobChange"
          />
          <SmartIntakePanel
            target="interview_context"
            title="用自然语言整理面试上下文"
            :context="{
              canonical_job_id: selectedJobId,
              job_version_id: selectedJobVersionId,
              resume_version_id: selectedResumeId,
              profile_version_id: selectedProfileVersionId || null,
              evidence_ids: selectedEvidenceIds,
            }"
            :base-snapshot="{ user_context: userContext }"
            :current-snapshot="{ user_context: userContext }"
            :feature-enabled="smartIntakeUiEnabled"
            @applied="applySmartInterviewPatch"
          />
          <a-button type="primary" :loading="loading.interview" :disabled="!canStartAgent" @click="startInterviewPreparation">
            开始面试准备
          </a-button>
        </a-card>
        <AgentResultCard :run="latestRunFor('interview_preparation')" @review="reviewRun" />
      </a-tab-pane>

      <a-tab-pane key="history" tab="运行历史">
        <a-card title="Agent 运行记录" class="workbench-card">
          <div class="history-toolbar">
            <span class="muted">每条记录包含输入身份、状态和人工审核结果。</span>
            <a-button :loading="loading.runs" @click="loadRuns">刷新</a-button>
          </div>
          <div v-if="runs.length" class="result-list">
            <button v-for="run in runs" :key="run.id" type="button" class="result-row" @click="selectRun(run)">
              <span>
                <strong>{{ capabilityLabel(run.capability) }}</strong>
                <small>{{ formatDate(run.created_at) }} · {{ run.model_id || 'disabled' }}</small>
              </span>
              <a-space>
                <a-tag :color="stateColor(run.state)">{{ stateLabel(run.state) }}</a-tag>
                <a-tag v-if="run.review_decision" color="blue">{{ run.review_decision }}</a-tag>
              </a-space>
            </button>
          </div>
          <a-empty v-else-if="!loading.runs" description="暂无 Agent 运行记录。" />
          <a-spin v-else />
        </a-card>
        <AgentResultCard :run="selectedRun" @review="reviewRun" />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { api, formatApiError, parseApiError, smartIntakeUiEnabled } from '../api/client.js'
import { user } from '../stores/session.js'
import CandidateSetupNotice from '../components/CandidateSetupNotice.vue'
import SmartIntakePanel from '../components/SmartIntakePanel.vue'

const activeTab = ref('matching')
const jobs = ref([])
const jobVersions = ref([])
const resumes = ref([])
const profileVersions = ref([])
const evidence = ref([])
const matches = ref([])
const runs = ref([])
const selectedMatch = ref(null)
const selectedRun = ref(null)
const selectedJobId = ref('')
const selectedJobVersionId = ref('')
const selectedResumeId = ref('')
const selectedProfileVersionId = ref('')
const selectedEvidenceIds = ref([])
const userContext = ref('')
const candidateProfileRequired = ref(false)
const error = ref('')
const loading = reactive({
  jobs: false,
  inputs: false,
  matches: false,
  match: false,
  runs: false,
  resume: false,
  interview: false,
})

const canRunMatch = computed(() => Boolean(user.candidate_id && selectedJobId.value))
const canStartAgent = computed(
  () =>
    Boolean(
      user.candidate_id &&
        selectedJobId.value &&
        selectedJobVersionId.value &&
        selectedResumeId.value,
    ),
)

function listItems(data) {
  return Array.isArray(data?.items) ? data.items : []
}

async function callApi(fn, fallback = '加载失败，请稍后重试。') {
  try {
    return await fn()
  } catch (err) {
    const info = parseApiError(err)
    if (info.isCandidateProfileRequired) {
      candidateProfileRequired.value = true
    } else if (!error.value) {
      error.value = formatApiError(err, fallback)
    }
    return null
  }
}

async function loadJobs() {
  loading.jobs = true
  const data = await callApi(() => api.listJobs({ limit: 200 }), '加载职位失败，请稍后重试。')
  if (data) {
    jobs.value = listItems(data)
    if (!selectedJobId.value && jobs.value[0]) {
      await onJobChange(jobs.value[0].id)
    }
  }
  loading.jobs = false
}

async function loadCandidateInputs() {
  loading.inputs = true
  const [resumeData, profileData, evidenceData] = await Promise.all([
    callApi(() => api.listEligibleResumes(), '加载可用简历失败，请稍后重试。'),
    callApi(() => api.listProfileVersions({ limit: 100 }), '加载画像版本失败，请稍后重试。'),
    callApi(() => api.listEvidence({ status: 'confirmed', limit: 200 }), '加载确认证据失败，请稍后重试。'),
  ])
  if (resumeData) {
    resumes.value = listItems(resumeData)
    if (!selectedResumeId.value && resumes.value[0]) selectedResumeId.value = resumes.value[0].id
  }
  if (profileData) {
    profileVersions.value = listItems(profileData)
    const active = profileVersions.value.find((item) => item.is_active)
    selectedProfileVersionId.value = active?.id || profileVersions.value[0]?.id || ''
  }
  if (evidenceData) evidence.value = listItems(evidenceData).filter((item) => item.confirmation_status === 'confirmed')
  loading.inputs = false
}

async function loadJobVersions(jobId = selectedJobId.value) {
  jobVersions.value = []
  selectedJobVersionId.value = ''
  if (!jobId) return
  const data = await callApi(() => api.getJob(jobId), '加载职位版本失败，请稍后重试。')
  if (data) {
    jobVersions.value = Array.isArray(data.versions) ? data.versions : []
    selectedJobVersionId.value = jobVersions.value[0]?.id || ''
  }
}

async function onJobChange(jobId) {
  selectedJobId.value = jobId || ''
  await loadJobVersions(jobId)
}

async function loadMatches() {
  loading.matches = true
  const data = await callApi(() => api.listMatches({ limit: 100 }), '加载匹配结果失败，请稍后重试。')
  if (data) matches.value = listItems(data)
  loading.matches = false
}

async function runMatching() {
  if (!canRunMatch.value) return
  loading.match = true
  error.value = ''
  const result = await callApi(
    () => api.runMatch({ candidate_id: user.candidate_id, canonical_job_id: selectedJobId.value }),
    '运行匹配失败，请稍后重试。',
  )
  if (result) {
    matches.value = [result, ...matches.value.filter((item) => item.id !== result.id)]
    selectedMatch.value = result
  }
  loading.match = false
}

function agentPayload() {
  return {
    resume_version_id: selectedResumeId.value,
    canonical_job_id: selectedJobId.value,
    job_version_id: selectedJobVersionId.value,
    profile_version_id: selectedProfileVersionId.value || undefined,
    evidence_ids: selectedEvidenceIds.value,
    user_context: userContext.value,
  }
}

async function startResumeReview() {
  if (!canStartAgent.value) return
  loading.resume = true
  error.value = ''
  const result = await callApi(() => api.startResumeReview(agentPayload()), '简历审查启动失败，请稍后重试。')
  if (result) {
    selectedRun.value = result
    runs.value = [result, ...runs.value.filter((item) => item.id !== result.id)]
  }
  loading.resume = false
}

async function startInterviewPreparation() {
  if (!canStartAgent.value) return
  loading.interview = true
  error.value = ''
  const result = await callApi(
    () => api.startInterviewPreparation(agentPayload()),
    '面试准备启动失败，请稍后重试。',
  )
  if (result) {
    selectedRun.value = result
    runs.value = [result, ...runs.value.filter((item) => item.id !== result.id)]
  }
  loading.interview = false
}

function applySmartInterviewPatch({ patch, baseline }) {
  const next = patch?.user_context
  if (typeof next !== 'string') return
  if (baseline?.user_context !== userContext.value) {
    message.warning('面试上下文在预览后已被修改，已保留当前内容。')
    return
  }
  userContext.value = next
  message.success('智能建议已写入面试上下文；请检查后再启动准备。')
}

async function loadRuns() {
  loading.runs = true
  const data = await callApi(() => api.listAgentRuns({ limit: 100 }), '加载 Agent 历史失败，请稍后重试。')
  if (data) runs.value = listItems(data)
  loading.runs = false
}

async function reviewRun(run, decision) {
  if (!run?.id) return
  const result = await callApi(
    () => api.reviewAgentRun(run.id, { decision, note: '页面人工审核', edited_result: {} }),
    '保存审核结果失败，请稍后重试。',
  )
  if (result) {
    selectedRun.value = result
    runs.value = runs.value.map((item) => (item.id === result.id ? result : item))
  }
}

function selectMatch(match) {
  selectedMatch.value = match
}

function selectRun(run) {
  selectedRun.value = run
}

function latestRunFor(capability) {
  return runs.value.find((run) => run.capability === capability) || null
}

function jobTitle(jobId) {
  return jobs.value.find((job) => job.id === jobId)?.canonical_title || jobId
}

function formatScore(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : '—'
}

function formatDate(value) {
  if (!value) return '时间未知'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function capabilityLabel(value) {
  return { resume_review: '简历审查', interview_preparation: '面试准备', job_matching: '职位匹配' }[value] || value
}

function stateLabel(value) {
  return {
    pending: '排队中', running: '运行中', succeeded: '已完成', unavailable: '模型关闭',
    abstained: '已弃答', failed: '失败', reviewed: '已审核', stale: '已过期',
  }[value] || value
}

function stateColor(value) {
  if (value === 'succeeded' || value === 'reviewed') return 'green'
  if (value === 'unavailable') return 'orange'
  if (value === 'failed' || value === 'abstained') return 'red'
  return 'blue'
}

onMounted(async () => {
  await loadJobs()
  await Promise.all([loadCandidateInputs(), loadMatches(), loadRuns()])
})
</script>

<script>
import { defineComponent, h } from 'vue'

const AgentInputForm = defineComponent({
  name: 'AgentInputForm',
  props: {
    jobId: { type: String, default: '' },
    jobVersionId: { type: String, default: '' },
    resumeId: { type: String, default: '' },
    profileVersionId: { type: String, default: '' },
    evidenceIds: { type: Array, default: () => [] },
    userContext: { type: String, default: '' },
    jobs: { type: Array, default: () => [] },
    jobVersions: { type: Array, default: () => [] },
    resumes: { type: Array, default: () => [] },
    profileVersions: { type: Array, default: () => [] },
    evidence: { type: Array, default: () => [] },
    loading: { type: Boolean, default: false },
    showContext: { type: Boolean, default: false },
  },
  emits: ['update:jobId', 'update:jobVersionId', 'update:resumeId', 'update:profileVersionId', 'update:evidenceIds', 'update:userContext', 'jobChange'],
  setup(props, { emit }) {
    return () => h('div', { class: 'agent-input-form' }, [
      h('div', { class: 'input-grid' }, [
        h('label', { class: 'native-field' }, [h('span', '目标职位'), h('select', {
          value: props.jobId,
          disabled: props.loading || !props.jobs.length,
          onChange: (event) => emit('jobChange', event.target.value),
        }, [h('option', { value: '' }, '请选择职位'), ...props.jobs.map((job) => h('option', { key: job.id, value: job.id }, job.canonical_title))])]),
        h('label', { class: 'native-field' }, [h('span', '职位版本'), h('select', {
          value: props.jobVersionId,
          disabled: props.loading || !props.jobVersions.length,
          onChange: (event) => emit('update:jobVersionId', event.target.value),
        }, [h('option', { value: '' }, '请选择职位版本'), ...props.jobVersions.map((version) => h('option', { key: version.id, value: version.id }, `${version.structured_data?.title || '职位版本'} · ${version.captured_at || '时间未知'}`))])]),
        h('label', { class: 'native-field' }, [h('span', '已确认简历'), h('select', {
          value: props.resumeId,
          disabled: props.loading || !props.resumes.length,
          onChange: (event) => emit('update:resumeId', event.target.value),
        }, [h('option', { value: '' }, '请选择已确认简历'), ...props.resumes.map((resume) => h('option', { key: resume.id, value: resume.id }, `v${resume.version_number} · ${resume.target_type || 'general'}`))])]),
        h('label', { class: 'native-field' }, [h('span', '画像版本（可选）'), h('select', {
          value: props.profileVersionId,
          disabled: props.loading,
          onChange: (event) => emit('update:profileVersionId', event.target.value),
        }, [h('option', { value: '' }, '使用当前激活版本'), ...props.profileVersions.map((profile) => h('option', { key: profile.id, value: profile.id }, `v${profile.version}${profile.is_active ? ' · 激活' : ''}`))])]),
      ]),
      h('label', { class: 'native-field' }, [h('span', '确认后的证据（可选；不选则自动读取该简历的全部确认证据）'), h('select', {
        multiple: true,
        value: props.evidenceIds,
        disabled: props.loading || !props.evidence.length,
        onChange: (event) => emit('update:evidenceIds', Array.from(event.target.selectedOptions, (option) => option.value)),
      }, props.evidence.map((item) => h('option', { key: item.id, value: item.id }, item.name)))]),
      props.showContext ? h('label', { class: 'native-field' }, [h('span', '用户补充上下文（非验证事实）'), h('textarea', {
        value: props.userContext,
        rows: 3,
        maxlength: 2000,
        placeholder: '例如：我想重点练习系统设计表达。',
        onInput: (event) => emit('update:userContext', event.target.value),
      })]) : null,
    ])
  },
})

const AgentResultCard = defineComponent({
  name: 'AgentResultCard',
  props: { run: { type: Object, default: null } },
  emits: ['review'],
  setup(props, { emit }) {
    const pretty = (value) => JSON.stringify(value || {}, null, 2)
    return () => props.run ? h('section', { class: 'workbench-card agent-result-card' }, [
      h('div', { class: 'result-card-header' }, [
        h('div', [h('h3', '最近一次结果'), h('span', { class: 'muted' }, `${props.run.state} · ${props.run.model_id || 'disabled'}`)]),
        h('div', { class: 'review-actions' }, [
          h('button', { type: 'button', class: 'review-button', onClick: () => emit('review', props.run, 'accepted') }, '接受'),
          h('button', { type: 'button', class: 'review-button review-button--quiet', onClick: () => emit('review', props.run, 'edited') }, '标记已编辑'),
          h('button', { type: 'button', class: 'review-button review-button--danger', onClick: () => emit('review', props.run, 'rejected') }, '拒绝'),
        ]),
      ]),
      h('p', { class: 'result-note' }, props.run.result?.status === 'unavailable' ? '模型关闭：以下是安全降级的人工核对提示，不是模型判断。' : '结果来自真实 Agent 接口，请在使用前完成人工审核。'),
      h('pre', { class: 'result-json' }, pretty(props.run.result)),
    ]) : h('section', { class: 'workbench-card empty-result' }, [h('span', { class: 'muted' }, '运行后将在这里显示可审查结果。')])
  },
})

export default { components: { AgentInputForm, AgentResultCard } }
</script>

<style scoped>
.agent-workbench {
  max-width: 1180px;
}

.workbench-tabs {
  margin-top: 20px;
}

.workbench-card {
  margin-top: 16px;
}

.card-hint,
.result-note {
  color: var(--color-tertiary);
  font-size: 13px;
}

.result-list {
  display: grid;
  gap: 8px;
}

.result-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 16px;
  padding: 12px 14px;
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-radius: 8px;
  background: var(--surface-primary);
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.result-row:hover {
  border-color: var(--accent-primary);
  background: #fffaf5;
}

.result-row span:first-child {
  display: grid;
  gap: 4px;
}

.result-row small {
  color: var(--color-tertiary);
}

.score-line {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}

.score-value {
  color: var(--accent-primary);
  font-size: 32px;
  font-weight: 750;
}

.history-toolbar,
.result-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.agent-result-card {
  padding: 20px;
}

.result-card-header h3 {
  margin: 0 0 4px;
}

.review-actions {
  display: flex;
  gap: 8px;
}

.review-button {
  border: 1px solid #1677ff;
  border-radius: 6px;
  padding: 6px 10px;
  background: #1677ff;
  color: white;
  cursor: pointer;
}

.review-button--quiet {
  background: white;
  color: #1677ff;
}

.review-button--danger {
  border-color: #d4380d;
  background: white;
  color: #d4380d;
}

.result-json {
  max-height: 360px;
  overflow: auto;
  padding: 14px;
  border-radius: 8px;
  background: #1f1f1f;
  color: #f5f5f5;
  font-size: 12px;
  white-space: pre-wrap;
}

.empty-result {
  min-height: 72px;
  display: grid;
  place-items: center;
}

:deep(.ant-card-head-title) {
  font-weight: 650;
}

:global(.agent-input-form) {
  display: grid;
  gap: 16px;
  margin-bottom: 18px;
}

:global(.input-grid) {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

:global(.native-field) {
  display: grid;
  gap: 6px;
  color: var(--color-secondary);
  font-size: 13px;
}

:global(.native-field select),
:global(.native-field textarea) {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #d9d9d9;
  border-radius: 6px;
  padding: 8px 10px;
  background: white;
  color: var(--color-primary);
  font: inherit;
}

:global(.native-field select[multiple]) {
  min-height: 84px;
}

:global(.native-field select:focus),
:global(.native-field textarea:focus) {
  border-color: #1677ff;
  outline: 2px solid rgba(22, 119, 255, 0.15);
}

@media (max-width: 720px) {
  .input-grid {
    grid-template-columns: 1fr;
  }

  .result-card-header,
  .history-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .review-actions {
    width: 100%;
  }

  .review-button {
    flex: 1;
  }
}
</style>
