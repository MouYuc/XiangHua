/**
 * 个人音色转换（kNN-VC）的共享类型与纯逻辑。
 *
 * 页面外壳在 `components/TimbrePanel.vue`：这里只放能被单元测试直接调用的部分
 * （类型、格式化、状态文案、任务轮询）。这样「topk 解释」和「失败态」这类
 * 容易写错的产品约束可以脱离 DOM 验证。
 */

export type TimbreJobStatus = 'running' | 'done' | 'failed'
export type TimbreJobKind = 'convert' | 'profile'
export type TimbreSourceOrigin = 'synthesis' | 'upload'

export type TimbreConvertResult = {
  file_name: string
  url: string
  duration: number
  seconds_taken: number
  topk: number
  profile_id: string
  matching_frames: number
  cuda?: boolean
  free_gb?: number
  total_gb?: number
}

export type TimbreProfileBuildResult = {
  profile_id: string
  reference_files: number
  frames: number
  seconds: number
  cache_path: string
  cuda?: boolean
  free_gb?: number
  total_gb?: number
}

export type TimbreJobResult = Partial<TimbreConvertResult> & Partial<TimbreProfileBuildResult>

export type TimbreJob = {
  job_id: string
  kind: TimbreJobKind
  status: TimbreJobStatus
  phase: string
  message: string
  progress: number
  source?: string
  source_name?: string
  profile_id?: string
  topk?: number
  result?: TimbreJobResult
  error: string
  started_at?: string
  updated_at?: string
}

export type TimbreProfileStatus = {
  profile_id: string
  reference_files: number
  reference_bytes: number
  cache_path: string
  cache_ready: boolean
  cached_frames: number
  cached_seconds: number
  cache_digest: string
  cuda: boolean
  loaded: boolean
  device: string
  free_gb?: number
  total_gb?: number
}

export type TimbreSource = {
  label: string
  path: string
  download_id: string
  origin: TimbreSourceOrigin
  created_at: string
  bytes: number
}

export type TimbreVram = {
  cuda: boolean
  loaded: boolean
  device: string
  free_gb: number | null
  total_gb: number | null
}

export const TIMBRE_PROFILE_ID = 'current'
export const DEFAULT_TIMBRE_TOPK = 4
export const TIMBRE_TOPK_OPTIONS: number[] = [1, 2, 4, 8]
export const TIMBRE_TOPK_EXPLANATION =
  'k 越大音色越贴近目标，但会平均掉更多源帧细节；k 越小越保留源音频细节（尤其是唱歌的颤音与旋律）。'
/** 实测值：模型加载后空闲显存 6.94 GB → 3.99 GB（RTX 3070 Ti 8GB，约 2.95 GB）。 */
export const TIMBRE_MODEL_VRAM_GB = 2.95
export const TIMBRE_AUTO_RELEASE_NOTE = '长时间不用会自动释放：空闲约 10 分钟后模型自动卸载，显存交还给合成页。'
export const TIMBRE_VRAM_CONFLICT_NOTE =
  `模型加载后约占 ${TIMBRE_MODEL_VRAM_GB} GB 显存，会与合成页争抢 GPU；转换完成后可以手动释放。`

const TIMBRE_JOB_STATUS_LABELS: Record<string, string> = {
  running: '进行中',
  done: '已完成',
  failed: '失败',
}
const TIMBRE_JOB_KIND_LABELS: Record<string, string> = {
  convert: '音色转换',
  profile: '匹配池',
}
const TIMBRE_JOB_PHASE_LABELS: Record<string, string> = {
  prepare: '准备源音频',
  encode: '编码参考音',
  done: '完成',
  failed: '失败',
}

export function timbreJobStatusLabel(status: unknown): string {
  return TIMBRE_JOB_STATUS_LABELS[String(status ?? '')] || '未知'
}

export function timbreJobKindLabel(kind: unknown): string {
  return TIMBRE_JOB_KIND_LABELS[String(kind ?? '')] || '音色任务'
}

export function timbreJobPhaseLabel(phase: unknown): string {
  return TIMBRE_JOB_PHASE_LABELS[String(phase ?? '')] || String(phase ?? '') || '—'
}

export function timbreSourceOriginLabel(origin: unknown): string {
  if (origin === 'synthesis') return '合成结果'
  if (origin === 'upload') return '上传'
  return '其它来源'
}

export function isTimbreJobSettled(job: TimbreJob | null | undefined): boolean {
  return job?.status === 'done' || job?.status === 'failed'
}

/** job.progress 是 0~1；页面上的进度条用整数百分比。 */
export function timbreProgressPercent(job: TimbreJob | null | undefined): number {
  const raw = Number(job?.progress)
  if (!Number.isFinite(raw)) return 0
  return Math.round(Math.min(1, Math.max(0, raw)) * 100)
}

/**
 * 后端在无参考音频、格式不支持、显存不足时返回明确中文错误；
 * `error` 是技术注解，`message` 是给用户看的那句，优先显示前者、回落到后者。
 */
export function timbreErrorMessage(job: TimbreJob | null | undefined): string {
  const error = String(job?.error ?? '').trim()
  if (error) return error
  const message = String(job?.message ?? '').trim()
  return message || '音色任务失败，但没有返回原因。'
}

/** 后端在字段缺失时会给出 null，格式化必须先区分「没有值」和「值是 0」。 */
function isMissingMetric(value: unknown): boolean {
  return value === null || value === undefined || value === ''
}

export function formatTimbreBytes(bytes: unknown): string {
  if (isMissingMetric(bytes)) return '—'
  const value = Number(bytes)
  if (!Number.isFinite(value) || value <= 0) return '—'
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(value / 1024))} KB`
}

export function formatTimbreSeconds(seconds: unknown): string {
  if (isMissingMetric(seconds)) return '—'
  const value = Number(seconds)
  if (!Number.isFinite(value) || value < 0) return '—'
  if (value < 60) return `${value.toFixed(value < 10 ? 2 : 1)} 秒`
  const minutes = Math.floor(value / 60)
  return `${minutes} 分 ${(value - minutes * 60).toFixed(1)} 秒`
}

export function formatTimbreClock(seconds: unknown): string {
  if (isMissingMetric(seconds)) return '—'
  const value = Number(seconds)
  if (!Number.isFinite(value) || value < 0) return '—'
  return `${value.toFixed(2)}s`
}

export function formatTimbreGb(value: unknown): string {
  if (isMissingMetric(value)) return '—'
  const gb = Number(value)
  if (!Number.isFinite(gb)) return '—'
  return `${gb.toFixed(2)} GB`
}

export function formatTimbreCount(value: unknown): string {
  if (isMissingMetric(value)) return '—'
  const count = Number(value)
  if (!Number.isFinite(count)) return '—'
  return count.toLocaleString('zh-CN')
}

export function asTimbreConvertResult(result: TimbreJobResult | null | undefined): TimbreConvertResult | null {
  if (!result || typeof result.url !== 'string' || !result.url) return null
  return {
    file_name: String(result.file_name ?? ''),
    url: result.url,
    duration: Number(result.duration ?? 0),
    seconds_taken: Number(result.seconds_taken ?? 0),
    topk: Number(result.topk ?? 0),
    profile_id: String(result.profile_id ?? ''),
    matching_frames: Number(result.matching_frames ?? 0),
  }
}

export function asTimbreProfileResult(result: TimbreJobResult | null | undefined): TimbreProfileBuildResult | null {
  if (!result || result.frames === undefined) return null
  return {
    profile_id: String(result.profile_id ?? ''),
    reference_files: Number(result.reference_files ?? 0),
    frames: Number(result.frames ?? 0),
    seconds: Number(result.seconds ?? 0),
    cache_path: String(result.cache_path ?? ''),
  }
}

/** 任何带显存字段的响应（档案状态 / 任务结果 / 释放结果）都可以喂进来。 */
export type TimbreVramSource = {
  cuda?: boolean
  loaded?: boolean
  device?: string
  free_gb?: number
  total_gb?: number
}

export function timbreVramFrom(source: TimbreVramSource | null | undefined): TimbreVram {
  const free = Number(source?.free_gb)
  const total = Number(source?.total_gb)
  return {
    cuda: Boolean(source?.cuda),
    loaded: Boolean(source?.loaded),
    device: String(source?.device ?? (source?.cuda ? 'cuda' : 'cpu')),
    free_gb: Number.isFinite(free) ? free : null,
    total_gb: Number.isFinite(total) ? total : null,
  }
}

/** 空闲 10 分钟后后端会自动卸载模型，页面上如实说明，避免用户以为显存被永久占用。 */
export function timbreVramSummary(vram: TimbreVram): string {
  if (!vram.cuda) return '未检测到可用 GPU，转换会在 CPU 上运行（很慢）。'
  const free = vram.free_gb === null ? '—' : formatTimbreGb(vram.free_gb)
  const total = vram.total_gb === null ? '—' : formatTimbreGb(vram.total_gb)
  return vram.loaded
    ? `模型已加载 · 空闲 ${free} / 共 ${total}`
    : `模型未加载 · 空闲 ${free} / 共 ${total}`
}

export type TimbrePollerHandlers = {
  onUpdate?: (job: TimbreJob) => void
  onSettled?: (job: TimbreJob) => void
  onError?: (message: string) => void
}

export type TimbrePollerOptions = { intervalMs?: number; failureLimit?: number }
export type TimbrePoller = { stop: () => void }

/**
 * 轮询音色任务快照（`GET /api/timbre/status/{job_id}`）。
 *
 * 与合成页的轮询保持一致的行为：任务结束后停止，连续失败到上限也停止，
 * 避免页面被一个已经不存在的任务无限拖着请求。
 */
export function pollTimbreJob(
  jobId: string,
  fetchJob: (jobId: string) => Promise<TimbreJob>,
  handlers: TimbrePollerHandlers = {},
  options: TimbrePollerOptions = {},
): TimbrePoller {
  const intervalMs = Math.max(100, options.intervalMs ?? 900)
  const failureLimit = Math.max(1, options.failureLimit ?? 5)
  let active = true
  let timer: ReturnType<typeof setInterval> | null = null
  let failures = 0
  let inflight = false

  function stop() {
    active = false
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  async function tick() {
    if (!active || inflight) return
    inflight = true
    try {
      const job = await fetchJob(jobId)
      if (!active) return
      failures = 0
      handlers.onUpdate?.(job)
      if (isTimbreJobSettled(job)) {
        stop()
        handlers.onSettled?.(job)
      }
    } catch (error) {
      if (!active) return
      const message = error instanceof Error ? error.message : String(error)
      if (message.includes('不存在')) {
        stop()
        handlers.onError?.(message)
        return
      }
      failures += 1
      if (failures >= failureLimit) {
        stop()
        handlers.onError?.(message)
      }
    } finally {
      inflight = false
    }
  }

  void tick()
  timer = setInterval(() => { void tick() }, intervalMs)
  return { stop }
}