<!-- 模块: web/src/views/HotView.vue
     职责: 热度榜页——48 小时窗口 / 24 小时半衰期 / 按独立参与者计数 -->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type HotBoard } from '../api/client'
import { badgeText, formatTime, heatIndex, relativeTime, trendText } from '../api/format'

const board = ref<HotBoard | null>(null)
const error = ref('')
const loading = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    board.value = await api.hot(20)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function trendTone(trend: string): string {
  if (trend === 'up') return 'ok'
  if (trend === 'down') return 'muted'
  return ''
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <h1 class="page-title">热度榜</h1>
    <span class="page-sub">按独立参与者计数，不是按文章条数</span>
    <span class="spacer" />
    <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
  </div>

  <div v-if="error" class="banner err">{{ error }}</div>

  <section v-if="board" class="card rule-card">
    <div class="rule-row">
      <span class="rule-item">规则：{{ board.rule }}</span>
      <span class="rule-item">窗口 {{ board.window_hours }} 小时</span>
      <span class="rule-item">半衰期 {{ board.half_life_hours }} 小时</span>
      <span class="rule-item">更新于 {{ formatTime(board.updated_at, true) }}</span>
    </div>
    <p class="hint">
      一家媒体发十篇只算一次（否则榜单会被单个媒体刷满），社区里每个作者各算一个但<strong>不计入独立来源数</strong>。
      进榜要求参与者 ≥2 且编辑类来源 ≥1——一条独家报道不是热点。
    </p>
  </section>

  <div v-if="board && board.items.length === 0" class="card empty">
    <p>还没有事件达到进榜门槛。</p>
    <p class="hint">门槛是"至少两家互相独立的来源都报道了它"。单篇独家报道不上榜是有意为之。</p>
  </div>

  <section v-if="board && board.items.length" class="card">
    <table class="table">
      <thead>
        <tr>
          <th style="width: 48px">#</th>
          <th>事件</th>
          <th style="width: 96px">热度</th>
          <th style="width: 110px">参与者</th>
          <th style="width: 110px">独立来源</th>
          <th style="width: 150px">趋势</th>
          <th style="width: 130px">最新报道</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(item, index) in board.items" :key="item.story_id">
          <td class="rank">{{ index + 1 }}</td>
          <td>
            <router-link :to="`/events/${item.story_id}`">{{ item.title }}</router-link>
            <span v-for="badge in item.badges" :key="badge" class="tag warn">{{ badgeText(badge) }}</span>
          </td>
          <td>{{ heatIndex(item.index) }}</td>
          <td>{{ item.participants }}</td>
          <td>{{ item.sources }}</td>
          <td>
            <span class="tag" :class="trendTone(item.trend)">{{ trendText(item.trend) }}</span>
            <span v-if="item.trend_pct !== null" class="muted"> {{ Math.round(item.trend_pct * 100) }}%</span>
          </td>
          <td class="muted">{{ relativeTime(item.latest_at) }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.rule-card {
  margin-bottom: 18px;
}

.rule-row {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}

.rule-item {
  font-size: 13px;
  opacity: 0.85;
}

.rank {
  font-weight: 700;
  color: var(--muted, #8a94a6);
}

.muted {
  color: var(--muted, #8a94a6);
  font-size: 12px;
}

.tag {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 10px;
  margin-left: 6px;
  background: rgba(127, 127, 127, 0.12);
}

.tag.ok {
  background: rgba(46, 160, 67, 0.16);
}

.tag.warn {
  background: rgba(210, 153, 34, 0.18);
}

.tag.muted {
  opacity: 0.6;
}
</style>
