import { ref, watch } from 'vue'
import {
  api,
  errorMessage,
  getReferenceAudioPrepareSnapshot,
  startReferenceAudioPrepare,
  type ReferenceAudioPrepareSnapshot,
} from '@/lib/api'
import { referenceQualityFailed, type ReferenceReport } from './referenceAudio'
import { createPolling } from './usePolling'
import { useConfig } from './useConfig'
import { useToast } from './useToast'

type Prompt = { path: string; text: string; source: string; language: string }
type Pair = {
  status: 'matched' | 'mismatched' | 'unknown' | 'empty'
  expected_text: string | null
  actual_text: string
  source: string | null
  message: string
  cer?: number | null
}

const POLL_MS = 700
const TIMEOUT_MS = 10 * 60 * 1000
const POLL_FAILURE_LIMIT = 5

const configStore = useConfig()
const toast = useToast()

const purpose = ref('synthesis')
const uploading = ref(false)
const stage = ref('')
const progress = ref(0)
const uploadName = ref('')
const uploadSize = ref('')
const checking = ref(false)
const report = ref<ReferenceReport | null>(null)
const prompt = ref<Prompt | null>(null)
const pair = ref<Pair | null>(null)
const patchBusy = ref(false)

let token = 0
let lookupTimer: number | undefined
// Current prepare job, consumed by the poller below.
const prepareJobId = ref('')
let prepareStartedAt = 0

function formatSize(size: number) {
  return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`
}

const uploadLabel = ref('')
watch([uploading, stage, progress], () => {
  if (!uploading.value) {
    uploadLabel.value = ''
    return
  }
  const percent = Math.round(Math.max(0, Math.min(1, progress.value)) * 100)
  const base = stage.value || '准备中'
  uploadLabel.value = percent > 0 ? `${base} ${percent}%` : `${base}…`
})

function stopPolling() {
  token += 1
  preparePoll.stop()
}

function finish() {
  uploading.value = false
  stage.value = ''
  progress.value = 0
}

// 参考音准备进度。原先是一套手写的 setTimeout 自链（自己维护 token、失败计数和
// 超时），与另外几处实现逐渐漂移；现在统一用 createPolling——它自带防重叠、
// 失败重试和 generation 失效，超时仍由这里判断（业务含义，不属于轮询器）。
const preparePoll = createPolling({
  interval: POLL_MS,
  failLimit: POLL_FAILURE_LIMIT,
  run: async () => {
    if (!prepareJobId.value) return false
    if (Date.now() - prepareStartedAt > TIMEOUT_MS) {
      finish()
      toast.error('音色样本准备超时，请换一段更短的录音。')
      return false
    }
    const job = await getReferenceAudioPrepareSnapshot(prepareJobId.value)
    progress.value = Math.max(0, Math.min(1, Number(job.progress) || 0))
    stage.value = job.stage || '准备中'
    if (job.status === 'done') {
      finish()
      applyPrepare(job)
      return false
    }
    if (job.status === 'failed') {
      finish()
      toast.error(job.error || job.message || '音色样本准备失败。')
      return false
    }
    return true
  },
  onGiveUp: (error) => {
    finish()
    toast.error(errorMessage(error) || '读取准备进度失败，请重试。')
  },
})

function applyPrepare(job: ReferenceAudioPrepareSnapshot) {
  const result = job.result
  if (!result?.ref_audio) {
    toast.error('音色样本准备完成，但没有返回参考音频。')
    return
  }
  configStore.config.ref_audio = result.ref_audio
  if (result.ref_text) configStore.config.ref_text = result.ref_text
  report.value = result.quality || null
  prompt.value = null
  pair.value = null
  if (referenceQualityFailed(result.quality)) {
    // 设备质量参差是常态：fail 只降级提示，配置照常写入，用户随时可以直接生成。
    toast.info('音色样本已填入，可以直接生成；这段录音有明显质量问题（见报告），介意可重录。')
  } else {
    const seconds = (result.selected?.duration || result.duration_seconds || 0).toFixed(1)
    toast.success(`已自动选取 ${seconds} 秒语音并识别原文，可以直接生成。`)
  }
}

async function upload(file: File) {
  if (!file) return
  uploadName.value = file.name
  uploadSize.value = formatSize(file.size)
  const body = new FormData()
  body.append('audio', file)
  body.append('purpose', purpose.value)
  body.append('auto_prepare', 'true')
  body.append('language', configStore.config.prompt_lang)

  stopPolling()
  const currentToken = token
  const startedAt = Date.now()
  progress.value = 0
  stage.value = '上传中'
  uploading.value = true

  try {
    if (purpose.value !== 'synthesis') {
      const result = await api.post<{ path: string; ref_text?: string; report: ReferenceReport }>('/api/reference-audio', body)
      if (currentToken !== token) return
      configStore.config.ref_audio = result.path
      if (result.ref_text) configStore.config.ref_text = result.ref_text
      report.value = result.report
      prompt.value = null
      pair.value = null
      finish()
      if (referenceQualityFailed(result.report)) {
        toast.info('音色样本已填入，可以直接使用；录音有明显质量问题，介意可重录。')
      } else {
        toast.success('音色样本已上传。')
      }
      return
    }
    const started = await startReferenceAudioPrepare(body)
    if (currentToken !== token) return
    stage.value = '排队中'
    prepareJobId.value = started.job_id
    prepareStartedAt = startedAt
    preparePoll.start()
  } catch (error) {
    if (currentToken !== token) return
    stopPolling()
    finish()
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

async function inspect() {
  const target = String(configStore.config.ref_audio || '').trim()
  if (!target) {
    toast.error('请先填写或上传参考音频。')
    return
  }
  checking.value = true
  report.value = null
  try {
    const body: Record<string, string> = {
      purpose: purpose.value,
      ref_text: configStore.config.ref_text || '',
    }
    if (/[\\/]$/.test(target)) body.directory = target
    else body.path = target
    const result = await api.post<{ report: ReferenceReport; pair?: Pair | null }>('/api/reference-audio/inspect', body)
    report.value = result.report
    pair.value = result.pair || null
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    checking.value = false
  }
}

async function importPatch(file: File) {
  const body = new FormData()
  body.append('patch', file)
  patchBusy.value = true
  try {
    const result = await api.post<{ message: string }>('/api/engine/pronunciation-patch', body)
    toast.success(result.message)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    patchBusy.value = false
  }
}

async function lookupPrompt() {
  const target = String(configStore.config.ref_audio || '').trim()
  if (!target || /[\\/]$/.test(target)) {
    prompt.value = null
    pair.value = null
    return
  }
  try {
    const params = new URLSearchParams({ path: target, ref_text: configStore.config.ref_text || '' })
    const result = await api.get<{ prompt: Prompt | null; pair: Pair | null }>(
      `/api/reference-audio/prompt?${params.toString()}`,
    )
    prompt.value = result.prompt
    pair.value = result.pair?.status === 'unknown' ? null : result.pair
    if (result.prompt?.text && !configStore.config.ref_text.trim()) {
      configStore.config.ref_text = result.prompt.text
      pair.value = result.pair
    }
  } catch {
    prompt.value = null
    pair.value = null
  }
}

watch(
  () => configStore.config.ref_audio,
  () => {
    if (lookupTimer !== undefined) window.clearTimeout(lookupTimer)
    lookupTimer = window.setTimeout(() => void lookupPrompt(), 350)
  },
)

export function useReferenceAudio() {
  return {
    purpose,
    uploading,
    stage,
    progress,
    uploadName,
    uploadSize,
    uploadLabel,
    checking,
    report,
    prompt,
    pair,
    patchBusy,
    upload,
    inspect,
    importPatch,
  }
}
