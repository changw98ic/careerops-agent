<template>
  <div v-if="!sessionReady" class="session-loading">
    <a-spin size="large" />
  </div>

  <router-view v-else-if="isLoginPage" />

  <a-layout v-else class="app-shell">
    <!-- mobile backdrop -->
    <div v-if="isMobile && mobileOpen" class="sider-backdrop" @click="closeMobileSider" />

    <a-layout-sider
      v-model:collapsed="collapsed"
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
          Overview
        </a-menu-item>
        <a-menu-item key="inbox">
          <template #icon><MailOutlined /></template>
          Inbox
        </a-menu-item>
        <a-menu-item key="jobs">
          <template #icon><InboxOutlined /></template>
          Jobs
        </a-menu-item>
        <a-menu-item key="companies">
          <template #icon><BankOutlined /></template>
          Companies
        </a-menu-item>
        <a-menu-item key="applications">
          <template #icon><FileTextOutlined /></template>
          Applications
        </a-menu-item>
        <a-menu-item key="profile">
          <template #icon><SolutionOutlined /></template>
          Profile
        </a-menu-item>
        <a-menu-item key="resumes">
          <template #icon><FileTextOutlined /></template>
          Resumes
        </a-menu-item>
        <a-menu-item key="evidence">
          <template #icon><AuditOutlined /></template>
          Evidence
        </a-menu-item>
        <a-menu-item key="crawl-plans">
          <template #icon><CloudDownloadOutlined /></template>
          Crawl Plans
        </a-menu-item>
        <a-menu-item key="crawl-runs">
          <template #icon><ThunderboltOutlined /></template>
          Run History
        </a-menu-item>
      </a-menu>

      <div v-if="isMobile || !collapsed" class="sider-footer">
        <a-tag color="blue">Review-only mode</a-tag>
        <span>External writes remain disabled.</span>
      </div>
    </a-layout-sider>

    <a-layout>
      <a-layout-header class="app-header">
        <a-space size="middle">
          <a-button type="text" class="collapse-button" @click="toggleSider">
            <MenuUnfoldOutlined v-if="isMobile ? !mobileOpen : collapsed" />
            <MenuFoldOutlined v-else />
          </a-button>
          <span class="header-title">{{ pageTitle }}</span>
        </a-space>

        <a-space size="middle">
          <a-tag color="green">Safe workspace</a-tag>
          <a-avatar :size="32"><template #icon><UserOutlined /></template></a-avatar>
          <span class="header-username">{{ user.username }}</span>
          <a-button type="text" class="logout-button" :loading="loggingOut" @click="logout">
            <LogoutOutlined /> Logout
          </a-button>
        </a-space>
      </a-layout-header>

      <a-layout-content class="app-content">
        <router-view />
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  AuditOutlined,
  BankOutlined,
  CloudDownloadOutlined,
  DashboardOutlined,
  FileTextOutlined,
  InboxOutlined,
  LogoutOutlined,
  MailOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  SolutionOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from '@ant-design/icons-vue'
import { status, user, checkSession, logout as sessionLogout } from './stores/session.js'
import { message } from 'ant-design-vue'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)
const loggingOut = ref(false)

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

// collapse = true on mobile by default
onMounted(() => {
  if (status.value === 'unknown') {
    checkSession()
  }
  if (isMobile.value) {
    collapsed.value = true
  }
})

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

const isLoginPage = computed(() => route.name === 'login')

// Block rendering until session check completes
const sessionReady = computed(() => status.value !== 'unknown' && status.value !== 'checking')
const selectedKeys = computed(() => [String(route.name || 'dashboard')])
const pageTitle = computed(() => {
  const titles = {
    dashboard: 'Overview',
    inbox: 'Job Inbox',
    'inbox-detail': 'Job detail',
    jobs: 'Jobs',
    'job-detail': 'Job detail',
    companies: 'Companies',
    applications: 'Applications',
    'application-workspace': 'Application Workspace',
    profile: 'Profile',
    resumes: 'Resumes',
    evidence: 'Evidence',
    'crawl-plans': 'Crawl Plans',
    'crawl-runs': 'Run History',
    'crawl-run-detail': 'Run Detail',
  }
  return titles[String(route.name)] || 'CareerOps'
})

function handleMenuClick({ key }) {
  router.push({ name: String(key) })
  closeMobileSider()
}

async function logout() {
  loggingOut.value = true
  try {
    await sessionLogout()
  } catch (err) {
    message.error(err.message || 'Logout failed')
    loggingOut.value = false
  }
}

</script>

<style scoped>
.session-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
}
.header-username {
  font-size: 14px;
  color: rgba(0, 0, 0, 0.65);
}
</style>
