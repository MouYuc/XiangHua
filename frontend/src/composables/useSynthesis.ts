import { computed, ref } from 'vue'
import {
  api,
  type SynthesisOutput,
  type SynthesisReadiness,
  type SynthesisSnapshot,
  type SynthesisStreamEvent,
} from '@/lib/api'
import { createSynthesisStream, shouldApplySynthesisRevision, type SynthesisStreamController } from '@/lib/synthesisStream'
import { useConfig } from './useConfig'
import { useToast } from './useToast'

const POLL_FAILURE_LIMIT = 5
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

let poller: number | undefined
let readinessPoller: number | undefined
let stream: SynthesisStreamController | null = null
let latestRevision = -1
let pollFailures = 0

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
  if (poller) {
    window.clearInterval(poller)
    poller = undefined
  }
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

  if (snapshot.status === 'done' || snapshot.status === 'failed') {
    busy.value = false
    stopTracking()
    if (snapshot.status === 'done') {
      toast.success(`完成 ${snapshot.completed}/${snapshot.total} 条配音。`)
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

async function pollStatus(jobId: string) {
  try {
    const data = await api.get<SynthesisSnapshot & { revision?: number }>(`/api/synthesis/status/${jobId}`)
    pollFailures = 0
    if (!shouldApplySynthesisRevision(data.revision, latestRevision)) return
    if (Number.isFinite(Number(data.revision))) latestRevision = Number(data.revision)
    applySnapshot(data)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    pollFailures += 1
    const missing = message.includes('合成任务不存在')
    if (!missing && pollFailures < POLL_FAILURE_LIMIT) return
    stopTracking()
    busy.value = false
    job.value = null
    toast.error(missing ? '合成任务已不存在，请重新生成。' : '读取合成进度失败，请稍后重试。')
  }
}

function startTracking(jobId: string) {
  stopTracking()
  pollFailures = 0
  poller = window.setInterval(() => void pollStatus(jobId), 500)
  void pollStatus(jobId)
  stream = createSynthesisStream({
    jobId,
    createEventSource: (url) => new EventSource(url),
    onEvent: (raw) => {
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
  stopTracking()
  try {
    const data = await api.post<SynthesisSnapshot>('/api/synthesis', synthesisRequestBody(text))
    applySnapshot(data)
    if (data.job_id && data.status !== 'done' && data.status !== 'failed') {
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
  stopTracking()
  try {
    const data = await api.post<SynthesisSnapshot>('/api/synthesis', synthesisRequestBody(item.text))
    applySnapshot(data)
    if (data.job_id && data.status !== 'done' && data.status !== 'failed') {
      startTracking(data.job_id)
    }
  } catch (error) {
    retryingIndex.value = null
    busy.value = false
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

function clearResults() {
  document.querySelectorAll<HTMLAudioElement>('.result-list audio').forEach((audio) => audio.pause())
  outputs.value = []
  job.value = null
  latestRevision = -1
  stopTracking()
}

async function refreshReadiness() {
  try {
    readiness.value = await api.get<SynthesisReadiness>('/api/synthesis/readiness')
    if (readiness.value.status !== 'warming' && readinessPoller) {
      window.clearInterval(readinessPoller)
      readinessPoller = undefined
    }
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
    if (readiness.value.status === 'warming' && !readinessPoller) {
      readinessPoller = window.setInterval(() => void refreshReadiness(), 1200)
    }
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

function initSynthesis() {
  void refreshReadiness()
  if (!readinessPoller) {
    readinessPoller = window.setInterval(() => {
      if (readiness.value.status === 'warming') void refreshReadiness()
    }, 1500)
  }
}

export function useSynthesis() {
  return {
    busy,
    outputs,
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
