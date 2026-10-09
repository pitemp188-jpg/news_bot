/**
 * 模块: web/src/api/client.ts
 * 职责: 唯一允许发 HTTP 请求的模块——CSRF 令牌管理、统一错误、各业务接口封装
 */

export interface Task {
  id: number
  kind: string
  query: string
  status: string
  platform: string | null
  chat_id: string | null
  attempts: number
  error: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface Report {
  id: number
  task_id: number | null
  content: string
  sources: { title: string; url: string }[]
  created_at: string | null
}

export interface Delivery {
  id: number
  report_id: number | null
  platform: string
  chat_id: string
  status: string
  attempts: number
  error: string | null
  sent_at: string | null
}

export interface Schedule {
  id: number
  cron: string
  topics: string[]
  platform: string
  chat_id: string
  enabled: boolean
}

export interface NewsItem {
  id: number
  url: string
  title: string
  source: string
  published_at: string | null
  fetched_at: string | null
}

export interface SystemStatus {
  platforms: Record<string, boolean>
  queue: { depth: number; running: number }
  tasks: Record<string, number>
  running: { deliveries: number; waiting_user: number }
  content: { news: number; reports: number; schedules: number }
  today_tokens: { prompt: number; completion: number }
  timezone: string
}

export interface TaskDetail {
  task: Task
  reports: Report[]
  deliveries: Delivery[]
}

export interface Page<T> {
  total?: number
  items: T[]
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

const CSRF_COOKIE = 'nb_csrf'

/** 从 Cookie 读取 CSRF 令牌；后端用双提交校验，写操作必须带上。 */
function readCsrf(): string {
  const hit = document.cookie.split('; ').find((row) => row.startsWith(`${CSRF_COOKIE}=`))
  return hit ? decodeURIComponent(hit.slice(CSRF_COOKIE.length + 1)) : ''
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET' && method !== 'HEAD') headers['X-CSRF-Token'] = readCsrf()

  const response = await fetch(url, {
    method,
    headers,
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`
    try {
      const payload = await response.json()
      if (typeof payload?.detail === 'string') detail = payload.detail
    } catch {
      /* 非 JSON 响应保留默认提示 */
    }
    throw new ApiError(detail, response.status)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export interface Health {
  ok: boolean
  version: string
  platforms: Record<string, boolean>
  queue: { depth: number; running: number }
}

export const api = {
  // ── 登录 ──
  health: () => request<Health>('GET', '/api/health'),
  login: (password: string) => request<{ ok: boolean }>('POST', '/api/login', { password }),
  logout: () => request<{ ok: boolean }>('POST', '/api/logout'),
  me: () => request<{ authenticated: boolean; timezone: string }>('GET', '/api/me'),

  // ── 任务 ──
  tasks: (params: { status?: string; kind?: string; limit?: number } = {}) => {
    const search = new URLSearchParams()
    if (params.status) search.set('status', params.status)
    if (params.kind) search.set('kind', params.kind)
    search.set('limit', String(params.limit ?? 50))
    return request<Page<Task>>('GET', `/api/tasks?${search}`)
  },
  task: (id: number) => request<TaskDetail>('GET', `/api/tasks/${id}`),
  submitTask: (payload: { query: string; platform?: string; chat_id?: string }) =>
    request<Task>('POST', '/api/tasks', payload),
  cancelTask: (id: number) => request<Task>('POST', `/api/tasks/${id}/cancel`),
  retryTask: (id: number) => request<Task>('POST', `/api/tasks/${id}/retry`),

  // ── 订阅 ──
  schedules: () => request<Page<Schedule>>('GET', '/api/schedules'),
  createSchedule: (payload: {
    topics: string[]
    time?: string
    cron?: string
    platform: string
    chat_id: string
  }) => request<Schedule>('POST', '/api/schedules', payload),
  updateSchedule: (id: number, payload: Partial<Pick<Schedule, 'topics' | 'enabled'> & { time: string }>) =>
    request<Schedule>('PATCH', `/api/schedules/${id}`, payload),
  deleteSchedule: (id: number, purge = false) =>
    request<Schedule | { deleted: number }>('DELETE', `/api/schedules/${id}?purge=${purge}`),

  // ── 内容 ──
  news: (params: { keyword?: string; days?: number; limit?: number } = {}) => {
    const search = new URLSearchParams()
    if (params.keyword) search.set('keyword', params.keyword)
    search.set('days', String(params.days ?? 7))
    search.set('limit', String(params.limit ?? 50))
    return request<Page<NewsItem>>('GET', `/api/news?${search}`)
  },
  reports: (limit = 20) => request<Page<Report>>('GET', `/api/reports?limit=${limit}`),
  deliveries: (status?: string) => {
    const search = new URLSearchParams()
    if (status) search.set('status', status)
    return request<Page<Delivery>>('GET', `/api/deliveries?${search}`)
  },

  // ── 系统 ──
  status: () => request<SystemStatus>('GET', '/api/system/status'),
  push: (payload: { text: string; platform?: string; chat_id?: string }) =>
    request<{ ok: boolean }>('POST', '/api/system/notify', payload),
}
