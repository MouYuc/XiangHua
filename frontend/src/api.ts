export type SynthesisLanguage = 'zh' | 'yue' | 'en' | 'ja' | 'ko'

export type Config = {
  gpt_sovits_root: string; gpt_model: string; sovits_model: string; ref_audio: string; ref_text: string
  text_lang: SynthesisLanguage; prompt_lang: SynthesisLanguage
  top_k: number; top_p: number; temperature: number; sample_steps: number; speed: number; pause_second: number
  mixed_eos_floor: number | null; mixed_token_cap: number | null
  auto_lightmid: boolean; theme: string
  orb_synthesis_token_color: string; orb_settled_color: string; orb_playback_token_color: string
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
