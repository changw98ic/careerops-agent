import { createRouter, createWebHistory } from 'vue-router'
import { status, checkSession } from './stores/session.js'

const Dashboard = () => import('./views/Dashboard.vue')
const Login = () => import('./views/Login.vue')
const Jobs = () => import('./views/Jobs.vue')
const JobDetail = () => import('./views/JobDetail.vue')
const Companies = () => import('./views/Companies.vue')
const Applications = () => import('./views/Applications.vue')
const Profile = () => import('./views/Profile.vue')
const Resumes = () => import('./views/Resumes.vue')
const Evidence = () => import('./views/Evidence.vue')
const NotFound = () => import('./views/NotFound.vue')

const routes = [
  { path: '/login', name: 'login', component: Login },
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', name: 'dashboard', component: Dashboard, meta: { auth: true, title: 'Overview' } },
  { path: '/jobs', name: 'jobs', component: Jobs, meta: { auth: true } },
  { path: '/jobs/:id', name: 'job-detail', component: JobDetail, meta: { auth: true } },
  { path: '/companies', name: 'companies', component: Companies, meta: { auth: true } },
  { path: '/applications', name: 'applications', component: Applications, meta: { auth: true } },
  { path: '/profile', name: 'profile', component: Profile, meta: { auth: true } },
  { path: '/resumes', name: 'resumes', component: Resumes, meta: { auth: true } },
  { path: '/evidence', name: 'evidence', component: Evidence, meta: { auth: true } },
  { path: '/bootstrap', name: 'bootstrap', component: () => import('./views/Bootstrap.vue') },
  { path: '/404', name: 'not-found', component: NotFound },
  { path: '/:pathMatch(.*)*', name: 'catch-all', redirect: '/404' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

let sessionReady = false

router.beforeEach(async (to) => {
  // Wait for session check on first navigation
  if (!sessionReady && status.value === 'unknown') {
    await checkSession()
    sessionReady = true
  }

  // Authenticated user hitting /login -> redirect to dashboard
  if (to.name === 'login' && status.value === 'authenticated') {
    return { name: 'dashboard' }
  }

  // Protected route with no session -> redirect to login
  if (to.meta.auth && status.value !== 'authenticated') {
    return { name: 'login' }
  }
})

export default router
