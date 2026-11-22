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

const { page, meta, setPage, navPages, isVoiceSubFlow } = usePage()
const configStore = useConfig()
const { config, loadConfig, savedAt } = configStore
const synthesis = useSynthesis()
const voices = useVoices()
const { theme, toggleTheme } = useTheme()

const settingsOpen = ref(false)
const cmdkOpen = ref(false)

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
    <div class="page">
      <!-- 顶部导航（D 稿） -->
      <nav class="nav">
        <div class="brand">
          <b>像话</b>
          <span>XIANGHUA</span>
        </div>

        <div class="nav-links">
          <button
            v-for="item in navPages"
            :key="item"
            type="button"
            :class="{ on: page === item || (item === 'voices' && isVoiceSubFlow) }"
            @click="setPage(item)"
          >
            {{ NAV_LABELS[item] }}
          </button>
        </div>

        <div class="nav-right">
          <span v-if="savedAtText" class="autosave">{{ savedAtText }}</span>

          <!-- 引擎状态（mono 小字状态点，点击打开设置） -->
          <button class="eng" type="button" :title="`${engineState.label} · v2Pro · 打开设置`" @click="settingsOpen = true">
            <span class="st" :class="engineState.tone"></span>{{ engineState.label }}
          </button>

          <!-- 搜索 ⌘K -->
          <button class="kmd-btn" type="button" title="搜索或跳转 (⌘K)" @click="cmdkOpen = true">
            <svg class="ic sm" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" /><path d="M16.5 16.5 21 21" /></svg>
            <span class="kmd-txt">搜索</span>
            <kbd>⌘K</kbd>
          </button>

          <!-- 任务中心（徽标在组件内） -->
          <TaskCenter />

          <!-- 设置 -->
          <button class="icon-btn" type="button" title="设置" @click="settingsOpen = true">
            <svg class="ic sm" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="3.2" />
              <path d="M12 2.6v2.7M12 18.7v2.7M2.6 12h2.7M18.7 12h2.7M5.2 5.2l1.9 1.9M16.9 16.9l1.9 1.9M18.8 5.2l-1.9 1.9M7.1 16.9l-1.9 1.9" />
            </svg>
          </button>

          <!-- 主题切换（浅色纸面为主，功能保留） -->
          <button class="icon-btn" type="button" title="切换亮/暗主题" @click="toggleTheme">
            <svg v-if="theme === 'dark'" class="ic sm" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg>
            <svg v-else class="ic sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
          </button>
        </div>
      </nav>

      <main class="content">
        <KeepAlive include="WorkbenchView">
          <WorkbenchView v-if="page === 'workbench'" />
          <VoicesView v-else-if="page === 'voices'" />
          <VoiceWizardView v-else-if="page === 'wizard'" />
          <WorkshopView v-else-if="page === 'workshop'" />
          <HistoryView v-else />
        </KeepAlive>
      </main>
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
