<!-- frontend/src/components/OrbSwallow.vue -->
<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{ play: number; text: string }>()
const emit = defineEmits<{ (event: 'done'): void }>()

type SwallowChar = { id: number; char: string; left: number; delay: number }

const chars = ref<SwallowChar[]>([])
let sequence = 0
let doneTimer = 0

function sampleChars(text: string): string[] {
  const compact = text.replace(/\s+/g, '')
  if (!compact) return []
  const count = Math.min(14, compact.length)
  const step = Math.max(1, Math.floor(compact.length / count))
  const sampled: string[] = []
  for (let index = 0; index < count; index += 1) sampled.push(compact[index * step])
  return sampled
}

watch(() => props.play, (value) => {
  if (!value) return
  window.clearTimeout(doneTimer)
  const sampled = sampleChars(props.text)
  if (!sampled.length) {
    chars.value = []
    emit('done')
    return
  }
  chars.value = sampled.map((char, index) => ({
    id: (sequence += 1),
    char,
    left: 12 + Math.random() * 76,
    delay: index * 55,
  }))
  doneTimer = window.setTimeout(() => {
    chars.value = []
    emit('done')
  }, 900 + sampled.length * 55)
})

onBeforeUnmount(() => window.clearTimeout(doneTimer))
</script>

<template>
  <div class="orb-swallow" aria-hidden="true">
    <span
      v-for="item in chars"
      :key="item.id"
      class="orb-swallow-char"
      :style="{ left: item.left + '%', animationDelay: item.delay + 'ms' }"
    >{{ item.char }}</span>
  </div>
</template>
