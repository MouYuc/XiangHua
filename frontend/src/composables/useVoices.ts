import { computed, ref } from 'vue'
import {
  fetchVoiceOptions,
  listVoiceCards,
  type AccentPack,
  type VoiceCard,
  type VoiceProfile,
} from '@/lib/api'

/**
 * 声音库数据源：GET /api/voice/options（内置档案 + 口音包）+ GET /api/voice-cards
 * （版本化声线卡，建档向导写入的命名声音在这里）。
 *
 * 任一来源加载失败都降级成内置 "current"，页面永远可用。
 */

const profiles = ref<VoiceProfile[]>([])
const accents = ref<AccentPack[]>([])
const cards = ref<VoiceCard[]>([])
const loaded = ref(false)
const loading = ref(false)
const error = ref('')

async function loadVoiceOptions() {
  if (loading.value) return
  loading.value = true
  try {
    const data = await fetchVoiceOptions()
    profiles.value = Array.isArray(data.profiles) ? data.profiles : []
    accents.value = Array.isArray(data.accents) ? data.accents : []
    error.value = ''
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
    if (!profiles.value.some((item) => item.id === 'current')) {
      profiles.value = [{ id: 'current', display_name: '当前音色', description: '内置当前参考音' }]
    }
  }
  try {
    const data = await listVoiceCards()
    cards.value = Array.isArray(data.cards) ? data.cards : []
  } catch {
    cards.value = []
  }
  loaded.value = true
  loading.value = false
}

function profileById(id: string) {
  return profiles.value.find((item) => item.id === id) ?? null
}

function displayName(profile: VoiceProfile | null) {
  return profile?.display_name || '未选择声音'
}

/** 初始字母头像，与设计稿一致：取显示名首个字。 */
function avatarText(name: string) {
  return (name || '声').trim().slice(0, 1) || '声'
}

export function useVoices() {
  const voiceCount = computed(() => cards.value.length)
  return { profiles, accents, cards, loaded, loading, error, voiceCount, loadVoiceOptions, profileById, displayName, avatarText }
}
