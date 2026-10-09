<!-- 模块: web/src/views/DashboardView.vue
     职责: 总览页——运行指标、最新日报卡片流、侧栏运行状态与最近推送榜 -->

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, type Delivery, type Report, type SystemStatus } from '../api/client'
import { formatTime, hostOf, platformName, relativeTime, stripMarks } from '../api/format'
import MetricCard from '../components/MetricCard.vue'
import SectionPanel from '../components/SectionPanel.vue'
import StatusTag from '../components/StatusTag.vue'

const status = ref<SystemStatus | null>(null)
const reports = ref<Report[]>([])
const deliveries = ref<Delivery[]>([])
const error = ref('')
const loading = ref(true)

const platforms = computed(() => Object.entries(status.value?.platforms ?? {}))
const onlineCount = computed(() => platforms.value.filter(([, up]) => up).length)
const todayTokens = computed(() => {
  const tokens = status.value?.today_tokens
  return tokens ? tokens.prompt + tokens.completion : 0
})

/** 报告正文首行当标题，其余当摘要——日报本身以结论开头。 */
function splitReport(content: string): { title: string; body: string } {
  const lines = content
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  if (lines.length === 0) return { title: '（空内容）', body: '' }
  const first = stripMarks(lines[0]).slice(0, 140)
  return { title: first || '（空内容）', body: stripMarks(lines.slice(1).join(' ')) }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [statusData, reportPage, deliveryPage] = await Promise.all([
      api.status(),
      api.reports(8),
      api.deliveries(),
    ])
    status.value = statusData
    reports.value = reportPage.items
    deliveries.value = deliveryPage.items.slice(0, 8)
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
    <h1 class="page-title">总览</h1>
    <span class="page-sub">{{ status ? `时区 ${status.timezone}` : '' }}</span>
    <span class="spacer" />
    <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
  </div>

  <div v-if="error" class="banner err">{{ error }}</div>

  <div class="metrics" style="margin-bottom: 24px">
    <MetricCard
      label="平台在线"
      :value="`${onlineCount}/${platforms.length || 0}`"
      :tone="onlineCount > 0 ? 'ok' : 'err'"
    />
    <MetricCard label="队列" :value="status?.queue.depth ?? 0" unit="待执行" />
    <MetricCard label="执行中" :value="status?.queue.running ?? 0" unit="个任务" />
    <MetricCard label="今日 token" :value="todayTokens" />
    <MetricCard label="资讯库" :value="status?.content.news ?? 0" unit="条" />
    <MetricCard label="定时推送" :value="status?.content.schedules ?? 0" unit="条" />
  </div>

  <div class="split">
    <div>
      <h2 class="page-title" style="font-size: 16px; margin-bottom: 12px">最新日报与答复</h2>
      <div v-if="loading" class="loading">加载中…</div>
      <div v-else-if="reports.length === 0" class="card empty">
        还没有生成任何报告。<br />去「任务」页手动跑一次，或到「定时推送」建一条订阅。
      </div>
      <div v-else class="feed">
        <article v-for="report in reports" :key="report.id" class="item">
          <h3 class="item-title">{{ splitReport(report.content).title }}</h3>
          <p v-if="splitReport(report.content).body" class="item-body">{{ splitReport(report.content).body }}</p>
          <div class="item-foot">
            <span class="mono">#{{ report.id }}</span>
            <span class="sep">·</span>
            <span>{{ formatTime(report.created_at, true) }}</span>
            <span class="sep">·</span>
            <span>{{ relativeTime(report.created_at) }}</span>
            <template v-if="report.sources.length">
              <span class="sep">·</span>
              <span>{{ report.sources.length }} 个来源</span>
            </template>
          </div>
          <div v-if="report.sources.length" class="item-foot" style="margin-top: 8px">
            <a
              v-for="source in report.sources.slice(0, 4)"
              :key="source.url"
              :href="source.url"
              target="_blank"
              rel="noreferrer noopener"
              class="tag"
              :title="source.title"
            >
              {{ hostOf(source.url) }}
            </a>
          </div>
        </article>
      </div>
    </div>

    <aside class="stack" style="gap: 16px">
      <SectionPanel title="运行状态">
        <div class="stack" style="gap: 8px">
          <div class="row" style="font-size: 13px">
            <span style="color: var(--text-dim)">平台</span>
            <span class="spacer" />
            <span v-if="platforms.length === 0" class="tag warn">未配置</span>
            <span
              v-for="[name, up] in platforms"
              :key="name"
              class="tag"
              :class="up ? 'ok' : 'err'"
            >
              <span class="dot" />{{ platformName(name) }}{{ up ? '' : ' 离线' }}
            </span>
          </div>
          <div v-for="(count, key) in status?.tasks ?? {}" :key="key" class="row" style="font-size: 13px">
            <span style="color: var(--text-dim)">{{ key === 'pending' ? '排队' : key === 'running' ? '执行中' : key === 'succeeded' ? '已完成' : key === 'failed' ? '失败' : key === 'timeout' ? '超时' : '已取消' }}</span>
            <span class="spacer" />
            <span class="mono">{{ count }}</span>
          </div>
          <div v-if="(status?.running.waiting_user ?? 0) > 0" class="banner warn" style="margin: 4px 0 0">
            有 {{ status?.running.waiting_user }} 条等待用户先发消息后补投
          </div>
        </div>
      </SectionPanel>

      <SectionPanel
        title="最近推送"
        :hint="`${deliveries.length} 条`"
        :empty="deliveries.length === 0"
        empty-text="还没有推送记录"
      >
        <div class="rank-list">
          <div v-for="(item, index) in deliveries" :key="item.id" class="rank-item">
            <span class="rank-no" :class="{ top: index < 3 }">{{ index + 1 }}</span>
            <div style="min-width: 0; flex: 1">
              <div class="clamp-1">{{ platformName(item.platform) }} · {{ item.chat_id }}</div>
              <div class="rank-meta">
                <StatusTag :status="item.status" />
                <span style="margin-left: 6px">{{
                  item.sent_at ? relativeTime(item.sent_at) : item.attempts ? `尝试 ${item.attempts} 次` : '排队中'
                }}</span>
              </div>
            </div>
          </div>
        </div>
      </SectionPanel>
    </aside>
  </div>
</template>
