<!-- 模块: web/src/App.vue
     职责: 应用外壳——顶栏导航与在线状态，登录页不显示外壳 -->

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAV } from './router'
import { api } from './api/client'

const route = useRoute()
const router = useRouter()
const online = ref<Record<string, boolean>>({})
const isLogin = computed(() => route.name === 'login')

/** 平台在线状态：只在有平台配置时展示。 */
async function refreshStatus() {
  if (isLogin.value) return
  try {
    const health = await api.health()
    online.value = health.platforms
  } catch {
    online.value = {}
  }
}

async function logout() {
  try {
    await api.logout()
  } finally {
    router.push({ name: 'login' })
  }
}

onMounted(refreshStatus)
watch(() => route.name, refreshStatus)
</script>

<template>
  <div v-if="isLogin" class="login-wrap">
    <RouterView />
  </div>

  <div v-else class="shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand-dot" />
        <span>newsbot</span>
      </div>
      <nav class="nav">
        <RouterLink
          v-for="item in NAV"
          :key="item.name"
          :to="item.path"
          class="nav-item"
          :class="{ active: route.name === item.name }"
        >
          {{ item.label }}
        </RouterLink>
      </nav>
      <div class="topbar-right">
        <span v-for="(up, name) in online" :key="name" class="tag" :class="up ? 'ok' : 'err'">
          <span class="dot" />{{ name === 'weixin' ? '微信' : name === 'qqbot' ? 'QQ' : name }}
        </span>
        <button class="btn sm" @click="logout">退出</button>
      </div>
    </header>

    <main class="main">
      <RouterView />
    </main>
  </div>
</template>
