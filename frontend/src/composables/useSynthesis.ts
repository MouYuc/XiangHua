import { computed, ref } from 'vue'
import {
  ApiError,
  api,
  type SynthesisOutput,
  type SynthesisReadiness,
  type SynthesisSnapshot,
  type SynthesisStreamEvent,
} from '@/lib/api'
import { createSynthesisStream, shouldApplySynthesisRevision, type SynthesisStreamController } from '@/lib/synthesisStream'
import { createPolling } from './usePolling'
import { useConfig } from './useConfig'
import { useToast } from './useToast'

const POLL_FAILURE_LIMIT = 5
/** Statuses after which the job will never change again. */
const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled'])
const configStore = useConfig()
const toast = useToast()

// 保存的配置里带着后端写入的声线卡/风格字段（激活卡时会落盘）。它们是
// "上次显式选择"的记录，不是本次请求的显式选择：原样回传会在卡片被删除后
// 让每次合成都 400。只有用户真正按卡/按风格发起合成时才显式传这两个字段。
const SERVER_MANAGED_CONFIG_KEYS = ['voice_card_id', 'voice_card_version', 'style_id'] as const

function synthesisRequestBody(text: string): Record<string, unknown> {
  const body: Record<string, unknown> = { ...configStore.config, text }
  for (const key of SERVER_MANAGED_CONFIG_KEYS) delete body[key]
  return body
}

const busy = ref(false)
const outputs = ref<SynthesisOutput[]>([])
const job = ref<SynthesisSnapshot | null>(null)
const retryingIndex = ref<number | null>(null)
const readiness = ref<SynthesisReadiness>({ status: 'warming', message: '模型预热中。', error: '' })

let stream: SynthesisStreamController | null = null
let latestRevision = -1
// The job id this composable is currently following. A status response or SSE
// event for any other job is discarded: without this, a poll issued 500 ms
// before `start()` resolved for the *previous* job and its `done` snapshot killed
// the new job's poller and EventSource while that job was still running.
let trackedJobId = ''

/** Non-empty, non-comment lines this run was asked to synthesise, in order. */
const generatedLines = ref<string[]>([])

function parseScriptLines(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '' && !line.startsWith('#'))
}

const progress = computed(() => {
  const total = Number(job.value?.total || 0)
  const completed = Number(job.value?.completed || 0)
  const streamProgress = job.value?.progress
  if (typeof streamProgress === 'number' && Number.isFinite(streamProgress)) {
    return Math.round(Math.min(100, Math.max(0, streamProgress * 100)))
  }
  return total ? Math.round(Math.min(100, Math.max(0, (completed / total) * 100))) : 0
})

const failedCount = computed(() => outputs.value.filter((item) => Boolean(item.error)).length)
const ready = computed(() => readiness.value.status === 'ready')
const statusLabel = computed(() => {
  if (busy.value) return '正在生成'
  if (readiness.value.status === 'warming') return '引擎预热中'
  if (readiness.value.status === 'failed') return '引擎未就绪'
  if (readiness.value.status === 'ready') return '引擎就绪'
  return '引擎待机'
})
const statusTone = computed<'ready' | 'busy' | 'failed' | 'idle'>(() => {
  if (busy.value) return 'busy'
  if (readiness.value.status === 'ready') return 'ready'
  if (readiness.value.status === 'failed') return 'failed'
  return 'idle'
})

function normalizeSnapshot(data: Partial<SynthesisSnapshot> & { outputs?: unknown[] } | null | undefined): SynthesisSnapshot {
  const items = Array.isArray(data?.outputs) ? data?.outputs : []
  return {
    job_id: String(data?.job_id || ''),
    status: String(data?.status || 'running'),
    phase: String(data?.phase || 'prepare'),
    current: Number(data?.current || 0),
    total: Number(data?.total || 0),
    completed: Number(data?.completed || 0),
    progress: typeof data?.progress === 'number' ? data.progress : undefined,
    current_text: String(data?.current_text || ''),
    rendered_text: String(data?.rendered_text || ''),
    output_dir: String(data?.output_dir || ''),
    outputs: items.map((raw, index) => {
      const item = raw as Record<string, unknown>
      return {
        text: String(item?.text || ''),
        rendered_text: String(item?.rendered_text || ''),
        url: String(item?.url || ''),
        error: String(item?.error || ''),
        output_index: Number.isFinite(Number(item?.output_index)) ? Number(item.output_index) : index,
        semantic_token_count: Math.max(0, Number(item?.semantic_token_count) || 0),
        semantic_frame_rate: Math.max(0, Number(item?.semantic_frame_rate) || 0),
      }
    }),
    error: String(data?.error || ''),
    message: data?.message,
  }
}

function stopTracking() {
  trackedJobId = ''
  statusPoll.stop()
  stream?.close()
  stream = null
}

function applySnapshot(data: Partial<SynthesisSnapshot> & { outputs?: unknown[] }) {
  const snapshot = normalizeSnapshot(data)
  job.value = snapshot

  const retryIndex = retryingIndex.value
  if (retryIndex === null) {
    outputs.value = snapshot.outputs
  } else if (snapshot.status === 'done' || snapshot.status === 'failed') {
    const replacement = snapshot.outputs[0]
    if (replacement) {
      outputs.value = outputs.value.map((item, index) => (index === retryIndex ? replacement : item))
    }
    retryingIndex.value = null
  }

  if (TERMINAL_STATUSES.has(snapshot.status)) {
    busy.value = false
    stopTracking()
    if (snapshot.status === 'done') {
      toast.success(`完成 ${snapshot.completed}/${snapshot.total} 条配音。`)
    } else if (snapshot.status === 'cancelled') {
      // Not an error: the user asked for this, and completed takes stay usable.
      toast.info(snapshot.message || '任务已取消。')
    } else {
      toast.error(snapshot.message || snapshot.error || '合成失败。')
    }
  }
}

function applyStreamEvent(event: SynthesisStreamEvent) {
  if (event.type === 'snapshot') {
    applySnapshot(event.snapshot)
  }
}

// 500 ms status polling, kept alongside the SSE stream on purpose: the stream
// carries semantic-token progress, the poll is the fallback when it dies. Both
// go through the same revision guard, so a late poll cannot overwrite a newer
// stream event (and vice versa). `createPolling` supplies the no-overlap and
// retry behaviour the hand-rolled interval was missing.
const statusPoll = createPolling({
  interval: 500,
  immediate: false,
  failLimit: POLL_FAILURE_LIMIT,
  run: async () => {
    const jobId = trackedJobId
    if (!jobId) return false
    try {
      const data = await api.get<SynthesisSnapshot & { revision?: number }>(
        `/api/synthesis/status/${jobId}`,
      )
      if (trackedJobId !== jobId) return false
      if (!shouldApplySynthesisRevision(data.revision, latestRevision)) return true
      if (Number.isFinite(Number(data.revision))) latestRevision = Number(data.revision)
      applySnapshot(data)
      return true
    } catch (error) {
      if (trackedJobId !== jobId) return false
      // 404 means the job is really gone; anything else may be a transient blip.
      if (error instanceof ApiError && error.status === 404) {
        stopTracking()
        busy.value = false
        job.value = null
        toast.error('合成任务已不存在，请重新生成。')
        return false
      }
      throw error
    }
  },
  onGiveUp: () => {
    stopTracking()
    busy.value = false
    job.value = null
    toast.error('读取合成进度失败，请稍后重试。')
  },
})

function startTracking(jobId: string) {
  stopTracking()
  trackedJobId = jobId
  statusPoll.start()
  stream = createSynthesisStream({
    jobId,
    createEventSource: (url) => new EventSource(url),
    onEvent: (raw) => {
      if (trackedJobId !== jobId) return
      try {
        const event = JSON.parse(raw) as SynthesisStreamEvent & { snapshot?: SynthesisSnapshot }
        const revision = event.revision ?? event.snapshot?.revision
        if (!shouldApplySynthesisRevision(revision, latestRevision)) return
        if (Number.isFinite(Number(revision))) latestRevision = Number(revision)
        applyStreamEvent(event)
      } catch {
        // 单条事件解析失败不打断轮询兜底。
      }
    },
  })
}

async function start(text: string) {
  if (busy.value) return
  if (!text.trim()) {
    toast.error('请先输入台词。')
    return
  }
  busy.value = true
  outputs.value = []
  job.value = null
  latestRevision = -1
  retryingIndex.value = null
  generatedLines.value = parseScriptLines(text)
  stopTracking()
  try {
    const data = await api.post<SynthesisSnapshot>('/api/synthesis', synthesisRequestBody(text))
    applySnapshot(data)
    if (data.job_id && !TERMINAL_STATUSES.has(data.status)) {
      startTracking(data.job_id)
    }
  } catch (error) {
    busy.value = false
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

async function retry(index: number) {
  const item = outputs.value[index]
  if (!item || busy.value || !item.text.trim()) return
  busy.value = true
  retryingIndex.value = index
  job.value = null
  latestRevision = -1
  // `generatedLines` is deliberately left alone. It is the key list the view maps
  // `outputs` against (text + ordinal), and a retry only replaces one slot:
  // shrinking the keys to the retried line made every other line unmappable and
  // made the retried line resolve to `outputs[0]` — i.e. the first line's audio.
  stopTracking()
  try {
    const data = await api.post<SynthesisSnapshot>('/api/synthesis', synthesisRequestBody(item.text))
    applySnapshot(data)
    if (data.job_id && !TERMINAL_STATUSES.has(data.status)) {
      startTracking(data.job_id)
    }
  } catch (error) {
    retryingIndex.value = null
    busy.value = false
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

function clearResults() {
  // No DOM poking from a composable: the view owns playback. It watches
  // `outputs` and stops its own <AudioPlayer> instances.
  outputs.value = []
  job.value = null
  latestRevision = -1
  generatedLines.value = []
  stopTracking()
}

// 模型预热状态。轮询在「已就绪/已失败」时收表，在 warming/idle（GPU 被占、
// 稍后自动重试）时继续，避免预热完成后界面还停在旧状态。
const readinessPoll = createPolling({
  interval: 1500,
  immediate: false,
  run: async () => {
    if (readiness.value.status === 'ready' || readiness.value.status === 'failed') return false
    await refreshReadiness()
    return true
  },
})

async function refreshReadiness() {
  try {
    readiness.value = await api.get<SynthesisReadiness>('/api/synthesis/readiness')
  } catch (error) {
    readiness.value = {
      status: 'failed',
      message: '无法读取模型状态。',
      error: error instanceof Error ? error.message : String(error),
    }
  }
}

async function retryWarmup() {
  try {
    readiness.value = await api.post<SynthesisReadiness>('/api/synthesis/readiness/retry')
    readinessPoll.start()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

function initSynthesis() {
  void refreshReadiness()
  readinessPoll.start()
}

export function useSynthesis() {
  return {
    busy,
    outputs,
    generatedLines,
    job,
    retryingIndex,
    readiness,
    progress,
    failedCount,
    ready,
    statusLabel,
    statusTone,
    start,
    retry,
    clearResults,
    refreshReadiness,
    retryWarmup,
    initSynthesis,
  }
}
