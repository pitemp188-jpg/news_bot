<!-- 模块: web/src/views/DigestView.vue
     职责: 当日精选页——日报/周报/月报的编排结果，一个事件一条 -->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type Digest } from '../api/client'
import { formatTime, heatIndex, periodText } from '../api/format'

const period = ref('daily')
const digest = ref<Digest | null>(null)
const error = ref('')
const loading = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    digest.value = await api.digest(period.value)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载失败'
  } finally {
    loading.value = false
  }
}

/** 重要度只用于排序，展示时收成一位小数——它的作用是解释"为什么这条在前面"。 */
function importance(value: number): string {
  return value.toFixed(1)
}

function pickPeriod(value: string) {
  period.value = value
  load()
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <h1 class="page-title">当日精选</h1>
    <span class="page-sub">一个事件一条 · 按重要度排序 · 组装不调用模型</span>
    <span class="spacer" />
    <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
  </div>

  <div v-if="error" class="banner err">{{ error }}</div>

  <section class="card" style="margin-bottom: 20px">
    <div class="tabs">
      <button
        v-for="item in ['daily', 'weekly', 'monthly']"
        :key="item"
        class="tab"
        :class="{ active: period === item }"
        @click="pickPeriod(item)"
      >
        {{ periodText(item) }}
      </button>
    </div>
    <p v-if="digest" class="hint">
      统计区间：{{ formatTime(digest.since, true) }} — {{ formatTime(digest.until, true) }}；共
      {{ digest.items.length }} 条
    </p>
  </section>

  <div v-if="digest && digest.items.length === 0" class="card empty">
    <p>本期没有达到入选标准的内容。</p>
    <p class="hint">
      入选要求：同一份标准独立打两次分、两次之和达到该信源分级门槛（官方一手 6 / 官方账号 7 / 媒体 8），
      且本期有新报道。
    </p>
  </div>

  <article v-for="(item, index) in digest?.items ?? []" :key="item.story_id" class="card digest-card">
    <div class="digest-head">
      <span class="rank">{{ index + 1 }}</span>
      <h2 class="digest-title">
        <router-link :to="`/events/${item.story_id}`">{{ item.title }}</router-link>
      </h2>
      <span class="spacer" />
      <span class="metric">重要度 {{ importance(item.importance) }}</span>
    </div>

    <div class="tag-row">
      <span class="tag">{{ item.participants }} 家报道</span>
      <span v-if="item.official" class="tag ok">官方一手</span>
      <span v-if="item.follow_up_only" class="tag warn">本期只有后续进展</span>
      <span v-if="item.heat > 0" class="tag">热度 {{ heatIndex(item.heat / 10) }}</span>
      <span v-if="item.score > 0" class="tag">精选 {{ item.score }}/10</span>
      <span class="tag muted">最近 {{ formatTime(item.latest_at, true) }}</span>
    </div>

    <p v-if="item.digest" class="digest-body">{{ item.digest }}</p>
    <p v-else-if="item.also_reported.length" class="digest-body muted">
      另有报道：{{ item.also_reported.join('；') }}
    </p>

    <a v-if="item.url" class="digest-link" :href="item.url" target="_blank" rel="noreferrer noopener">
      {{ item.url }}
    </a>
  </article>
</template>

<style scoped>
.digest-card {
  margin-bottom: 14px;
}

.digest-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.rank {
  font-size: 20px;
  font-weight: 700;
  color: var(--muted, #8a94a6);
  min-width: 24px;
}

.digest-title {
  font-size: 17px;
  margin: 0;
}

.digest-title a {
  color: inherit;
  text-decoration: none;
}

.digest-title a:hover {
  text-decoration: underline;
}

.metric {
  font-size: 12px;
  color: var(--muted, #8a94a6);
}

.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 10px 0 8px;
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

.tag.warn {
  background: rgba(210, 153, 34, 0.18);
}

.tag.muted {
  opacity: 0.7;
}

.digest-body {
  margin: 0 0 8px;
  line-height: 1.6;
}

.digest-body.muted {
  opacity: 0.7;
  font-size: 13px;
}

.digest-link {
  font-size: 12px;
  color: var(--muted, #8a94a6);
  word-break: break-all;
}
</style>
