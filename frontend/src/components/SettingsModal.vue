<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, listLexicon, upsertLexicon, deleteLexicon, type DoctorReport, type LexiconEntry } from '@/lib/api'
import { useConfig } from '@/composables/useConfig'
import { useTheme } from '@/composables/useTheme'
import { useToast } from '@/composables/useToast'

const emit = defineEmits<{ (event: 'close'): void }>()

const { config } = useConfig()
const { theme, reduceMotion, toggleTheme, toggleReduceMotion } = useTheme()
const toast = useToast()

const scanning = ref(false)
const envRows = ref<{ name: string; status: string; detail: string; fix: string }[]>([])
const repairing = ref(false)
const doctor = ref<DoctorReport | null>(null)

/**
 * Engine-root status shown next to the path.
 *
 * This used to be a hardcoded green "已检测" next to a "未配置" path — the dialog
 * asserted success before anything had been checked. Now it reflects reality:
 * configured or not, scanned or not, and whether the scan reported a problem.
 */
const rootStatus = computed(() => {
  if (!config.gpt_sovits_root) return { tone: 'warn', label: '未配置' }
  if (scanning.value) return { tone: 'mut', label: '检测中…' }
  if (doctor.value?.summary && !doctor.value.rows.every((row) => row.status === 'ok')) {
    return { tone: 'warn', label: '需检查' }
  }
  if (!envRows.value.length) return { tone: 'mut', label: '未检测' }
  return { tone: 'ok', label: '已检测' }
})

// —— 发音词典：多音字 / 专名读法修正，仅改读音不改字幕 ——
const lexicon = ref<LexiconEntry[]>([])
const lexTerm = ref('')
const lexReplacement = ref('')
const lexBusy = ref(false)

async function loadLexicon() {
  try {
    lexicon.value = (await listLexicon()).entries
  } catch {
    lexicon.value = []
  }
}

async function addLexiconEntry() {
  if (lexBusy.value || !lexTerm.value.trim() || !lexReplacement.value.trim()) return
  lexBusy.value = true
  try {
    const result = await upsertLexicon({ term: lexTerm.value, replacement: lexReplacement.value })
    toast.success(result.message)
    lexTerm.value = ''
    lexReplacement.value = ''
    await loadLexicon()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    lexBusy.value = false
  }
}

async function removeLexiconEntry(term: string) {
  try {
    await deleteLexicon(term)
    await loadLexicon()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

async function scanEnvironment() {
  scanning.value = true
  try {
    const data = await api.post<{ rows?: { name: string; status: string; detail: string; fix: string }[] }>('/api/environment/scan')
    envRows.value = data.rows ?? []
    toast.success('环境检测完成。')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    scanning.value = false
  }
}

async function runDoctor() {
  repairing.value = true
  try {
    doctor.value = await api.post<DoctorReport>('/api/doctor', { repair: false })
    toast.info(doctor.value.summary || '自检完成。')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    repairing.value = false
  }
}

async function unloadTimbre() {
  try {
    await api.post('/api/timbre/unload')
    toast.success('已释放音色转换显存。')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

function statusTag(status: string) {
  if (status === 'ok') return 'ok'
  if (status === 'warn') return 'warn'
  if (status === 'bad' || status === 'fail') return 'err'
  return 'mut'
}

onMounted(() => {
  void loadLexicon()
  void (async () => {
    try {
      const data = await api.get<{ rows: { name: string; status: string; detail: string; fix: string }[] }>('/api/environment')
      envRows.value = data.rows ?? []
    } catch {
      envRows.value = []
    }
  })()
})
</script>

<template>
  <div class="modal" role="dialog" aria-modal="true" aria-label="设置" @click.stop @keydown.esc="emit('close')">
    <div class="mh">
      <h2>设置</h2>
      <p>改动即自动保存。声音与素材相关操作都在对应页面内。</p>
    </div>
    <div class="mb" style="gap:0">
      <div class="set-row">
        <span class="k">引擎路径</span>
        <span class="v"><span class="path-chip">{{ config.gpt_sovits_root || '未配置' }}</span> <span class="tag" :class="rootStatus.tone">{{ rootStatus.label }}</span></span>
        <button class="mini" style="height:24px" :disabled="repairing" @click="runDoctor">{{ repairing ? '自检中…' : '自检' }}</button>
      </div>
      <div class="set-row">
        <span class="k">模型</span>
        <span class="v"><span class="path-chip">{{ config.gpt_model || '默认' }} + {{ config.sovits_model || '默认' }}</span></span>
      </div>
      <div class="set-row">
        <span class="k">环境</span>
        <span class="v" style="display:flex;flex-direction:column;gap:3px">
          <span v-for="row in envRows.slice(0, 4)" :key="row.name" style="display:flex;gap:8px;align-items:center">
            <span class="tag" :class="statusTag(row.status)">{{ row.name }}</span>
            <span style="color:var(--text3);font-size:11.5px">{{ row.detail }}</span>
          </span>
          <span v-if="!envRows.length" style="color:var(--text3);font-size:11.5px">尚未检测</span>
        </span>
        <button class="mini" style="height:24px" :disabled="scanning" @click="scanEnvironment">{{ scanning ? '检测中…' : '重新检测' }}</button>
      </div>
      <div class="set-row">
        <span class="k">发音词典</span>
        <span class="v" style="display:flex;flex-direction:column;gap:6px;flex:1;min-width:0">
          <span v-if="!lexicon.length" style="color:var(--text3);font-size:11.5px">暂无词条。多音字 / 游戏专名的读法修正（如 重创 → 虫创），只改读音不改字幕，下次生成即生效。</span>
          <span
            v-for="entry in lexicon"
            :key="entry.term"
            style="display:flex;gap:8px;align-items:center;font-size:11.5px"
          >
            <span class="mono" style="color:var(--text2)">{{ entry.term }} → {{ entry.replacement }}</span>
            <span v-if="entry.note" style="color:var(--text3)">{{ entry.note }}</span>
            <span v-if="!entry.enabled" class="tag warn">已停用</span>
            <span style="flex:1"></span>
            <button class="mini" style="height:20px" type="button" @click="removeLexiconEntry(entry.term)">删除</button>
          </span>
          <span style="display:flex;gap:6px">
            <input
              v-model="lexTerm"
              placeholder="原词，如 重创"
              style="flex:1;height:24px;padding:0 8px;font-size:11.5px;border:1px solid var(--border);background:transparent;color:inherit"
              @keydown.enter.prevent="addLexiconEntry"
            >
            <input
              v-model="lexReplacement"
              placeholder="读法，如 虫创"
              style="flex:1;height:24px;padding:0 8px;font-size:11.5px;border:1px solid var(--border);background:transparent;color:inherit"
              @keydown.enter.prevent="addLexiconEntry"
            >
            <button class="mini" style="height:24px" type="button" :disabled="lexBusy || !lexTerm.trim() || !lexReplacement.trim()" @click="addLexiconEntry">{{ lexBusy ? '保存中…' : '添加' }}</button>
          </span>
        </span>
      </div>
      <div class="set-row">
        <span class="k">主题</span>
        <span class="v">浅色 / 深色</span>
        <button class="toggle" :class="{ on: theme === 'dark' }" type="button" aria-label="切换主题" @click="toggleTheme" />
      </div>
      <div class="set-row">
        <span class="k">减少动效</span>
        <span class="v">关闭波形与过渡动画</span>
        <button class="toggle" :class="{ on: reduceMotion }" type="button" aria-label="减少动效" @click="toggleReduceMotion" />
      </div>
      <div class="set-row">
        <span class="k">释放显存</span>
        <span class="v">卸载音色转换编码器</span>
        <button class="mini" style="height:24px" @click="unloadTimbre">释放</button>
      </div>
      <p v-if="doctor" style="font-size:11.5px;color:var(--text3);padding:8px 0 0">{{ doctor.summary }}</p>
    </div>
    <div class="mf">
      <span>像话 XiangHua · 本地单用户 · AGPL-3.0</span>
      <button style="color:var(--text2)" @click="emit('close')">关闭</button>
    </div>
  </div>
</template>
