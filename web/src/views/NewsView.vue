<!-- 模块: web/src/views/NewsView.vue
     职责: 资讯库页——关键词与天数过滤、条目卡片流 -->

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type NewsItem } from '../api/client'
import { hostOf, relativeTime } from '../api/format'

const items = ref<NewsItem[]>([])
const total = ref(0)
const keyword = ref('')
const days = ref(7)
const error = ref('')
const loading = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const page = await api.news({ keyword: keyword.value || undefined, days: days.value, limit: 100 })
    items.value = page.items
    total.value = page.total ?? page.items.length
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载失败'
  } finally {
    loading.value = false
  }
}

/** 按抓取日期分组，卡片流里插日期分隔，方便一眼看出新旧。 */
function groupLabel(value: string | null): string {
  if (!value) return '未知时间'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未知时间'
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <h1 class="page-title">资讯库</h1>
    <span class="page-sub">共 {{ total }} 条</span>
    <span class="spacer" />
    <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
  </div>

  <div v-if="error" class="banner err">{{ error }}</div>

  <section class="card" style="margin-bottom: 20px">
    <div class="form-grid">
      <label class="field" style="grid-column: span 2">
        <span class="field-label">关键词（标题或网址）</span>
        <input v-model="keyword" class="input" placeholder="留空表示不限" @keyup.enter="load" />
      </label>
      <label class="field">
        <span class="field-label">时间范围</span>
        <select v-model.number="days" class="select">
          <option :value="1">最近 1 天</option>
          <option :value="7">最近 7 天</option>
          <option :value="30">最近 30 天</option>
          <option :value="365">最近一年</option>
        </select>
      </label>
      <button class="btn primary" :disabled="loading" @click="load">查询</button>
    </div>
  </section>

  <div v-if="!loading && items.length === 0" class="card empty">
    这段时间没有采集到内容。任务执行时会自动把来源写进资讯库，重复内容不会二次入库。
  </div>
  <div v-else class="feed">
    <article v-for="item in items" :key="item.id" class="item">
      <h3 class="item-title">
        <a :href="item.url" target="_blank" rel="noreferrer noopener">{{ item.title || item.url }}</a>
      </h3>
      <div class="item-foot">
        <span class="tag">{{ item.source || hostOf(item.url) }}</span>
        <span class="sep">·</span>
        <span>{{ groupLabel(item.fetched_at) }}</span>
        <span class="sep">·</span>
        <span>{{ relativeTime(item.fetched_at) }}</span>
        <span class="sep">·</span>
        <span class="mono">{{ hostOf(item.url) }}</span>
      </div>
    </article>
  </div>
</template>
