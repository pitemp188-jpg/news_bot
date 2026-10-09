<!-- 模块: web/src/views/SettingsView.vue
     职责: 设置页——平台状态、主动推送测试、运行配置说明 -->

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type SystemStatus } from '../api/client'
import { platformName } from '../api/format'
import MetricCard from '../components/MetricCard.vue'

const status = ref<SystemStatus | null>(null)
const text = ref('')
const platform = ref('')
const chatId = ref('')
const error = ref('')
const notice = ref('')
const sending = ref(false)
const loaded = ref(false)

async function load() {
  try {
    status.value = await api.status()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载失败'
  } finally {
    loaded.value = true
  }
}

async function push() {
  if (!text.value.trim()) return
  sending.value = true
  error.value = ''
  notice.value = ''
  try {
    await api.push({
      text: text.value.trim(),
      platform: platform.value || undefined,
      chat_id: chatId.value.trim() || undefined,
    })
    notice.value = '已发送'
    text.value = ''
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '发送失败'
  } finally {
    sending.value = false
  }
}

onMounted(load)

const CONFIG_ROWS = [
  ['LLM_BASE_URL', '模型接口地址（OpenAI 兼容）'],
  ['LLM_MODEL', '模型名'],
  ['SEARCH_PROVIDER / SEARXNG_URL', '搜索后端'],
  ['WEIXIN_ACCOUNT_ID / WEIXIN_TOKEN', '微信 iLink bot 凭证（开放平台发放）'],
  ['QQ_APP_ID / QQ_CLIENT_SECRET', 'QQ 开放平台机器人凭证'],
  ['WEIXIN_ALLOWED_USERS / QQ_ALLOWED_USERS', '白名单，逗号分隔，* 表示全部放行'],
  ['ADMIN_PASSWORD', '本管理后台登录口令'],
]
</script>

<template>
  <div class="page-head">
    <h1 class="page-title">设置</h1>
    <span class="spacer" />
    <button class="btn" @click="load">刷新</button>
  </div>

  <div v-if="error" class="banner err">{{ error }}</div>
  <div v-if="notice" class="banner ok">{{ notice }}</div>

  <div class="metrics" style="margin-bottom: 20px">
    <MetricCard label="今日 prompt token" :value="status?.today_tokens.prompt ?? 0" />
    <MetricCard label="今日 completion token" :value="status?.today_tokens.completion ?? 0" />
    <MetricCard label="报告总数" :value="status?.content.reports ?? 0" unit="篇" />
    <MetricCard label="推送总数" :value="status?.running.deliveries ?? 0" unit="条" />
  </div>

  <div class="split">
    <div class="stack" style="gap: 20px">
      <section class="panel">
        <header class="panel-head"><span>平台连接</span></header>
        <div class="panel-body stack" style="gap: 10px">
          <div v-if="loaded && Object.keys(status?.platforms ?? {}).length === 0" class="banner warn" style="margin: 0">
            没有配置任何平台凭证。服务仍能跑定时任务与本地查询，但不能收发消息。
          </div>
          <div v-for="(up, name) in status?.platforms ?? {}" :key="name" class="row">
            <span class="tag" :class="up ? 'ok' : 'err'"><span class="dot" />{{ platformName(name) }}</span>
            <span class="page-sub">{{ up ? '已连接' : '未连接' }}</span>
          </div>
          <div class="row" style="font-size: 13px">
            <span style="color: var(--text-dim)">队列</span>
            <span class="spacer" />
            <span class="mono">{{ status?.queue.depth ?? 0 }} 待执行 · {{ status?.queue.running ?? 0 }} 执行中</span>
          </div>
          <div v-if="(status?.running.waiting_user ?? 0) > 0" class="row" style="font-size: 13px">
            <span style="color: var(--text-dim)">待补投</span>
            <span class="spacer" />
            <span class="mono">{{ status?.running.waiting_user }} 条</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <header class="panel-head"><span>运行配置</span><span class="spacer" /><span style="font-weight: 400; color: var(--text-faint)">.env / config.yaml</span></header>
        <div class="panel-body panel">
          <div v-for="[key, desc] in CONFIG_ROWS" :key="key" class="row" style="font-size: 13px; padding: 5px 0">
            <span class="mono" style="min-width: 240px">{{ key }}</span>
            <span class="page-sub">{{ desc }}</span>
          </div>
          <p class="page-sub" style="margin: 8px 0 0">
            密钥只显示是否配置，不会在此页面展示明文。改完配置重启服务生效。
          </p>
        </div>
      </section>
    </div>

    <aside>
      <section class="panel">
        <header class="panel-head"><span>测试推送</span></header>
        <div class="panel-body stack" style="gap: 12px">
          <label class="field">
            <span class="field-label">内容</span>
            <textarea v-model="text" class="input" rows="4" placeholder="随便写点什么，验证通道是否通畅" />
          </label>
          <label class="field">
            <span class="field-label">平台（留空发给所有订阅者）</span>
            <select v-model="platform" class="select">
              <option value="">全部订阅者</option>
              <option value="weixin">微信</option>
              <option value="qqbot">QQ</option>
            </select>
          </label>
          <label v-if="platform" class="field">
            <span class="field-label">目标 ID</span>
            <input v-model="chatId" class="input" placeholder="留空也可" />
          </label>
          <button class="btn primary" :disabled="sending || !text.trim()" @click="push">
            {{ sending ? '发送中…' : '发送' }}
          </button>
          <p class="page-sub" style="margin: 0">
            推送失败最常见的原因是平台要求用户先发消息建立会话，此时会在推送记录里标记为「待补投」。
          </p>
        </div>
      </section>
    </aside>
  </div>
</template>
