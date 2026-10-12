/**
 * 模块: web/src/api/format.spec.ts
 * 职责: 校验展示层格式化——时间、cron 中文、状态映射与域名提取
 */

import { describe, expect, it } from 'vitest'
import {
  badgeText,
  cronText,
  formatNumber,
  heatIndex,
  hostOf,
  kindText,
  periodText,
  platformName,
  relativeTime,
  roleText,
  roleTone,
  statusText,
  statusTone,
  tierText,
  trendText,
} from './format'

describe('statusText / statusTone', () => {
  it('把任务与投递状态翻成中文并给出配色', () => {
    expect(statusText('succeeded')).toBe('已完成')
    expect(statusText('waiting_user')).toBe('待用户回复后补投')
    expect(statusText('unknown_state')).toBe('unknown_state')

    expect(statusTone('succeeded')).toBe('ok')
    expect(statusTone('sent')).toBe('ok')
    expect(statusTone('failed')).toBe('err')
    expect(statusTone('waiting_user')).toBe('warn')
    expect(statusTone('running')).toBe('info')
    expect(statusTone('cancelled')).toBe('')
  })
})

describe('kindText / platformName', () => {
  it('按中文展示任务类型与平台', () => {
    expect(kindText('scheduled')).toBe('定时推送')
    expect(kindText('other')).toBe('other')
    expect(platformName('weixin')).toBe('微信')
    expect(platformName('qqbot')).toBe('QQ')
    expect(platformName(null)).toBe('未指定')
  })
})

describe('cronText', () => {
  it('把常见 cron 说成人话', () => {
    expect(cronText('0 21 * * *')).toBe('每天 21:00')
    expect(cronText('30 8 * * *')).toBe('每天 08:30')
    expect(cronText('0 9 * * 1')).toBe('每周一 09:00')
    expect(cronText('0 10 1 * *')).toBe('每月 1 日 10:00')
  })

  it('认不出来的原样返回，不瞎猜', () => {
    expect(cronText('*/5 * * * *')).toBe('*/5 * * * *')
    expect(cronText('bad')).toBe('bad')
  })
})

describe('relativeTime', () => {
  const ago = (seconds: number) => new Date(Date.now() - seconds * 1000).toISOString()

  it('按距今时长给不同粒度', () => {
    expect(relativeTime(ago(10))).toBe('刚刚')
    expect(relativeTime(ago(120))).toBe('2 分钟前')
    expect(relativeTime(ago(7200))).toBe('2 小时前')
    expect(relativeTime(ago(86400 * 3))).toBe('3 天前')
  })

  it('空值与非法值都给占位符', () => {
    expect(relativeTime(null)).toBe('—')
    expect(relativeTime('not-a-date')).toBe('—')
  })
})

describe('hostOf', () => {
  it('取域名并去掉 www', () => {
    expect(hostOf('https://www.example.com/a/b')).toBe('example.com')
    expect(hostOf('https://news.ycombinator.com/')).toBe('news.ycombinator.com')
  })

  it('非法网址原样返回，不抛错', () => {
    expect(hostOf('不是一个网址')).toBe('不是一个网址')
  })
})

describe('formatNumber', () => {
  it('加千分位', () => {
    expect(formatNumber(1234567)).toBe('1,234,567')
    expect(formatNumber(0)).toBe('0')
  })
})

describe('事件层展示', () => {
  it('报道角色三态各自有中文名与轻重不同的配色', () => {
    expect(roleText('primary')).toBe('一手报道')
    expect(roleText('follow_up')).toBe('后续进展')
    expect(roleText('duplicate')).toBe('重复报道')
    expect(roleText('unknown')).toBe('unknown')

    expect(roleTone('primary')).toBe('ok')
    expect(roleTone('follow_up')).toBe('info')
    expect(roleTone('duplicate')).toBe('')
  })

  it('信源分级翻成中文，认不出来原样返回', () => {
    expect(tierText('T1')).toBe('官方一手')
    expect(tierText('T1_5')).toBe('官方账号')
    expect(tierText('T2')).toBe('媒体')
    expect(tierText('T9')).toBe('T9')
  })

  it('趋势与标记翻成中文', () => {
    expect(trendText('up')).toBe('升温')
    expect(trendText('down')).toBe('降温')
    expect(trendText('flat')).toBe('持平')
    expect(trendText('new')).toBe('新事件')
    expect(trendText('weird')).toBe('—')

    expect(badgeText('surge')).toBe('骤增')
    expect(badgeText('rising')).toBe('上升')
    expect(badgeText('new')).toBe('新出现')
  })

  it('热度刻度保留一位小数，缺值显示占位符', () => {
    expect(heatIndex(0.5333333333333333)).toBe('0.5')
    expect(heatIndex(12)).toBe('12.0')
    expect(heatIndex(null)).toBe('—')
    expect(heatIndex(undefined)).toBe('—')
  })

  it('周期翻成中文', () => {
    expect(periodText('daily')).toBe('日报')
    expect(periodText('weekly')).toBe('周报')
    expect(periodText('monthly')).toBe('月报')
  })
})
