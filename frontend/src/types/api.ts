/**
 * API 响应类型定义
 */

// 基础响应类型
export interface ApiResponse<T = unknown> {
  ok: boolean
  data?: T
  error?: string
  code?: string
  trace_id?: string
}

// 分页响应
export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

// 职位相关类型
export interface Job {
  id: string
  canonical_job_id: string
  title: string
  company_name: string
  location: string
  description: string
  aggregate_state: JobState
  created_at: string
  crawled_at: string
  updated_at: string
}

export type JobState =
  | 'active'
  | 'closed'
  | 'expired'
  | 'unknown'

// 投递相关类型
export interface Application {
  id: string
  canonical_job_id: string
  state: ApplicationState
  submitted_at: string | null
  created_at: string
  updated_at: string
}

export type ApplicationState =
  | 'favorited'
  | 'preparing'
  | 'submitted'
  | 'interviewing'
  | 'offered'
  | 'rejected'
  | 'ignored'

// 公司相关类型
export interface Company {
  id: string
  name: string
  website: string
  industry: string
  size: string
  created_at: string
  updated_at: string
}

// 统计数据类型
export interface DashboardMetrics {
  jobs: number
  activeJobs: number
  applications: number
  submitted: number
}

// 图表数据类型
export interface ChartData {
  statusDist: StatusDistribution[]
  trend: TrendData[]
  funnel: FunnelData[]
  timeline: TimelineData[]
}

export interface StatusDistribution {
  state: string
  count: number
}

export interface TrendData {
  date: string
  count: number
}

export interface FunnelData {
  state: string
  count: number
}

export interface TimelineData {
  date: string
  state: string
  id: string
}

// 爬取相关类型
export interface CrawlSource {
  id: string
  name: string
  url: string
  type: string
  status: CrawlStatus
  last_crawled_at: string | null
  created_at: string
  updated_at: string
}

export type CrawlStatus =
  | 'active'
  | 'paused'
  | 'error'
  | 'disabled'

export interface CrawlRun {
  id: string
  source_id: string
  status: CrawlRunStatus
  started_at: string
  completed_at: string | null
  jobs_found: number
  jobs_new: number
  errors: number
}

export type CrawlRunStatus =
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'

// 邮件相关类型
export interface MailThread {
  id: string
  subject: string
  snippet: string
  last_message_at: string
  unread: boolean
  messages: MailMessage[]
}

export interface MailMessage {
  id: string
  thread_id: string
  from: string
  to: string[]
  subject: string
  body: string
  received_at: string
  sent_at: string | null
}

// 简历相关类型
export interface Resume {
  id: string
  filename: string
  target_type: string
  source_reference: string
  status: ResumeStatus
  created_at: string
  updated_at: string
}

export type ResumeStatus =
  | 'pending'
  | 'confirmed'
  | 'rejected'

// 证明材料相关类型
export interface Evidence {
  id: string
  type: string
  content: string
  source: string
  status: EvidenceStatus
  created_at: string
  updated_at: string
}

export type EvidenceStatus =
  | 'pending'
  | 'confirmed'
  | 'rejected'

// 响应包装类型
export type JobResponse = ApiResponse<Job>
export type JobsResponse = ApiResponse<PaginatedResponse<Job>>
export type ApplicationResponse = ApiResponse<Application>
export type ApplicationsResponse = ApiResponse<PaginatedResponse<Application>>
export type CompanyResponse = ApiResponse<Company>
export type CompaniesResponse = ApiResponse<PaginatedResponse<Company>>
export type CrawlSourcesResponse = ApiResponse<PaginatedResponse<CrawlSource>>
export type CrawlRunsResponse = ApiResponse<PaginatedResponse<CrawlRun>>
export type MailThreadsResponse = ApiResponse<PaginatedResponse<MailThread>>
export type ResumesResponse = ApiResponse<PaginatedResponse<Resume>>
export type EvidenceResponse = ApiResponse<PaginatedResponse<Evidence>>
