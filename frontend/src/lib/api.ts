import type { ReferenceReport } from '../composables/referenceAudio'

export type SynthesisLanguage = 'zh' | 'yue' | 'en' | 'ja' | 'ko'
export type AccentLevel = 'off' | 'light' | 'medium' | 'heavy'

export type Config = {
  gpt_sovits_root: string; gpt_model: string; sovits_model: string; ref_audio: string; ref_text: string
  text_lang: SynthesisLanguage; prompt_lang: SynthesisLanguage
  voice_profile_id: string; accent_id: string
  top_k: number; top_p: number; temperature: number; sample_steps: number; speed: number; pause_second: number
  mixed_eos_floor: number | null; mixed_token_cap: number | null
  auto_lightmid: boolean; accent_level: AccentLevel; theme: string
  orb_synthesis_token_color: string; orb_settled_color: string; orb_playback_token_color: string
}

export type VoiceProfile = { id: string; display_name: string; description: string }
export type AccentPack = {
  id: string; display_name: string; lang: string; description: string
  enabled: boolean; available: boolean
}
export type VoiceOptions = { profiles: VoiceProfile[]; accents: AccentPack[] }

export type VoiceCardVersionMeta = {
  version: string
  created_at: string
  notes: string
}
export type VoiceCard = {
  voice_id: string
  display_name: string
  language: string
  status: 'active' | 'retired' | string
  license_id: string | null
  consent_id: string | null
  created_at: string
  updated_at: string
  active_version: string
  version_count: number
  timbre_available: boolean
  versions: VoiceCardVersionMeta[]
}

export type JobRecord = {
  job_id: string
  kind: 'synthesis' | 'voice_prepare' | 'timbre' | 'training' | 'media_analysis'
  title: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'unknown'
  progress: number | null
  stage: string
  error: string
  started_at: string
  updated_at: string
  cancelable: boolean
  source: string
}
export type JobsResponse = {
  jobs: JobRecord[]
  counts: Partial<Record<JobRecord['status'], number>>
  by_kind: Partial<Record<JobRecord['kind'], number>>
  errors: string[]
}

export type HistoryEntry = {
  time: string
  text: string
  params: string
  name: string
  url: string
}

export type JobResult = { message: string; job_id?: string; log?: { job_id: string; text: string } }
export type SynthesisOutput = {
  text: string
  rendered_text: string
  url: string
  error: string
  output_index: number
  semantic_token_count: number
  semantic_frame_rate: number
  duration?: number
  warning?: string
}
export type DoctorAction = { kind: string; status: string; message: string; job_id?: string }
export type DoctorReport = {
  repair: boolean
  summary: string
  repaired: boolean
  selected: string
  rows: { name: string; status: string; detail: string; fix: string }[]
  actions: DoctorAction[]
  config: Partial<Config>
}
export type SynthesisSnapshot = {
  job_id: string
  status: string
  phase: string
  current: number
  total: number
  completed: number
  progress?: number
  current_text: string
  rendered_text: string
  output_dir: string
  outputs: SynthesisOutput[]
  error: string
  message?: string
  revision?: number
}
export type SynthesisStreamEvent =
  | { type: 'snapshot'; snapshot: SynthesisSnapshot; revision?: number }
  | { type: 'semantic_token'; output_index: number; token_index: number; semantic_frame_rate: number; revision?: number }
  | { type: 'output_completed'; output_index: number; semantic_token_count: number; semantic_frame_rate: number; revision?: number }
  | { type: 'output_failed'; output_index: number; error: string; revision?: number }
export type SynthesisReadiness = { status: 'idle' | 'warming' | 'ready' | 'failed'; message: string; error: string }
export type ReferenceAudioPrepareStart = { job_id: string; status: string }
export type ReferenceAudioPrepareResult = {
  source_path?: string
  ref_audio: string
  ref_text: string
  duration_seconds?: number
  source_duration_seconds?: number
  selected?: { start: number; end: number; duration: number; candidate_count?: number; score?: number }
  transcription?: Record<string, unknown>
  quality?: ReferenceReport
  warnings?: string[]
}
export type ReferenceAudioPrepareSnapshot = {
  job_id: string
  status: 'queued' | 'running' | 'done' | 'failed'
  phase: string
  progress: number
  stage: string
  result: ReferenceAudioPrepareResult | null
  error: string
  message: string
  started_at: string
  updated_at: string
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...options.headers } })
  if (response.status === 204) return undefined as T
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || '请求失败')
  return data as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body: body instanceof FormData ? body : JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}

/** 上传一次，后端在后台完成切片 / 质检 / 转写。 */
export const startReferenceAudioPrepare = (body: FormData) =>
  request<ReferenceAudioPrepareStart>('/api/reference-audio/prepare', { method: 'POST', body })

export const getReferenceAudioPrepareSnapshot = (jobId: string) =>
  request<ReferenceAudioPrepareSnapshot>(`/api/reference-audio/prepare/${encodeURIComponent(jobId)}`)

export const fetchVoiceOptions = () => api.get<VoiceOptions>('/api/voice/options')

export type VoiceCardCreateInput = {
  display_name: string
  language?: string
  notes?: string
  license_id?: string | null
  consent_id?: string | null
}

export const listVoiceCards = () => api.get<{ cards: VoiceCard[] }>('/api/voice-cards')

export const createVoiceCard = (body: VoiceCardCreateInput) =>
  api.post<{ message: string; card: VoiceCard }>('/api/voice-cards', body)

export const activateVoiceCard = (voiceId: string) =>
  api.post<{ message: string; voice_card_id: string; active_version: string; applied: string[] }>(
    `/api/voice-cards/${encodeURIComponent(voiceId)}/activate`,
  )

export const fetchJobs = (limit = 50) => api.get<JobsResponse>(`/api/jobs?limit=${limit}`)

export const cancelJob = (jobId: string) =>
  api.post<{ message: string }>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`)

export const fetchHistory = () => api.get<HistoryEntry[]>('/api/history')

export const clearHistory = () => api.delete<void>('/api/history')
