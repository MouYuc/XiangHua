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

/** A failed API call, carrying the HTTP status so callers can branch on it. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(message: string, status: number, code = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }

  /** 404 from a job-status route means the job is gone, not that the call failed. */
  get isMissing(): boolean {
    return this.status === 404
  }
}

/** Abort a request that the backend never answers, so spinners can end. */
const DEFAULT_TIMEOUT_MS = 30_000

/**
 * Uploading a recording or a video is bounded by the user's disk and by how long
 * they are willing to wait — not by the API's response time.
 *
 * The 30 s default is right for a JSON round trip and wrong for a large file:
 * on a slow disk the abort would fire mid-upload, which is a regression against
 * the pre-timeout behaviour where the request could take as long as it needed.
 * Every endpoint that streams a file body uses this instead.
 */
export const UPLOAD_TIMEOUT_MS = 10 * 60_000

export type RequestOptions = {
  method?: string
  body?: BodyInit | null
  /** Override for slow endpoints (uploads, model scans). */
  timeoutMs?: number
  /** Keep the request alive across a page unload (used by the autosave flush). */
  keepalive?: boolean
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...init } = options
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  const isForm = init.body instanceof FormData
  try {
    const response = await fetch(path, {
      ...init,
      signal: controller.signal,
      headers: isForm || !init.body ? undefined : { 'Content-Type': 'application/json' },
    })
    if (response.status === 204) return undefined as T
    const data = await response.json().catch(() => ({}))
    if (!response.ok) {
      const detail = typeof data?.detail === 'string' ? data.detail : ''
      throw new ApiError(detail || `请求失败（HTTP ${response.status}）`, response.status, String(data?.code ?? ''))
    }
    return data as T
  } catch (error) {
    // An abort surfaces as a DOMException; turn it into something the UI can show.
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError('请求超时，请检查后端是否仍在运行。', 0, 'TIMEOUT')
    }
    if (error instanceof TypeError) {
      throw new ApiError('无法连接后端服务，请确认它已启动。', 0, 'NETWORK')
    }
    throw error
  } finally {
    window.clearTimeout(timer)
  }
}

export const api = {
  get: <T>(path: string, options: RequestOptions = {}) => request<T>(path, options),
  post: <T>(path: string, body?: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'POST', body: body instanceof FormData ? body : JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'PUT', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}

/** 让调用方判断错误文案时不必重复写 `instanceof`。 */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return String(error)
}

/** 上传一次，后端在后台完成切片 / 质检 / 转写。 */
export const startReferenceAudioPrepare = (body: FormData) =>
  request<ReferenceAudioPrepareStart>('/api/reference-audio/prepare', {
    method: 'POST',
    body,
    // Uploading a long recording is streamed to disk before the job starts.
    timeoutMs: UPLOAD_TIMEOUT_MS,
  })

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

// —— 训练工作区（此前整条链路是 `any`，后端字段一改就要到运行时才炸） ——

export type TrainingStepState = {
  status: string
  title?: string
  message?: string
  progress?: { overall_percent?: number } & Record<string, unknown>
}
export type TrainingState = {
  steps?: Record<string, TrainingStepState>
  exp?: string
  job_id?: string
}
export type TrainingJobRow = { job_id: string } & Record<string, unknown>
export type EnvironmentRow = { name: string; status: string; detail: string; fix: string }
export type TrainingStatusResponse = {
  state?: TrainingState
  stats?: Record<string, number | string | null>
  running?: TrainingJobRow[]
  log?: { text: string; job_id?: string }
  environment?: EnvironmentRow[]
  last_job_id?: string
}
export type TrainingMetricRow = {
  step?: number
  step_idx?: number
  loss_d?: number
  loss_g?: number
} & Record<string, unknown>
export type CheckpointRow = { name?: string; step?: number } & Record<string, unknown>
export type TranscriptRow = { path: string; text: string; audio_url?: string }

// —— 用户发音词典（多音字 / 专名读法修正） ——

export type LexiconEntry = {
  term: string
  replacement: string
  note: string
  enabled: boolean
  updated_at: string
}

export const listLexicon = () => api.get<{ entries: LexiconEntry[] }>('/api/lexicon')

export const upsertLexicon = (body: { term: string; replacement: string; note?: string; enabled?: boolean }) =>
  api.post<{ message: string; entry: LexiconEntry }>('/api/lexicon', body)

export const deleteLexicon = (term: string) =>
  request<void>(`/api/lexicon?term=${encodeURIComponent(term)}`, { method: 'DELETE' })

// —— 整集交付导出（SRT + 整集音轨 + 角色分轨） ——

export type EpisodeExportResult = {
  message: string
  srt_url: string
  full_url: string
  stem_urls: Record<string, string>
  line_count: number
  failed: number
}

export const exportBatch = (jobId: string) =>
  api.post<EpisodeExportResult>('/api/batch/export', { job_id: jobId })

// —— 多角色对话编排 ——

export type DialogueOutput = {
  speaker: string
  line: string
  url?: string
  error?: string
  duration?: number
  voice_card_id?: string
}

export type DialogueSnapshot = {
  job_id: string
  status: string
  current: number
  total: number
  completed: number
  outputs: DialogueOutput[]
  message?: string
  error?: string
}

export const startDialogue = (script: string, roles: Record<string, string>) =>
  api.post<DialogueSnapshot>('/api/dialogue/synthesis', { script, roles })

export const dialogueStatus = (jobId: string) =>
  api.get<DialogueSnapshot>(`/api/dialogue/status/${encodeURIComponent(jobId)}`)

export const exportDialogue = (jobId: string) =>
  api.post<EpisodeExportResult>('/api/dialogue/export', { job_id: jobId })

// —— 溯源凭据校验 ——

export type ProvenanceResult = {
  valid: boolean
  reason: string
  manifest: Record<string, unknown> | null
}

export const verifyProvenance = (body: { path?: string; download_id?: string }) =>
  api.post<ProvenanceResult>('/api/provenance/verify', body)
