import { computed, reactive, ref, watch } from 'vue'
import { api, type AccentLevel, type Config, type SynthesisLanguage } from '@/lib/api'
import { useToast } from './useToast'

export const LANGUAGE_OPTIONS: { value: SynthesisLanguage; label: string }[] = [
  { value: 'zh', label: '中文' },
  { value: 'yue', label: '粤语' },
  { value: 'en', label: '英语' },
  { value: 'ja', label: '日语' },
  { value: 'ko', label: '韩语' },
]

export const ACCENT_OPTIONS: { value: AccentLevel; label: string; hint: string }[] = [
  { value: 'off', label: '关闭', hint: '标准普通话' },
  { value: 'light', label: '轻', hint: '只替换无歧义短语' },
  { value: 'medium', label: '中', hint: '口语词替换' },
  { value: 'heavy', label: '浓', hint: '完整广普写法' },
]

const DEFAULT_CONFIG: Config = {
  gpt_sovits_root: '',
  gpt_model: '',
  sovits_model: '',
  ref_audio: '',
  ref_text: '',
  text_lang: 'zh',
  prompt_lang: 'zh',
  voice_profile_id: 'current',
  accent_id: 'standard',
  top_k: 30,
  top_p: 0.88,
  temperature: 0.95,
  sample_steps: 64,
  speed: 1,
  pause_second: 0.15,
  mixed_eos_floor: null,
  mixed_token_cap: null,
  auto_lightmid: false,
  accent_level: 'off',
  theme: 'workbench',
  orb_synthesis_token_color: '#75BBFF',
  orb_settled_color: '#69DDA4',
  orb_playback_token_color: '#F8FBFF',
}

/** 只有这些字段参与自动保存，避免把后端回填的只读字段写回去。 */
const AUTOSAVE_KEYS = [
  'gpt_sovits_root',
  'gpt_model',
  'sovits_model',
  'ref_audio',
  'ref_text',
  'prompt_lang',
  'voice_profile_id',
  'accent_id',
  'top_k',
  'top_p',
  'temperature',
  'sample_steps',
  'speed',
  'pause_second',
  'mixed_eos_floor',
  'mixed_token_cap',
  'accent_level',
] as const

const config = reactive<Config>({ ...DEFAULT_CONFIG })
const loaded = ref(false)
const saving = ref(false)
const savedAt = ref<number | null>(null)
const loadError = ref('')
const toast = useToast()

let hydrating = true
let saveTimer: number | null = null
let persistedSignature = ''

const signature = computed(() => AUTOSAVE_KEYS.map((key) => String(config[key] ?? '')).join('\u0001'))
const voiceConfigured = computed(() => Boolean(config.ref_audio?.trim()))

function normalize() {
  config.text_lang = 'zh'
  config.theme = 'workbench'
  if (!ACCENT_OPTIONS.some((option) => option.value === config.accent_level)) {
    config.accent_level = config.auto_lightmid ? 'medium' : 'off'
  }
}

async function loadConfig() {
  try {
    const data = await api.get<Config>('/api/config')
    hydrating = true
    Object.assign(config, data)
    normalize()
    loaded.value = true
    loadError.value = ''
    persistedSignature = signature.value
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : String(error)
    toast.error(`读取配置失败：${loadError.value}`)
  } finally {
    hydrating = false
  }
}

async function persistConfig() {
  if (!loaded.value) return
  saving.value = true
  try {
    await api.put<Config>('/api/config', { ...config })
    savedAt.value = Date.now()
    persistedSignature = signature.value
  } catch (error) {
    toast.error(`保存配置失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    saving.value = false
  }
}

function scheduleSave() {
  if (hydrating || !loaded.value) return
  if (signature.value === persistedSignature) return
  if (saveTimer !== null) window.clearTimeout(saveTimer)
  saveTimer = window.setTimeout(() => {
    saveTimer = null
    void persistConfig()
  }, 650)
}

watch(signature, scheduleSave)

function flushSave() {
  if (saveTimer !== null) {
    window.clearTimeout(saveTimer)
    saveTimer = null
    void persistConfig()
  }
}

export function useConfig() {
  return {
    config,
    loaded,
    saving,
    savedAt,
    loadError,
    voiceConfigured,
    loadConfig,
    persistConfig,
    flushSave,
  }
}
