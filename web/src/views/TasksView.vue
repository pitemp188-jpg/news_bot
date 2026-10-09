<!-- 模块: web/src/views/TasksView.vue
     职责: 任务页——手动提交查询、按状态过滤、查看详情、取消与重跑 -->

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, ApiError, type Task, type TaskDetail } from '../api/client'
import { formatTime, kindText, platformName, relativeTime, stripMarks } from '../api/format'
import StatusTag from '../components/StatusTag.vue'

const tasks = ref<Task[]>([])
const total = ref(0)
const filter = ref('')
const query = ref('')
const platform = ref('')
const error = ref('')
const notice = ref('')
const loading = ref(false)
const busyId = ref<number | null>(null)
const detail = ref<TaskDetail | null>(null)

const FILTERS = [
  { value: '', label: '全部' },
  { value: 'running', label: '执行中' },
  { value: 'pending', label: '排队中' },
  { value: 'succeeded', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

const canAct = computed(() => ['pending', 'running'].includes(detail.value?.task.status ?? ''))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const page = await api.tasks({ status: filter.value || undefined, limit: 100 })
    tasks.value = page.items
    total.value = page.total ?? page.items.length
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function submit() {
  if (!query.value.trim()) return
  error.value = ''
  notice.value = ''
  try {
    const task = await api.submitTask({
      query: query.value.trim(),
      platform: platform.value || undefined,
      chat_id: platform.value ? 'manual' : undefined,
    })
    notice.value = `任务 #${task.id} 已入队`
    query.value = ''
    await load()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '提交失败'
  }
}

async function act(id: number, action: 'cancel' | 'retry') {
  busyId.value = id
  error.value = ''
  notice.value = ''
  try {
    const task = action === 'cancel' ? await api.cancelTask(id) : await api.retryTask(id)
    notice.value = `任务 #${task.id} 已${action === 'cancel' ? '取消' : '重新入队'}`
    await load()
    if (detail.value?.task.id === id) await open(id)
  } catch (exc) {
    error.value = exc instanceof ApiError ? exc.message : '操作失败'
  } finally {
    busyId.value = null
  }
}

async function open(id: number) {
  try {
    detail.value = await api.task(id)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载详情失败'
  }
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <h1 class="page-title">任务</h1>
    <span class="page-sub">共 {{ total }} 条</span>
    <span class="spacer" />
    <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
  </div>

  <div v-if="error" class="banner err">{{ error }}</div>
  <div v-if="notice" class="banner ok">{{ notice }}</div>

  <section class="card" style="margin-bottom: 20px">
    <div class="form-grid">
      <label class="field" style="grid-column: span 2">
        <span class="field-label">要查询的内容</span>
        <input v-model="query" class="input" placeholder="例如：最近一周 AI 芯片有哪些新进展" @keyup.enter="submit" />
      </label>
      <label class="field">
        <span class="field-label">推送到</span>
        <select v-model="platform" class="select">
          <option value="">只生成报告，不推送</option>
          <option value="weixin">微信</option>
          <option value="qqbot">QQ</option>
        </select>
      </label>
      <button class="btn primary" :disabled="!query.trim()" @click="submit">提交任务</button>
    </div>
  </section>

  <div class="row" style="margin-bottom: 14px">
    <button
      v-for="item in FILTERS"
      :key="item.value"
      class="nav-item"
      :class="{ active: filter === item.value }"
      @click="((filter = item.value), load())"
    >
      {{ item.label }}
    </button>
  </div>

  <div class="split">
    <div class="panel table-wrap">
      <table class="data">
        <thead>
          <tr>
            <th>编号</th>
            <th>内容</th>
            <th>类型</th>
            <th>状态</th>
            <th>创建</th>
            <th style="text-align: right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="task in tasks" :key="task.id">
            <td class="mono">#{{ task.id }}</td>
            <td>
              <button class="btn sm" style="border: none; background: none; padding: 0; text-align: left" @click="open(task.id)">
                <span class="clamp-1" style="max-width: 320px; display: inline-block">{{ task.query }}</span>
              </button>
            </td>
            <td><span class="tag">{{ kindText(task.kind) }}</span></td>
            <td><StatusTag :status="task.status" /></td>
            <td>
              <span class="mono">{{ relativeTime(task.created_at) }}</span>
            </td>
            <td>
              <div class="cell-actions">
                <button
                  v-if="['pending', 'running'].includes(task.status)"
                  class="btn sm danger"
                  :disabled="busyId === task.id"
                  @click="act(task.id, 'cancel')"
                >
                  取消
                </button>
                <button
                  v-else
                  class="btn sm"
                  :disabled="busyId === task.id"
                  @click="act(task.id, 'retry')"
                >
                  重跑
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="!loading && tasks.length === 0">
            <td colspan="6" class="empty">没有符合条件的任务</td>
          </tr>
        </tbody>
      </table>
    </div>

    <aside class="stack" style="gap: 16px">
      <section class="panel">
        <header class="panel-head">
          <span>任务详情</span>
          <span class="spacer" />
          <button v-if="detail" class="nav-item" style="padding: 2px 8px" @click="detail = null">关闭</button>
        </header>
        <div v-if="!detail" class="empty">点击左侧任务查看详情</div>
        <div v-else class="panel-body stack" style="gap: 12px; font-size: 13px">
          <div>
            <div class="field-label">内容</div>
            <div style="font-weight: 550">{{ detail.task.query }}</div>
          </div>
          <div class="row">
            <StatusTag :status="detail.task.status" />
            <span class="tag">{{ kindText(detail.task.kind) }}</span>
            <span class="tag">{{ platformName(detail.task.platform) }}</span>
          </div>
          <div>
            <div class="field-label">创建 / 开始 / 结束</div>
            <div class="mono">
              {{ formatTime(detail.task.created_at, true) }} · {{ formatTime(detail.task.started_at, true) }} ·
              {{ formatTime(detail.task.finished_at, true) }}
            </div>
          </div>
          <div v-if="detail.task.error" class="banner err" style="margin: 0">{{ detail.task.error }}</div>
          <div v-if="detail.reports.length">
            <div class="field-label">报告（{{ detail.reports.length }}）</div>
            <div
              v-for="report in detail.reports"
              :key="report.id"
              class="prose reader"
              style="font-size: 13.5px; line-height: 1.7"
            >
              {{ stripMarks(report.content) }}
            </div>
          </div>
          <div v-if="detail.deliveries.length">
            <div class="field-label">投递（{{ detail.deliveries.length }}）</div>
            <div v-for="row in detail.deliveries" :key="row.id" class="row" style="font-size: 12.5px">
              <StatusTag :status="row.status" />
              <span class="mono">{{ platformName(row.platform) }} / {{ row.chat_id }}</span>
            </div>
          </div>
          <div class="btn-row">
            <button v-if="canAct" class="btn sm danger" @click="act(detail.task.id, 'cancel')">取消任务</button>
            <button v-else class="btn sm" @click="act(detail.task.id, 'retry')">重跑任务</button>
          </div>
        </div>
      </section>
    </aside>
  </div>
</template>
