<!-- 模块: web/src/views/SchedulesView.vue
     职责: 定时推送页——新建、改主题与时间、启停、删除 -->

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, ApiError, type Schedule } from '../api/client'
import { cronText, platformName } from '../api/format'

const schedules = ref<Schedule[]>([])
const topics = ref('')
const time = ref('21:00')
const platform = ref('weixin')
const chatId = ref('')
const error = ref('')
const notice = ref('')
const busyId = ref<number | null>(null)
const creating = ref(false)

async function load() {
  error.value = ''
  try {
    schedules.value = (await api.schedules()).items
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载失败'
  }
}

async function create() {
  const list = topics.value
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  if (list.length === 0 || !chatId.value.trim()) {
    error.value = '主题与推送目标都要填写'
    return
  }
  creating.value = true
  error.value = ''
  notice.value = ''
  try {
    const row = await api.createSchedule({
      topics: list,
      time: time.value,
      platform: platform.value,
      chat_id: chatId.value.trim(),
    })
    notice.value = `已新建订阅 #${row.id}，${cronText(row.cron)} 推送`
    topics.value = ''
    chatId.value = ''
    await load()
  } catch (exc) {
    error.value = exc instanceof ApiError ? exc.message : '新建失败'
  } finally {
    creating.value = false
  }
}

async function toggle(row: Schedule) {
  await update(row.id, { enabled: !row.enabled }, row.enabled ? '已暂停' : '已启用')
}

async function update(id: number, payload: Record<string, unknown>, message: string) {
  busyId.value = id
  error.value = ''
  notice.value = ''
  try {
    await api.updateSchedule(id, payload as never)
    notice.value = message
    await load()
  } catch (exc) {
    error.value = exc instanceof ApiError ? exc.message : '修改失败'
  } finally {
    busyId.value = null
  }
}

async function editTime(row: Schedule) {
  const input = window.prompt('新的推送时间（HH:MM）', '21:00')
  if (input) await update(row.id, { time: input.trim() }, `订阅 #${row.id} 时间已更新`)
}

async function editTopics(row: Schedule) {
  const input = window.prompt('新的主题（逗号分隔）', row.topics.join('，'))
  if (!input) return
  const list = input
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  if (list.length === 0) {
    error.value = '主题不能为空'
    return
  }
  await update(row.id, { topics: list }, `订阅 #${row.id} 主题已更新`)
}

async function remove(row: Schedule) {
  busyId.value = row.id
  error.value = ''
  notice.value = ''
  try {
    await api.deleteSchedule(row.id)
    notice.value = `订阅 #${row.id} 已停用（记录保留）`
    await load()
  } catch (exc) {
    error.value = exc instanceof ApiError ? exc.message : '停用失败'
  } finally {
    busyId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <h1 class="page-title">定时推送</h1>
    <span class="page-sub">共 {{ schedules.length }} 条</span>
    <span class="spacer" />
    <button class="btn" @click="load">刷新</button>
  </div>

  <div v-if="error" class="banner err">{{ error }}</div>
  <div v-if="notice" class="banner ok">{{ notice }}</div>

  <section class="card" style="margin-bottom: 20px">
    <h2 class="login-title" style="font-size: 15px; margin-bottom: 12px">新建订阅</h2>
    <div class="form-grid">
      <label class="field">
        <span class="field-label">主题（逗号分隔）</span>
        <input v-model="topics" class="input" placeholder="AI，半导体" />
      </label>
      <label class="field">
        <span class="field-label">推送时间</span>
        <input v-model="time" class="input" type="time" />
      </label>
      <label class="field">
        <span class="field-label">平台</span>
        <select v-model="platform" class="select">
          <option value="weixin">微信</option>
          <option value="qqbot">QQ</option>
        </select>
      </label>
      <label class="field">
        <span class="field-label">推送目标 ID</span>
        <input v-model="chatId" class="input" placeholder="微信 user_id / QQ openid" />
      </label>
      <button class="btn primary" :disabled="creating" @click="create">
        {{ creating ? '创建中…' : '创建' }}
      </button>
    </div>
    <p class="page-sub" style="margin: 10px 0 0">
      用户在聊天里发任意消息后，会得到一条默认订阅（默认主题与时间见 config.yaml），无需在这里手动加。
    </p>
  </section>

  <div class="panel table-wrap">
    <table class="data">
      <thead>
        <tr>
          <th>编号</th>
          <th>主题</th>
          <th>频率</th>
          <th>目标</th>
          <th>状态</th>
          <th style="text-align: right">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in schedules" :key="row.id">
          <td class="mono">#{{ row.id }}</td>
          <td>
            <span v-for="topic in row.topics" :key="topic" class="tag" style="margin-right: 4px">{{ topic }}</span>
          </td>
          <td>{{ cronText(row.cron) }}<span class="mono" style="color: var(--text-faint)"> · {{ row.cron }}</span></td>
          <td class="mono">{{ platformName(row.platform) }} / {{ row.chat_id }}</td>
          <td><span class="tag" :class="row.enabled ? 'ok' : ''">{{ row.enabled ? '启用' : '已停用' }}</span></td>
          <td>
            <div class="cell-actions">
              <button class="btn sm" :disabled="busyId === row.id" @click="editTopics(row)">改主题</button>
              <button class="btn sm" :disabled="busyId === row.id" @click="editTime(row)">改时间</button>
              <button class="btn sm" :disabled="busyId === row.id" @click="toggle(row)">
                {{ row.enabled ? '暂停' : '启用' }}
              </button>
              <button v-if="row.enabled" class="btn sm danger" :disabled="busyId === row.id" @click="remove(row)">
                停用
              </button>
            </div>
          </td>
        </tr>
        <tr v-if="schedules.length === 0">
          <td colspan="6" class="empty">还没有定时推送。等用户在聊天里发一条消息，或在这里手动创建。</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
