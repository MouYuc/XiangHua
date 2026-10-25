<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { usePage } from '@/composables/usePage'
import { useConfig } from '@/composables/useConfig'
import { useSynthesis } from '@/composables/useSynthesis'
import { useVoices } from '@/composables/useVoices'
import { useTheme } from '@/composables/useTheme'
import TaskCenter from '@/components/TaskCenter.vue'
import CommandPalette from '@/components/CommandPalette.vue'
import SettingsModal from '@/components/SettingsModal.vue'
import ToastStack from '@/components/ToastStack.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'
import VoicesView from '@/views/VoicesView.vue'
import VoiceWizardView from '@/views/VoiceWizardView.vue'
import WorkshopView from '@/views/WorkshopView.vue'
import HistoryView from '@/views/HistoryView.vue'

const { page, meta, setPage, navPages } = usePage()
const configStore = useConfig()
const { config, loadConfig, savedAt } = configStore
const synthesis = useSynthesis()
const voices = useVoices()
const { theme, toggleTheme } = useTheme()

const settingsOpen = ref(false)
const cmdkOpen = ref(false)

const NAV_ICONS: Record<string, string> = {
  workbench: 'M15.5 8.5a3.5 3.5 0 1 0-7 0|M4 12h16v3a8 8 0 0 1-16 0z|M12 3v2',
  voices: 'M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z|M19 10v2a7 7 0 0 1-14 0v-2|M12 19v3',
  history: 'M12 3a9 9 0 1 0 9 9|M12 7v5l3 2',
}
const NAV_LABELS: Record<string, string> = { workbench: '工作台', voices: '声音库', history: '记录' }

const savedAtText = computed(() => {
  if (!savedAt.value) return ''
  const at = new Date(savedAt.value)
  return `已自动保存 · ${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
})

const engineState = computed(() => {
  if (synthesis.busy.value) return { tone: 'busy', label: '引擎合成中' }
  if (synthesis.readiness.value.status === 'ready') return { tone: '', label: '引擎就绪' }
  if (synthesis.readiness.value.status === 'warming') return { tone: 'busy', label: '引擎预热中' }
  if (synthesis.readiness.value.status === 'failed') return { tone: 'bad', label: '引擎未就绪' }
  return { tone: '', label: '引擎待机' }
})

watch(cmdkOpen, (open) => {
  if (open) void voices.loadVoiceOptions()
})

onMounted(() => {
  void loadConfig()
  synthesis.initSynthesis()
  void voices.loadVoiceOptions()
})
</script>

<template>
  <div class="app-shell">
    <!-- 侧栏 -->
    <aside class="side">
      <div class="brand"><span class="brand-mark">话</span><b>像话</b></div>
      <nav class="side-nav">
        <button
          v-for="item in navPages"
          :key="item"
          type="button"
          :class="{ active: page === item }"
          @click="setPage(item)"
        >
          <svg class="ic sm" viewBox="0 0 24 24">
            <path v-for="(d, index) in (NAV_ICONS[item] || '').split('|')" :key="index" :d="d" />
          </svg>
          {{ NAV_LABELS[item] }}
          <span v-if="item === 'voices' && voices.voiceCount.value" class="n">{{ voices.voiceCount.value }}</span>
        </button>
      </nav>
      <div class="side-foot">
        <button class="eng" type="button" @click="settingsOpen = true">
          <span class="st" :class="engineState.tone"></span>{{ engineState.label }} · v2Pro
        </button>
        <div class="side-foot-row">
          <button class="icon-btn" type="button" title="切换亮/暗主题" @click="toggleTheme">
            <svg v-if="theme === 'dark'" class="ic sm" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg>
            <svg v-else class="ic sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
          </button>
          <button class="icon-btn" type="button" title="设置" @click="settingsOpen = true">
            <svg class="ic sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h0a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55h0a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v0a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z" /></svg>
          </button>
        </div>
      </div>
    </aside>

    <div class="main-col">
      <!-- 顶栏 -->
      <header class="topbar">
        <div class="crumb">{{ meta.crumb }}</div>
        <span v-if="savedAtText" class="autosave">{{ savedAtText }}</span>
        <div class="top-right">
          <button class="kmd-btn" type="button" @click="cmdkOpen = true">
            搜索或跳转… <kbd>⌘K</kbd>
          </button>
          <TaskCenter />
          <span class="avatar-me">Z</span>
        </div>
      </header>

      <div class="content-col">
        <KeepAlive include="WorkbenchView">
          <WorkbenchView v-if="page === 'workbench'" />
          <VoicesView v-else-if="page === 'voices'" />
          <VoiceWizardView v-else-if="page === 'wizard'" />
          <WorkshopView v-else-if="page === 'workshop'" />
          <HistoryView v-else />
        </KeepAlive>
      </div>
    </div>

    <!-- 弹窗 -->
    <div v-if="settingsOpen" class="overlay" @click.self="settingsOpen = false">
      <SettingsModal @close="settingsOpen = false" />
    </div>
    <div v-if="cmdkOpen" class="overlay" style="padding-top:14vh" @click.self="cmdkOpen = false">
      <CommandPalette
        @close="cmdkOpen = false"
        @open-settings="cmdkOpen = false; settingsOpen = true"
        @create-voice="cmdkOpen = false; setPage('wizard')"
      />
    </div>

    <ToastStack />
  </div>
</template>
