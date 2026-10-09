<!-- 模块: web/src/components/MetricCard.vue
     职责: 指标卡——一个标签配一个大数字 -->

<script setup lang="ts">
import { formatNumber } from '../api/format'

withDefaults(
  defineProps<{
    label: string
    value: number | string
    unit?: string
    tone?: '' | 'ok' | 'err' | 'warn' | 'info'
  }>(),
  { unit: '', tone: '' },
)
</script>

<template>
  <div class="metric">
    <div class="metric-label">
      <span v-if="tone" class="dot" :style="{ color: `var(--${tone === 'ok' ? 'accent' : tone === 'err' ? 'danger' : tone === 'warn' ? 'warn' : 'info'})` }" />
      {{ label }}
    </div>
    <div class="metric-value" :class="{ muted: value === '—' }">
      {{ typeof value === 'number' ? formatNumber(value) : value }}
      <span v-if="unit" class="metric-unit">{{ unit }}</span>
    </div>
  </div>
</template>

<style scoped>
.metric-label .dot {
  display: inline-block;
}
.muted {
  color: var(--text-faint);
}
</style>
