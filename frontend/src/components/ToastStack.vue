<script setup lang="ts">
import { useToast } from '@/composables/useToast'

const { toasts, dismiss } = useToast()
</script>

<template>
  <div class="notice-stack" aria-live="polite" aria-atomic="false">
    <div
      v-for="item in toasts"
      :key="item.id"
      class="notice"
      :class="`notice-${item.tone}`"
      role="status"
    >
      <span class="notice-message">{{ item.message }}</span>
      <button class="notice-close" type="button" aria-label="关闭提示" @click="dismiss(item.id)">×</button>
    </div>
  </div>
</template>
