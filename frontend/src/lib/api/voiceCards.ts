import type { VoiceCard, VoiceCardCreateInput, VoiceOptions } from './types'
import { api } from './client'

export const fetchVoiceOptions = () => api.get<VoiceOptions>('/api/voice/options')

export const listVoiceCards = () => api.get<{ cards: VoiceCard[] }>('/api/voice-cards')

export const createVoiceCard = (body: VoiceCardCreateInput) =>
  api.post<{ message: string; card: VoiceCard }>('/api/voice-cards', body)

export const activateVoiceCard = (voiceId: string) =>
  api.post<{ message: string; voice_card_id: string; active_version: string; applied: string[] }>(
    `/api/voice-cards/${encodeURIComponent(voiceId)}/activate`,
  )
