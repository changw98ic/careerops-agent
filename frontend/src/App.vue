<template>
  <a-layout class="app-shell">
    <!-- mobile backdrop -->
    <div v-if="isMobile && mobileOpen" class="sider-backdrop" @click="closeMobileSider" />

    <a-layout-sider
      :collapsed="isMobile ? false : collapsed"
      collapsible
      :trigger="null"
      class="app-sider"
      :class="{ 'sider-mobile-overlay': isMobile }"
      :style="siderVisible ? {} : { display: 'none' }"
    >
      <div class="brand" :class="{ 'brand-collapsed': !isMobile && collapsed }">
        <div class="brand-mark">CO</div>
        <span v-if="isMobile || !collapsed" class="brand-name">CareerOps</span>
      </div>

      <a-menu :selected-keys="selectedKeys" theme="dark" mode="inline" @click="handleMenuClick">
        <a-menu-item key="dashboard">
          <template #icon><DashboardOutlined /></template>
          工作台
        </a-menu-item>
        <a-menu-item key="inbox">
          <template #icon><MailOutlined /></template>
          职位收件箱
        </a-menu-item>
        <a-menu-item key="jobs">
          <template #icon><InboxOutlined /></template>
          职位管理
        </a-menu-item>
        <a-menu-item key="companies">
          <template #icon><BankOutlined /></template>
          公司管理
        </a-menu-item>
        <a-menu-item key="applications">
          <template #icon><FileTextOutlined /></template>
          投递管理
        </a-menu-item>
        <a-menu-item key="profile">
          <template #icon><SolutionOutlined /></template>
          个人档案
        </a-menu-item>
        <a-menu-item key="resumes">
          <template #icon><FileTextOutlined /></template>
          简历管理
        </a-menu-item>
        <a-menu-item key="evidence">
          <template #icon><AuditOutlined /></template>
          证明材料
        </a-menu-item>
        <a-menu-item key="crawl-plans">
          <template #icon><CloudDownloadOutlined /></template>
          爬取计划
        </a-menu-item>
        <a-menu-item key="crawl-runs">
          <template #icon><ThunderboltOutlined /></template>
          运行历史
        </a-menu-item>
        <a-menu-item key="mail-follow-up">
          <template #icon><MailOutlined /></template>
          邮件跟进
        </a-menu-item>
        <a-menu-item key="reply-queue">
          <template #icon><MailOutlined /></template>
          回复审核
        </a-menu-item>
        <a-menu-item key="ai-workbench">
          <template #icon><RobotOutlined /></template>
          智能工作台
        </a-menu-item>
      </a-menu>

      <div v-if="isMobile || !collapsed" class="sider-footer">
        <a-tag color="blue">Review-only mode</a-tag>
        <span>External writes remain disabled.</span>
      </div>
    </a-layout-sider>

    <a-layout>
      <a-layout-header class="app-header">
        <div class="header-left">
          <a-button type="text" class="collapse-button" @click="toggleSider">
            <MenuUnfoldOutlined v-if="isMobile ? !mobileOpen : collapsed" />
            <MenuFoldOutlined v-else />
          </a-button>
          <div class="header-divider"></div>
          <span class="header-title">{{ pageTitle }}</span>
        </div>
        <CandidateSelector />
      </a-layout-header>

      <a-layout-content class="app-content">
        <!--
          Views are gated on the candidate list having settled (auth-rm Task 15
          review): child onMounted runs before App's async loadCandidates(), so
          without this gate first-visit views would fetch with an empty
          candidate id. The :key remounts the view tree when the current
          candidate changes, so every view re-fetches with the new identity
          (whole-env switch) instead of showing stale per-candidate data.
        -->
        <div v-if="candidateStatus === 'idle' || candidateStatus === 'loading'" class="candidate-gate">
          <a-spin size="large" />
        </div>
        <div v-else-if="candidateStatus === 'error'" class="candidate-gate">
          <a-empty description="候选人列表加载失败，请重试。" />
          <a-button type="primary" @click="retryCandidates">重新加载</a-button>
        </div>
        <div v-else-if="candidateStatus === 'empty'" class="candidate-gate">
          <a-empty description="还没有候选人，请点击右上角「创建候选人」开始使用。" />
        </div>
        <router-view v-else :key="currentCandidateId" />
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  AuditOutlined,
  BankOutlined,
  CloudDownloadOutlined,
  DashboardOutlined,
  FileTextOutlined,
  InboxOutlined,
  MailOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  RobotOutlined,
  SolutionOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import CandidateSelector from './components/CandidateSelector.vue'
import {
  currentCandidateId,
  load as loadCandidates,
  status as candidateStatus,
} from './stores/candidate.js'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)

// --- mobile detection ---
const MOBILE_BP = 768
const isMobile = ref(window.innerWidth <= MOBILE_BP)
const mobileOpen = ref(false) // sidebar open state on mobile

function onResize() {
  const nowMobile = window.innerWidth <= MOBILE_BP
  if (nowMobile !== isMobile.value) {
    isMobile.value = nowMobile
    if (nowMobile) {
      collapsed.value = true
      mobileOpen.value = false
    }
  }
}
window.addEventListener('resize', onResize)
onUnmounted(() => window.removeEventListener('resize', onResize))

// collapse = true on mobile by default; then settle the current candidate so
// per-candidate API calls always have an id to address. The router-view gate
// above keeps views unmounted until this resolves.
onMounted(async () => {
  if (isMobile.value) {
    collapsed.value = true
  }
  await retryCandidates()
})

async function retryCandidates() {
  try {
    await loadCandidates()
  } catch (err) {
    message.error(err.message || '加载候选人列表失败，请刷新重试。')
  }
}

const siderVisible = computed(() => !isMobile.value || mobileOpen.value)

function toggleSider() {
  if (isMobile.value) {
    mobileOpen.value = !mobileOpen.value
  } else {
    collapsed.value = !collapsed.value
  }
}

function closeMobileSider() {
  if (isMobile.value) {
    mobileOpen.value = false
  }
}

const selectedKeys = computed(() => [String(route.name || 'dashboard')])
const pageTitle = computed(() => {
  const titles = {
    dashboard: '工作台',
    inbox: '职位收件箱',
    'inbox-detail': '职位详情',
    jobs: '职位管理',
    'job-detail': '职位详情',
    companies: '公司管理',
    applications: '投递管理',
    'application-workspace': '投递工作区',
    profile: '个人档案',
    resumes: '简历管理',
    evidence: '证明材料',
    'crawl-plans': '爬取计划',
    'crawl-runs': '运行历史',
    'crawl-run-detail': '运行详情',
    'mail-follow-up': '邮件跟进',
    'reply-queue': '回复审核',
    'ai-workbench': '智能工作台',
  }
  return titles[String(route.name)] || 'CareerOps'
})

function handleMenuClick({ key }) {
  router.push({ name: String(key) })
  closeMobileSider()
}

</script>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 64px;
  background: var(--surface-primary);
  border-bottom: 1px solid rgba(0, 0, 0, 0.08);
  box-shadow: var(--shadow-sm);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.header-divider {
  width: 1px;
  height: 24px;
  background: rgba(28, 25, 23, 0.1);
}

.collapse-button {
  width: 40px;
  height: 40px;
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-tertiary);
  transition: all 0.2s ease;
}

.collapse-button:hover {
  background: rgba(0, 0, 0, 0.04);
  color: var(--color-primary);
}

.header-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-primary);
  letter-spacing: -0.01em;
}

.app-content {
  padding: 24px;
  min-height: calc(100vh - 64px);
  background: var(--surface-secondary);
}

.candidate-gate {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  min-height: 40vh;
}
</style>
