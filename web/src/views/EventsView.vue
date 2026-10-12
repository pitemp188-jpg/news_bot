<!-- 模块: web/src/views/EventsView.vue
     职责: 事件列表页——最近有新报道的事件，按最新进展倒序 -->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type Story } from '../api/client'
import { formatTime, heatIndex, relativeTime } from '../api/format'

const items = ref<Story[]>([])
const days = ref(7)
const keyword = ref('')
const error = ref('')
const loading = ref(false)

/** 关键词在浏览器侧过滤：事件数量级很小（几十条），不值得为它多一次请求参数。 */
function visible(): Story[] {
  const needle = keyword.value.trim()
  if (!needle) return items.value
  return items.value.filter((item) => item.title.includes(needle))
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const page = await api.events({ days: days.value, limit: 100 })
    items.value = page.items
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <h1 class="page-title">事件</h1>
    <span class="page-sub">多家来源报道的同一件事，合并成一条</span>
    <span class="spacer" />
    <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
  </div>

  <div v-if="error" class="banner err">{{ error }}</div>

  <section class="card" style="margin-bottom: 20px">
    <div class="form-grid">
      <label class="field" style="grid-column: span 2">
        <span class="field-label">过滤标题</span>
        <input v-model="keyword" class="input" placeholder="留空表示全部" />
      </label>
      <label class="field">
        <span class="field-label">时间范围</span>
        <select v-model.number="days" class="select" @change="load">
          <option :value="1">最近 1 天</option>
          <option :value="7">最近 7 天</option>
          <option :value="30">最近 30 天</option>
        </select>
      </label>
    </div>
  </section>

  <div v-if="!loading && visible().length === 0" class="card empty">
    <p>还没有归组的报道。</p>
    <p class="hint">事件由聚簇生成：先按标题与摘要挑出可能相关的报道，再让模型判定是不是同一件事。</p>
  </div>

  <article v-for="item in visible()" :key="item.id" class="card event-card">
    <div class="event-head">
      <h2 class="event-title">
        <router-link :to="`/events/${item.id}`">{{ item.title }}</router-link>
      </h2>
      <span class="spacer" />
      <span v-if="item.locked" class="tag ok">已人工锁定</span>
      <span v-if="item.participants" class="tag">{{ item.participants }} 家报道</span>
      <span v-if="item.heat !== null" class="tag">热度 {{ heatIndex(item.heat / 10) }}</span>
    </div>
    <p class="event-meta">
      首次出现 {{ formatTime(item.first_seen_at, true) }} · 最新进展 {{ relativeTime(item.last_seen_at) }}
    </p>
  </article>
</template>

<style scoped>
.event-card {
  margin-bottom: 12px;
}

.event-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.event-title {
  font-size: 16px;
  margin: 0;
}

.event-title a {
  color: inherit;
  text-decoration: none;
}

.event-title a:hover {
  text-decoration: underline;
}

.event-meta {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--muted, #8a94a6);
}

.tag {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 10px;
  background: rgba(127, 127, 127, 0.12);
}

.tag.ok {
  background: rgba(46, 160, 67, 0.16);
}
</style>
