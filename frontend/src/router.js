import { createRouter, createWebHistory } from 'vue-router'

const Dashboard = () => import('./views/Dashboard.vue')
const Jobs = () => import('./views/Jobs.vue')
const JobDetail = () => import('./views/JobDetail.vue')
const Companies = () => import('./views/Companies.vue')
const Applications = () => import('./views/Applications.vue')
const ApplicationWorkspace = () => import('./views/ApplicationWorkspace.vue')
const Profile = () => import('./views/Profile.vue')
const Resumes = () => import('./views/Resumes.vue')
const Evidence = () => import('./views/Evidence.vue')
const CrawlPlans = () => import('./views/CrawlPlans.vue')
const CrawlRunHistory = () => import('./views/CrawlRunHistory.vue')
const CrawlRunDetail = () => import('./views/CrawlRunDetail.vue')
const Inbox = () => import('./views/Inbox.vue')
const InboxDetail = () => import('./views/InboxDetail.vue')
const MailFollowUp = () => import('./views/MailFollowUp.vue')
const ReplyReviewQueue = () => import('./views/ReplyReviewQueue.vue')
const AgentWorkbench = () => import('./views/AgentWorkbench.vue')
const NotFound = () => import('./views/NotFound.vue')

const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', name: 'dashboard', component: Dashboard, meta: { title: 'Overview' } },
  { path: '/jobs', name: 'jobs', component: Jobs },
  { path: '/jobs/:id', name: 'job-detail', component: JobDetail },
  { path: '/companies', name: 'companies', component: Companies },
  { path: '/applications', name: 'applications', component: Applications },
  { path: '/applications/:id', name: 'application-workspace', component: ApplicationWorkspace },
  { path: '/profile', name: 'profile', component: Profile },
  { path: '/resumes', name: 'resumes', component: Resumes },
  { path: '/evidence', name: 'evidence', component: Evidence },
  { path: '/crawl-plans', name: 'crawl-plans', component: CrawlPlans },
  { path: '/crawl-runs', name: 'crawl-runs', component: CrawlRunHistory },
  { path: '/crawl-runs/:id', name: 'crawl-run-detail', component: CrawlRunDetail },
  { path: '/inbox', name: 'inbox', component: Inbox },
  { path: '/inbox/:id', name: 'inbox-detail', component: InboxDetail },
  { path: '/mail-follow-up', name: 'mail-follow-up', component: MailFollowUp, meta: { title: '邮件跟进' } },
  { path: '/reply-queue', name: 'reply-queue', component: ReplyReviewQueue, meta: { title: '回复评审' } },
  { path: '/ai-workbench', name: 'ai-workbench', component: AgentWorkbench, meta: { title: '智能工作台', allowedTabs: ['matching', 'resume', 'interview'] } },
  { path: '/404', name: 'not-found', component: NotFound },
  { path: '/:pathMatch(.*)*', name: 'catch-all', redirect: '/404' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// Exported for unit tests (task 14.1/14.8): lets the suite assert the route
// table without instantiating the browser history. Additive; the default
// export below is unchanged.
export { routes }

// Allowed tabs for the AI workbench route.  Kept in sync with the route meta
// so the guard can validate incoming `?tab=` query values and fall back to
// "matching" when the value is missing or not in the allowlist.
const AI_WORKBENCH_ALLOWED_TABS = ['matching', 'resume', 'interview']

router.beforeEach((to) => {
  // Validate `tab` query parameter for the AI workbench route.
  // context_id is passed through as-is (opaque identifier).
  if (to.name === 'ai-workbench') {
    const tab = to.query.tab
    if (tab && !AI_WORKBENCH_ALLOWED_TABS.includes(tab)) {
      return {
        name: 'ai-workbench',
        query: { ...to.query, tab: 'matching' },
        hash: to.hash,
      }
    }
  }
})

export default router
