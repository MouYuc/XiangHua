import type { VoiceCard, VoiceCardCreateInput, VoiceCardPatchInput, VoiceOptions } from './types'
import { api } from './client'

export const fetchVoiceOptions = () => api.get<VoiceOptions>('/api/voice/options')

export const listVoiceCards = () => api.get<{ cards: VoiceCard[] }>('/api/voice-cards')

export const createVoiceCard = (body: VoiceCardCreateInput) =>
  api.post<{ message: string; card: VoiceCard }>('/api/voice-cards', body)

/** 停用（status:'retired'）/ 恢复（status:'active'）声线卡；字段缺省 = 不修改。 */
export const patchVoiceCard = (voiceId: string, body: VoiceCardPatchInput) =>
  api.patch<{ message: string; card: VoiceCard }>(
    `/api/voice-cards/${encodeURIComponent(voiceId)}`,
    body,
  )

export const activateVoiceCard = (voiceId: string) =>
  api.post<{ message: string; voice_card_id: string; active_version: string; applied: string[] }>(
    `/api/voice-cards/${encodeURIComponent(voiceId)}/activate`,
  )

/** 永久删除声线卡（含版本历史）；正在用于合成的卡会被后端拒绝。 */
export const deleteVoiceCard = (voiceId: string) =>
  api.delete<{ message: string; voice_id: string }>(
    `/api/voice-cards/${encodeURIComponent(voiceId)}`,
  )
