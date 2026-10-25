<script setup lang="ts">
import { computed } from 'vue'
import { buildWaveBars } from '@/lib/waveshape'

const props = withDefaults(defineProps<{
  seed: string
  played?: number
  bars?: number
  height?: number
}>(), { played: 0, bars: 0, height: 24 })

const barList = computed(() => {
  const count = props.bars > 0 ? props.bars : 26
  return buildWaveBars(props.seed || 'seed', count, props.played, props.height)
})
</script>

<template>
  <span class="wave" :style="{ height: `${height}px` }" aria-hidden="true">
    <i v-for="(bar, index) in barList" :key="index" :class="{ on: bar.on }" :style="{ height: `${bar.height}px` }" />
  </span>
</template>
