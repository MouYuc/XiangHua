<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { usePage } from '@/composables/usePage'
import { useVoices } from '@/composables/useVoices'
import { useConfig } from '@/composables/useConfig'

const emit = defineEmits<{
  (event: 'close'): void
  (event: 'open-settings'): void
  (event: 'create-voice'): void
}>()

const { setPage } = usePage()
const { profiles, loadVoiceOptions, avatarText } = useVoices()
const { config } = useConfig()

const input = ref<HTMLInputElement | null>(null)
const query = ref('')

type Item = { id: string; icon: 'gen' | 'txt' | 'plus' | 'workshop' | 'clock' | 'gear' | 'voice' | 'page'; label: string; hint?: string; run: () => void }

/** mono 类型小标签（D 稿的等宽小注风格）。 */
const TYPE_LABELS: Record<Item['icon'], string> = {
  gen: '操作',
  txt: '操作',
  plus: '新建',
  workshop: '工作坊',
  clock: '记录',
  gear: '设置',
  voice: '声音',
  page: '跳转',
}

const items = computed<Item[]>(() => {
  const all: Item[] = [
    { id: 'nav-workbench', icon: 'page', label: '打开工作台', run: () => setPage('workbench') },
    { id: 'nav-voices', icon: 'page', label: '打开声音库', run: () => setPage('voices') },
    { id: 'nav-history', icon: 'clock', label: '打开记录', run: () => setPage('history') },
    { id: 'create-voice', icon: 'plus', label: '新建声音', hint: '向导', run: () => emit('create-voice') },
    { id: 'workshop', icon: 'workshop', label: '打开声音工作坊', hint: '多人素材 → 训练', run: () => setPage('workshop') },
    { id: 'settings', icon: 'gear', label: '打开设置', run: () => emit('open-settings') },
  ]
  for (const profile of profiles.value) {
    all.push({
      id: `voice-${profile.id}`,
      icon: 'voice',
      label: profile.display_name,
      hint: profile.id === 'current' ? '内置' : '档案',
      run: () => {
        config.voice_profile_id = profile.id
        setPage('workbench')
      },
    })
  }
  const keyword = query.value.trim().toLowerCase()
  if (!keyword) return all
  return all.filter((item) => `${item.label}${item.hint ?? ''}`.toLowerCase().includes(keyword))
})

const selected = ref(0)

watch(items, () => {
  selected.value = 0
})

function move(step: number) {
  const total = items.value.length
  if (!total) return
  selected.value = (selected.value + step + total) % total
}

/** Run one item (by reference, not by index — a click must run what was clicked). */
function runItem(item: { run: () => void } | undefined) {
  if (!item) return
  item.run()
  emit('close')
}

function runSelected() {
  runItem(items.value[selected.value])
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    // The footer and the top-right chip both promise ESC closes the palette.
    event.preventDefault()
    emit('close')
  } else if (event.key === 'ArrowDown') {
    event.preventDefault()
    move(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    move(-1)
  } else if (event.key === 'Enter') {
    event.preventDefault()
    runSelected()
  }
}

function onDocumentKeydown(event: KeyboardEvent) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    emit('close')
  }
}

onMounted(() => {
  document.addEventListener('keydown', onDocumentKeydown)
  void loadVoiceOptions()
  void nextTick(() => input.value?.focus())
})

onUnmounted(() => {
  document.removeEventListener('keydown', onDocumentKeydown)
})

function avatarFor(item: Item) {
  return item.icon === 'voice' ? avatarText(item.label) : ''
}
</script>

<template>
  <div class="cmdk" role="dialog" aria-modal="true" aria-label="命令面板" @keydown="onKeydown">
    <div class="in">
      <svg class="ic sm" viewBox="0 0 24 24" style="color:var(--text3)"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>
      <input ref="input" v-model="query" placeholder="搜索操作、声音，或直接跳转…">
      <kbd>ESC</kbd>
    </div>
    <div class="list">
      <div class="sec">操作与跳转</div>
      <button
        v-for="(item, index) in items"
        :key="item.id"
        class="it"
        :class="{ sel: index === selected }"
        type="button"
        @mouseenter="selected = index"
        @click="runItem(item)"
      >
        <span v-if="item.icon === 'voice'" class="v-avatar" style="width:22px;height:22px;font-size:10px">{{ avatarFor(item) }}</span>
        <svg v-else-if="item.icon === 'plus'" class="ic sm" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>
        <svg v-else-if="item.icon === 'workshop'" class="ic sm" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20a6.5 6.5 0 0 1 13 0" /><circle cx="17.5" cy="9.5" r="2.5" /><path d="M15.5 14.5a5 5 0 0 1 6 4.9" /></svg>
        <svg v-else-if="item.icon === 'clock'" class="ic sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>
        <svg v-else-if="item.icon === 'gear'" class="ic sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h0a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55h0a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v0a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z" /></svg>
        <svg v-else-if="item.icon === 'page'" class="ic sm" viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 7h8M8 11h8M8 15h5" /></svg>
        <svg v-else class="ic sm" viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5z" style="fill:currentColor;stroke:none" /></svg>
        {{ item.label }}
        <span v-if="item.hint" class="d">{{ item.hint }}</span>
        <span class="ty mono">{{ TYPE_LABELS[item.icon] }}</span>
      </button>
      <div v-if="!items.length" class="sec" style="padding:12px 10px;text-transform:none;font-weight:400">没有匹配的结果</div>
    </div>
    <div class="ft"><span><kbd>↑</kbd><kbd>↓</kbd> 选择</span><span><kbd>↵</kbd> 执行</span><span><kbd>ESC</kbd> 关闭</span></div>
  </div>
</template>

<style scoped>
/* 弹窗遮罩统一：纯色纸墨遮罩，无模糊玻璃（深色主题沿用全局暗遮罩） */
:global(.overlay) {
  background: rgba(25, 24, 23, 0.32);
  backdrop-filter: none;
}

/* 选中行：--surface2 底 */
.cmdk .it.sel {
  background: var(--surface2);
}

/* mono 类型小标签（右侧对齐） */
.cmdk .it .ty {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: 0.08em;
  color: var(--text3);
  flex: none;
}
.cmdk .it .d {
  margin-left: 0;
  font-size: 11px;
  color: var(--text3);
}
</style>
