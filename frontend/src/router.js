import { createRouter, createWebHistory } from 'vue-router'
import Login from './views/Login.vue'
import Jobs from './views/Jobs.vue'
import JobDetail from './views/JobDetail.vue'
import Companies from './views/Companies.vue'
import Applications from './views/Applications.vue'

const routes = [
  { path: '/login', name: 'login', component: Login },
  { path: '/', redirect: '/jobs' },
  { path: '/jobs', name: 'jobs', component: Jobs, meta: { auth: true } },
  { path: '/jobs/:id', name: 'job-detail', component: JobDetail, meta: { auth: true } },
  { path: '/companies', name: 'companies', component: Companies, meta: { auth: true } },
  { path: '/applications', name: 'applications', component: Applications, meta: { auth: true } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  if (to.meta.auth && !document.cookie.includes('careerops_session=')) {
    return { name: 'login' }
  }
})

export default router
