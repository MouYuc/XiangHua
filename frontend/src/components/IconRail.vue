<!-- frontend/src/components/IconRail.vue -->
<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import {
  AudioWaveform,
  BrainCircuit,
  ChevronLeft,
  ChevronRight,
  Clock,
  MicVocal,
  SlidersHorizontal,
} from '@lucide/vue'
import type { LucideIcon } from '@lucide/vue'

type RailPage = 'synthesis' | 'training' | 'history' | 'settings' | 'timbre'

const RAIL_WIDTH_KEY = 'xianghua.rail-width'
const DEFAULT_EXPANDED_RAIL_WIDTH = 168
const MIN_EXPANDED_RAIL_WIDTH = 148
const MAX_EXPANDED_RAIL_WIDTH = 300
const RAIL_COLLAPSE_WIDTH = 104

defineProps<{ page: RailPage; queueBadge?: string | null; queueProgress?: number | null }>()
const emit = defineEmits<{ (event: 'update:page', value: RailPage): void }>()

const navItems: { key: RailPage; icon: LucideIcon; label: string }[] = [
  { key: 'synthesis', icon: AudioWaveform, label: '合成' },
  { key: 'training', icon: BrainCircuit, label: '训练' },
  { key: 'timbre', icon: MicVocal, label: '音色' },
  { key: 'history', icon: Clock, label: '历史' },
  { key: 'settings', icon: SlidersHorizontal, label: '设置' },
]
const expanded = ref(readExpandedPreference())
const expandedWidth = ref(readRailWidth())
const resizing = ref(false)
let resizeStartX = 0
let resizeStartWidth = DEFAULT_EXPANDED_RAIL_WIDTH
let resizePointerId: number | null = null

const railWidth = () => (expanded.value ? expandedWidth.value : 44)

function readExpandedPreference() {
  try {
    if (typeof globalThis.localStorage === 'undefined') return false
    return globalThis.localStorage.getItem('xianghua.rail-expanded') === '1'
  } catch {
    return false
  }
}
function readRailWidth() {
  try {
    if (typeof globalThis.localStorage === 'undefined') return DEFAULT_EXPANDED_RAIL_WIDTH
    const storedWidth = globalThis.localStorage.getItem(RAIL_WIDTH_KEY)
    if (!storedWidth) return DEFAULT_EXPANDED_RAIL_WIDTH
    const width = Number(storedWidth)
    return Number.isFinite(width) ? clampRailWidth(width) : DEFAULT_EXPANDED_RAIL_WIDTH
  } catch {
    return DEFAULT_EXPANDED_RAIL_WIDTH
  }
}
function clampRailWidth(width: number) {
  return Math.min(MAX_EXPANDED_RAIL_WIDTH, Math.max(MIN_EXPANDED_RAIL_WIDTH, Math.round(width)))
}
function persistExpanded() {
  try {
    if (typeof globalThis.localStorage !== 'undefined') {
      globalThis.localStorage.setItem('xianghua.rail-expanded', expanded.value ? '1' : '0')
    }
  } catch {
    // Storage can be unavailable in private browsing modes; expansion stays session-only.
  }
}
function persistRailWidth() {
  try {
    if (typeof globalThis.localStorage !== 'undefined') {
      globalThis.localStorage.setItem(RAIL_WIDTH_KEY, String(expandedWidth.value))
    }
  } catch {
    // Width is a convenience setting, so storage failure can be ignored.
  }
}
function toggleExpanded() {
  if (!expanded.value) expandedWidth.value = clampRailWidth(expandedWidth.value)
  expanded.value = !expanded.value
  persistExpanded()
}
function onResizeStart(event: PointerEvent) {
  if (event.button !== 0 || resizing.value) return
  const target = event.currentTarget as HTMLElement | null
  if (!target) return
  resizing.value = true
  resizeStartX = event.clientX
  resizeStartWidth = expandedWidth.value
  resizePointerId = event.pointerId
  target.setPointerCapture(event.pointerId)
  event.preventDefault()
}
function onResizeMove(event: PointerEvent) {
  if (!resizing.value) return
  const rawWidth = resizeStartWidth + resizeStartX - event.clientX
  if (rawWidth <= RAIL_COLLAPSE_WIDTH) {
    if (expanded.value) {
      expanded.value = false
      persistExpanded()
    }
    expandedWidth.value = MIN_EXPANDED_RAIL_WIDTH
  } else {
    if (!expanded.value) {
      expanded.value = true
      persistExpanded()
    }
    expandedWidth.value = clampRailWidth(rawWidth)
  }
}
function onResizeEnd(event: PointerEvent) {
  if (!resizing.value) return
  const target = event.currentTarget as HTMLElement | null
  resizing.value = false
  resizePointerId = null
  if (target?.hasPointerCapture(event.pointerId)) {
    target.releasePointerCapture(event.pointerId)
  }
  if (expanded.value) persistRailWidth()
}
function onResizeKeydown(event: KeyboardEvent) {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
  event.preventDefault()
  const step = event.key === 'ArrowLeft' ? -16 : 16
  expanded.value = true
  expandedWidth.value = clampRailWidth(expandedWidth.value + step)
  persistExpanded()
  persistRailWidth()
}

onBeforeUnmount(() => {
  resizing.value = false
  resizePointerId = null
})
</script>

<template>
  <nav
    class="icon-rail" :class="{ expanded, dragging: resizing }"
    :style="{ width: `${railWidth()}px`, '--rail-w': `${railWidth()}px` }"
    aria-label="主导航"
  >
    <button
      class="rail-btn rail-toggle"
      type="button"
      :title="expanded ? '收起导航' : '展开导航'"
      :aria-label="expanded ? '收起导航' : '展开导航'"
      :aria-expanded="expanded"
      aria-controls="rail-items"
      @click="toggleExpanded"
    >
      <component :is="expanded ? ChevronRight : ChevronLeft" class="rail-glyph" aria-hidden="true" />
      <span class="rail-label">{{ expanded ? '收起' : '展开' }}</span>
      <span class="rail-tip">{{ expanded ? '收起导航' : '展开导航' }}</span>
    </button>
    <div id="rail-items" class="icon-rail-body">
      <div class="icon-rail-nav">
        <button
          v-for="item in navItems" :key="item.key"
          class="rail-btn" :class="{ active: page === item.key }"
          :title="item.key === 'synthesis' && queueBadge ? `合成 · 队列 ${queueBadge}` : item.label"
          :aria-label="item.key === 'synthesis' && queueBadge ? `合成，队列 ${queueBadge} 项` : item.label"
          :aria-current="page === item.key ? 'page' : undefined"
          @click="emit('update:page', item.key)"
        >
          <component :is="item.icon" class="rail-glyph" aria-hidden="true" />
          <span
            v-if="item.key === 'synthesis' && queueProgress !== null && queueProgress !== undefined"
            class="rail-progress"
            :style="{ '--rail-progress': `${Math.min(100, Math.max(0, Math.round(queueProgress)))}%` }"
            aria-hidden="true"
          />
          <span class="rail-label">{{ item.label }}</span>
          <span class="rail-tip">{{ item.key === 'synthesis' && queueBadge ? `合成 · 队列 ${queueBadge}` : item.label }}</span>
          <span v-if="item.key === 'synthesis' && queueBadge" class="rail-badge">{{ queueBadge }}</span>
        </button>
      </div>
    </div>
    <div
      v-if="expanded || resizing"
      class="rail-resizer"
      :class="{ dragging: resizing }"
      role="separator"
      aria-orientation="vertical"
      aria-label="拖动调整导航宽度"
      tabindex="0"
      :aria-valuenow="expandedWidth"
      :aria-valuemin="MIN_EXPANDED_RAIL_WIDTH"
      :aria-valuemax="MAX_EXPANDED_RAIL_WIDTH"
      @pointerdown="onResizeStart"
      @pointermove="onResizeMove"
      @pointerup="onResizeEnd"
      @pointercancel="onResizeEnd"
      @keydown="onResizeKeydown"
    />
  </nav>
</template>
