<!-- 模块: web/src/views/LoginView.vue
     职责: 登录页——口令输入与错误提示 -->

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'

const route = useRoute()
const router = useRouter()
const password = ref('')
const error = ref('')
const busy = ref(false)

async function submit() {
  error.value = ''
  busy.value = true
  try {
    await api.login(password.value)
    const next = typeof route.query.next === 'string' ? route.query.next : '/'
    await router.push(next)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '登录失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <form class="login-card" @submit.prevent="submit">
    <h1 class="login-title">newsbot 管理后台</h1>
    <p class="login-sub">输入 .env 中配置的 ADMIN_PASSWORD</p>

    <div v-if="error" class="banner err">{{ error }}</div>

    <div class="stack">
      <label class="field">
        <span class="field-label">管理口令</span>
        <input v-model="password" class="input" type="password" autocomplete="current-password" required />
      </label>
      <button class="btn primary" type="submit" :disabled="busy || !password">
        {{ busy ? '登录中…' : '登录' }}
      </button>
    </div>
  </form>
</template>
