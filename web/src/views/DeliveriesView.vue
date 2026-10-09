<!-- 模块: web/src/views/DeliveriesView.vue
     职责: 推送记录页——按状态过滤、查看失败原因与待补投 -->

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type Delivery } from '../api/client'
import { formatTime, platformName } from '../api/format'
import StatusTag from '../components/StatusTag.vue'

const deliveries = ref<Delivery[]>([])
const total = ref(0)
const filter = ref('')
const error = ref('')
const loading = ref(false)
const expanded = ref<number | null>(null)

const FILTERS = [
  { value: '', label: '全部' },
  { value: 'sent', label: '已送达' },
  { value: 'waiting_user', label: '待补投' },
  { value: 'failed', label: '失败' },
]

async function load() {
  loading.value = true
  error.value = ''
  try {
    const page = await api.deliveries(filter.value || undefined)
    deliveries.value = page.items
    total.value = page.total ?? page.items.length
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
    <h1 class="page-title">推送记录</h1>
    <span class="page-sub">共 {{ total }} 条</span>
    <span class="spacer" />
    <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
  </div>

  <div v-if="error" class="banner err">{{ error }}</div>

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

  <div class="panel table-wrap">
    <table class="data">
      <thead>
        <tr>
          <th>编号</th>
          <th>报告</th>
          <th>目标</th>
          <th>状态</th>
          <th>尝试</th>
          <th>送达时间</th>
          <th style="text-align: right">详情</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in deliveries" :key="row.id">
          <tr>
            <td class="mono">#{{ row.id }}</td>
            <td class="mono">{{ row.report_id ? `#${row.report_id}` : '—' }}</td>
            <td class="mono">{{ platformName(row.platform) }} / {{ row.chat_id }}</td>
            <td><StatusTag :status="row.status" /></td>
            <td class="mono">{{ row.attempts }}</td>
            <td class="mono">{{ row.sent_at ? formatTime(row.sent_at, true) : '—' }}</td>
            <td>
              <div class="cell-actions">
                <button class="btn sm" @click="expanded = expanded === row.id ? null : row.id">
                  {{ expanded === row.id ? '收起' : '查看' }}
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="expanded === row.id">
            <td colspan="7" style="background: var(--surface-2)">
              <div v-if="row.error" class="banner err" style="margin: 0 0 8px">{{ row.error }}</div>
              <p class="page-sub" style="margin: 0">
                <template v-if="row.status === 'waiting_user'">
                  平台要求用户先发一条消息才能主动推送。用户下次发消息时，系统会自动补投这条内容。
                </template>
                <template v-else-if="row.status === 'sent'">已成功送达。</template>
                <template v-else>共尝试 {{ row.attempts }} 次仍失败，可让用户先发一条消息后重跑对应任务。</template>
              </p>
            </td>
          </tr>
        </template>
        <tr v-if="!loading && deliveries.length === 0">
          <td colspan="7" class="empty">没有符合条件的推送记录</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
