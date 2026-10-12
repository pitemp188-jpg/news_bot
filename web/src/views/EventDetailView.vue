<!-- 模块: web/src/views/EventDetailView.vue
     职责: 事件详情页——这件事的全部报道，标出角色（一手 / 后续 / 重复）与精选分 -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api, type StoryDetail } from '../api/client'
import { formatTime, heatIndex, relativeTime, roleText, roleTone, tierText } from '../api/format'

const route = useRoute()
const detail = ref<StoryDetail | null>(null)
const error = ref('')
const loading = ref(false)

/** 按角色分组显示：读者先看"发生了什么"，再看"后来又怎样了"，重复报道收在最后。 */
const groups = computed(() => {
  const reports = detail.value?.reports ?? []
  return [
    { key: 'primary', label: '一手报道', items: reports.filter((row) => row.role === 'primary') },
    { key: 'follow_up', label: '后续进展', items: reports.filter((row) => row.role === 'follow_up') },
    { key: 'duplicate', label: '重复报道', items: reports.filter((row) => row.role === 'duplicate') },
  ].filter((group) => group.items.length > 0)
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    detail.value = await api.event(Number(route.params.id))
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载失败'
  } finally {
    loading.value = false
  }
}

watch(() => route.params.id, load)
onMounted(load)
</script>

<template>
  <div class="page-head">
    <h1 class="page-title">{{ detail?.story.title ?? '事件' }}</h1>
    <span class="page-sub">
      <template v-if="detail">
        {{ detail.story.participants ?? 0 }} 家报道 · 最新进展 {{ relativeTime(detail.story.last_seen_at) }}
      </template>
    </span>
    <span class="spacer" />
    <router-link class="btn" to="/events">返回列表</router-link>
  </div>

  <div v-if="error" class="banner err">{{ error }}</div>

  <section v-if="detail" class="card">
    <div class="tag-row">
      <span v-if="detail.story.locked" class="tag ok">已人工锁定（不会被自动归组覆盖）</span>
      <span v-if="detail.story.heat !== null" class="tag">热度 {{ heatIndex(detail.story.heat / 10) }}</span>
      <span class="tag muted">首次出现 {{ formatTime(detail.story.first_seen_at, true) }}</span>
      <span class="tag muted">最新进展 {{ formatTime(detail.story.last_seen_at, true) }}</span>
    </div>
    <p v-if="detail.story.digest" class="digest">{{ detail.story.digest }}</p>
  </section>

  <section v-for="group in groups" :key="group.key" class="group">
    <h2 class="group-title">
      {{ group.label }}
      <span class="count">{{ group.items.length }}</span>
    </h2>
    <article v-for="row in group.items" :key="row.news_item_id" class="card report">
      <div class="report-head">
        <a class="report-title" :href="row.url" target="_blank" rel="noreferrer noopener">{{ row.title }}</a>
        <span class="spacer" />
        <span class="tag" :class="roleTone(row.role)">{{ roleText(row.role) }}</span>
      </div>
      <div class="tag-row">
        <span class="tag muted">{{ row.source || '来源未标注' }}</span>
        <span class="tag muted">{{ tierText(row.tier) }}</span>
        <span v-if="!row.editorial" class="tag muted">社区讨论（不计入独立来源数）</span>
        <span v-if="row.score !== null" class="tag">精选 {{ row.score }}/10</span>
        <span v-if="row.selected === false" class="tag muted">未入选</span>
        <span class="tag muted">{{ formatTime(row.published_at ?? row.observed_at, true) }}</span>
      </div>
    </article>
  </section>

  <div v-if="detail && groups.length === 0" class="card empty">这个事件还没有报道。</div>
</template>

<style scoped>
.group {
  margin-top: 20px;
}

.group-title {
  font-size: 14px;
  opacity: 0.8;
  margin: 0 0 10px;
}

.count {
  margin-left: 6px;
  font-size: 12px;
  color: var(--muted, #8a94a6);
}

.report {
  margin-bottom: 10px;
}

.report-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.report-title {
  font-weight: 600;
  color: inherit;
  text-decoration: none;
}

.report-title:hover {
  text-decoration: underline;
}

.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
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

.tag.info {
  background: rgba(56, 139, 253, 0.16);
}

.tag.muted {
  opacity: 0.65;
}

.digest {
  margin: 10px 0 0;
  line-height: 1.6;
}
</style>
