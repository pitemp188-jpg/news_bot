<!-- 模块: web/src/components/StatusTag.vue
     职责: 状态徽标——按状态映射配色与中文文案 -->

<script setup lang="ts">
import { statusText, statusTone } from '../api/format'

const props = defineProps<{ status: string; label?: string }>()
</script>

<template>
  <span class="tag" :class="statusTone(props.status)">
    <span class="dot" />{{ props.label ?? statusText(props.status) }}
  </span>
</template>
