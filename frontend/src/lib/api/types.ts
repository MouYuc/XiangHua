/**
 * 后端 API 的类型契约，按域分段。
 *
 * 只有类型，没有请求逻辑：请求核心在 `./client`，端点函数按域在同级模块。
 * 每个域的字段与 `backend/` 的响应逐字段对齐，后端一改这里就跟着改。
 */

// —— 合成 / 全局配置 ——

export type SynthesisLanguage = 'zh' | 'yue' | 'en' | 'ja' | 'ko'
export type AccentLevel = 'off' | 'light' | 'medium' | 'heavy'

export type Config = {
  gpt_sovits_root: string; gpt_model: string; sovits_model: string; ref_audio: string; ref_text: string
  text_lang: SynthesisLanguage; prompt_lang: SynthesisLanguage
  voice_profile_id: string; accent_id: string
  /** 当前引擎配置登记的声线卡 id；空串 = 未绑定（「当前音色」跟随配置）。 */
  voice_card_id?: string
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

// —— 声线卡 ——

export type VoiceCardVersionMeta = {
  version: string
  created_at: string
  notes: string
}
export type VoiceCardTimbre = {
  gpt_model?: string
  sovits_model?: string
  ref_audio?: string
  ref_text?: string
  prompt_lang?: string
}
export type VoiceCardDefaults = Record<string, unknown>

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
  /** 当前版本快照里的 timbre（backend `card_public_dict` 透出）。 */
  active_timbre?: VoiceCardTimbre | null
  active_defaults?: VoiceCardDefaults | null
}

export type VoiceCardCreateInput = {
  display_name: string
  language?: string
  notes?: string
  license_id?: string | null
  consent_id?: string | null
}

/** PATCH /api/voice-cards/{voice_id}：全部字段可选，缺省 = 不修改（backend VoiceCardUpdate）。 */
export type VoiceCardPatchInput = {
  display_name?: string
  status?: 'active' | 'retired'
  license_id?: string | null
  consent_id?: string | null
  active_version?: string
}

// —— 任务中心 ——

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

// —— 历史 ——

export type HistoryEntry = {
  time: string
  text: string
  params: string
  name: string
  url: string
}

// —— 合成输出 / 健康检查 ——

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

// —— 参考音（含质量门禁报告；判定阈值在后端，前端如实呈现） ——

export type ReferenceCheckStatus = 'ok' | 'warn' | 'fail'
export type ReferenceStatus = ReferenceCheckStatus | 'unknown'

export type ReferenceCheck = {
  key: string
  label: string
  status: ReferenceCheckStatus
  measured: unknown
  threshold: string
  message: string
}

export type ReferenceMetrics = {
  duration_seconds?: number
  sample_rate?: number
  channels?: number
  clipping_ratio?: number
  clipping_run?: number
  dc_offset?: number
  silence_ratio?: number
  speech_spans?: number
  estimated_snr_db?: number | null
  quiet_headroom_db?: number | null
  speech_level_dbfs?: number | null
  noise_floor_dbfs?: number | null
  rolloff_99_hz?: number | null
  high_band_db?: number | null
  high_band_ratio?: number | null
  peak_dbfs?: number
  rms_dbfs?: number
  crest_db?: number
  f0?: {
    analyzed_seconds: number
    voiced_ratio: number
    f0_median_hz: number
    f0_p05_hz: number
    f0_p95_hz: number
    f0_range_semitones: number
  }
}

export type ReferenceReport = {
  path?: string
  purpose: string
  purpose_label?: string
  status: ReferenceStatus
  duration_seconds?: number
  sample_rate?: number
  channels?: number
  metrics?: ReferenceMetrics
  checks?: ReferenceCheck[]
  notes?: string[]
  blocking?: string[]
  warnings?: string[]
  blocking_checks?: string[]
  warning_checks?: string[]
  total_seconds?: number
  files?: number
  aggregate?: ReferenceMetrics
  per_file_counts?: Record<string, number>
  worst_file?: { path: string; status: ReferenceStatus; checks: ReferenceCheck[] }
  /** Only present when the backend could not analyse the clip at all. */
  error?: string
}

export type ReferenceInspectKind = 'file' | 'corpus'

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
  /** 引擎上报的全局步数（metrics.jsonl 的 global_step，画曲线的 x 轴）。 */
  global_step?: number | null
  epoch?: number
  stage?: string
  loss_d?: number | null
  loss_g?: number | null
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

// —— 整集交付导出（SRT + 整集音轨 + 角色分轨） ——

export type EpisodeExportResult = {
  message: string
  srt_url: string
  full_url: string
  stem_urls: Record<string, string>
  line_count: number
  failed: number
}

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

// —— 溯源凭据校验 ——

export type ProvenanceResult = {
  valid: boolean
  reason: string
  manifest: Record<string, unknown> | null
}
