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

<style scoped>
/* 纸面小条：白底 1px 边框 2px 直角 + 左侧 3px 状态竖线 + mono 小字 */
.notice {
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 3px solid var(--text3);
  border-radius: var(--r);
  box-shadow: var(--pop-shadow);
  padding: 9px 11px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--text2);
}
/* 状态竖线：成功=ink、错误=--err、info/提示=--warn */
.notice-ok {
  border-left-color: var(--text);
}
.notice-error {
  border-left-color: var(--err);
}
.notice-info {
  border-left-color: var(--warn);
}
.notice-message {
  color: var(--text2);
}
</style>
