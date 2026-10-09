<!-- 模块: web/src/components/SectionPanel.vue
     职责: 带标题的区块容器——统一标题栏与空态 -->

<script setup lang="ts">
defineProps<{ title: string; hint?: string; empty?: boolean; emptyText?: string }>()
</script>

<template>
  <section class="panel">
    <header class="panel-head">
      <span>{{ title }}</span>
      <span v-if="hint" class="spacer" />
      <span v-if="hint" style="font-weight: 400; color: var(--text-faint)">{{ hint }}</span>
    </header>
    <div v-if="empty" class="empty">{{ emptyText ?? '暂无数据' }}</div>
    <div v-else class="panel-body">
      <slot />
    </div>
  </section>
</template>
