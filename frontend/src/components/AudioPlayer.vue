<script setup lang="ts">
import { computed, ref } from 'vue'
import { Pause, Play } from '@lucide/vue'

const props = defineProps<{
  src: string
  compact?: boolean
}>()

const emit = defineEmits<{
  (event: 'play'): void
  (event: 'pause'): void
  (event: 'ended'): void
  (event: 'error'): void
}>()

const audio = ref<HTMLAudioElement | null>(null)
const playing = ref(false)
const current = ref(0)
const duration = ref(0)

const percent = computed(() => (duration.value > 0 ? Math.min(100, (current.value / duration.value) * 100) : 0))

function formatClock(seconds: number) {
  const total = Math.max(0, Number(seconds) || 0)
  const minutes = Math.floor(total / 60)
  const rest = Math.floor(total % 60)
  return `${minutes}:${String(rest).padStart(2, '0')}`
}

function toggle() {
  const element = audio.value
  if (!element) return
  if (playing.value) {
    element.pause()
    return
  }
  document.querySelectorAll<HTMLAudioElement>('audio').forEach((other) => {
    if (other !== element) other.pause()
  })
  void element.play().catch(() => {
    playing.value = false
  })
}

function onTime() {
  if (audio.value) current.value = audio.value.currentTime
}

function onLoaded() {
  if (audio.value) duration.value = Number(audio.value.duration) || 0
}

function seek(event: MouseEvent) {
  const element = audio.value
  if (!element || !duration.value) return
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
  element.currentTime = ratio * duration.value
  current.value = element.currentTime
}

function onPlay() {
  playing.value = true
  emit('play')
}

function onPause() {
  playing.value = false
  emit('pause')
}

function onEnded() {
  playing.value = false
  current.value = 0
  emit('ended')
}
</script>

<template>
  <div class="audio-player" :class="{ compact, playing }" :title="src">
    <button class="player-toggle" type="button" :aria-label="playing ? '暂停' : '播放'" @click="toggle">
      <Pause v-if="playing" :size="11" />
      <Play v-else :size="11" />
    </button>
    <span class="player-time">{{ formatClock(current) }}</span>
    <div class="player-track" role="slider" :aria-valuenow="Math.round(percent)" aria-valuemin="0" aria-valuemax="100" tabindex="0" @click="seek">
      <span :style="{ width: `${percent}%` }" />
    </div>
    <span class="player-time">{{ formatClock(duration) }}</span>
    <audio
      ref="audio"
      :src="src"
      preload="metadata"
      @play="onPlay"
      @pause="onPause"
      @ended="onEnded"
      @timeupdate="onTime"
      @loadedmetadata="onLoaded"
      @error="emit('error')"
    />
  </div>
</template>

<style scoped>
.audio-player {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 220px;
  min-width: 0;
  height: 26px;
}

.audio-player.compact {
  width: 150px;
}

.player-toggle {
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  border: 1px solid var(--rule-strong);
  border-radius: var(--radius);
  color: var(--ink-2);
}

.player-toggle:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.audio-player.playing .player-toggle {
  border-color: var(--accent);
  color: var(--accent);
}

.player-time {
  flex-shrink: 0;
  color: var(--ink-3);
  font-family: var(--font-mono);
  font-size: 9px;
  font-variant-numeric: tabular-nums;
}

.player-track {
  position: relative;
  flex: 1;
  min-width: 24px;
  height: 12px;
  display: flex;
  align-items: center;
  cursor: pointer;
  background: linear-gradient(var(--rule-strong), var(--rule-strong)) center / 100% 2px no-repeat;
}

.player-track > span {
  position: relative;
  height: 2px;
  background: var(--accent);
  max-width: 100%;
}

.audio-player audio {
  display: none;
}
</style>
