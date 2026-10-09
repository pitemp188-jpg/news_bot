/**
 * 模块: web/src/api/format.ts
 * 职责: 展示层格式化——时间、平台名、任务状态与 cron 的中文说明
 */

const PLATFORM_NAMES: Record<string, string> = {
  weixin: '微信',
  qqbot: 'QQ',
  fake: '本地测试',
}

const STATUS_TEXT: Record<string, string> = {
  pending: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  timeout: '超时',
  cancelled: '已取消',
  sent: '已送达',
  waiting_user: '待用户回复后补投',
}

const KIND_TEXT: Record<string, string> = {
  scheduled: '定时推送',
  chat: '聊天查询',
  manual: '手动触发',
}

/** 任务 / 投递状态对应的徽标配色。 */
export function statusTone(status: string): string {
  if (status === 'succeeded' || status === 'sent') return 'ok'
  if (status === 'failed' || status === 'timeout') return 'err'
  if (status === 'waiting_user' || status === 'pending') return 'warn'
  if (status === 'running') return 'info'
  return ''
}

export function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status
}

export function kindText(kind: string): string {
  return KIND_TEXT[kind] ?? kind
}

export function platformName(platform: string | null | undefined): string {
  if (!platform) return '未指定'
  return PLATFORM_NAMES[platform] ?? platform
}

/** ISO 时间转本地展示；只显示到今天则省略日期。 */
export function formatTime(value: string | null | undefined, withDate = false): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const time = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (!withDate) return time
  return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`
}

/** 相对时间：刚刚 / N 分钟前 / N 小时前 / N 天前。 */
export function relativeTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const seconds = (Date.now() - date.getTime()) / 1000
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天前`
  return formatTime(value, true)
}

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

/** 把五段式 cron 说成人话，认不出来的原样返回。 */
export function cronText(cron: string): string {
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return cron
  const [minute, hour, day, month, weekday] = parts
  if (!/^\d+$/.test(minute) || !/^\d+$/.test(hour)) return cron

  const at = `每天 ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
  if (day === '*' && month === '*' && weekday === '*') return at
  if (day === '*' && month === '*' && /^\d$/.test(weekday)) {
    return `每${WEEKDAYS[Number(weekday)]} ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
  }
  if (month === '*' && weekday === '*' && /^\d+$/.test(day)) {
    return `每月 ${day} 日 ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
  }
  return cron
}

/** 去掉 Markdown 标记，用于卡片标题这类只显示纯文本的位置。 */
export function stripMarks(text: string): string {
  return text
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1')
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/[*_`>]+/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

/** 数字加千分位。 */
export function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN')
}

/** 域名，用于卡片上的来源标注。 */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}
