/**
 * 模块: web/src/api/client.spec.ts
 * 职责: 校验 API 客户端——CSRF 头注入、错误提炼、查询参数拼装
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './client'

const calls: { url: string; init: RequestInit }[] = []

function mockFetch(response: { status?: number; body?: unknown; throws?: boolean }) {
  calls.length = 0
  vi.stubGlobal('fetch', (url: string, init: RequestInit = {}) => {
    calls.push({ url, init })
    if (response.throws) return Promise.reject(new Error('网络断了'))
    const status = response.status ?? 200
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(response.body ?? {}),
    })
  })
}

beforeEach(() => {
  document.cookie = 'nb_csrf=token-123'
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.cookie = 'nb_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
})

describe('CSRF', () => {
  it('写操作带上双提交令牌', async () => {
    mockFetch({ body: { ok: true } })
    await api.login('pw')
    const headers = calls[0].init.headers as Record<string, string>
    expect(headers['X-CSRF-Token']).toBe('token-123')
    expect(calls[0].init.credentials).toBe('same-origin')
  })

  it('GET 不需要令牌，也不带 Content-Type', async () => {
    mockFetch({ body: { items: [] } })
    await api.tasks()
    const headers = calls[0].init.headers as Record<string, string>
    expect(headers['X-CSRF-Token']).toBeUndefined()
    expect(headers['Content-Type']).toBeUndefined()
  })
})

describe('错误处理', () => {
  it('把后端 detail 提炼成 ApiError', async () => {
    mockFetch({ status: 401, body: { detail: '口令不正确' } })
    await expect(api.login('bad')).rejects.toMatchObject({ message: '口令不正确', status: 401 })
  })

  it('非 JSON 响应回退到通用提示', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve({
        ok: false,
        status: 502,
        json: () => Promise.reject(new Error('不是 JSON')),
      }),
    )
    await expect(api.status()).rejects.toBeInstanceOf(ApiError)
    await expect(api.status()).rejects.toMatchObject({ status: 502 })
  })
})

describe('查询参数', () => {
  it('只拼装填了的过滤条件', async () => {
    mockFetch({ body: { items: [] } })
    await api.tasks({ status: 'failed', limit: 10 })
    expect(calls[0].url).toBe('/api/tasks?status=failed&limit=10')

    await api.tasks()
    expect(calls[1].url).toBe('/api/tasks?limit=50')
  })

  it('资讯查询带关键词与天数', async () => {
    mockFetch({ body: { items: [] } })
    await api.news({ keyword: '芯片', days: 30 })
    expect(calls[0].url).toBe('/api/news?keyword=%E8%8A%AF%E7%89%87&days=30&limit=50')
  })

  it('推送记录不带状态时不加参数', async () => {
    mockFetch({ body: { items: [] } })
    await api.deliveries()
    expect(calls[0].url).toBe('/api/deliveries?')
  })
})
