import { computed, reactive, ref } from 'vue'
import { api, type JobResult } from '@/lib/api'
import { useToast } from './useToast'

export const TRAINING_STEP_LABELS: Record<string, string> = {
  slice: '自然音频切片',
  asr: '语音识别',
  correct: '校对',
  feat: '特征提取',
  train: '模型训练',
}

const STATUS_LABELS: Record<string, string> = {
  pending: '未开始',
  running: '进行中',
  done: '已完成',
  fail: '失败',
}

const toast = useToast()

const training = ref<any>({ steps: {} })
const stats = ref<any>({})
const running = ref<any[]>([])
const logText = ref('（还没有任务，日志会显示在这里）')
const metrics = ref<any[]>([])
const checkpoints = ref<any[]>([])
const environment = ref<{ name: string; status: string; detail: string; fix: string }[]>([])
const actionBusy = ref(false)
const refreshing = ref(false)
const loaded = ref(false)

const recording = ref<File | null>(null)
const sliceDefaults = { silence_threshold: -35, min_length: 4000, min_gap: 300, keep_silence: 500 }
const slice = reactive({ ...sliceDefaults })
const whisperModel = ref('large-v3')
const transcript = ref<{ path: string; text: string; audio_url?: string }[]>([])
const experiment = ref('myvoice')
const epochs = reactive({ s1_epochs: 15, s2_epochs: 12 })
const activeStep = ref('slice')
const asrModels = [
  { id: 'large-v3', title: 'large-v3', detail: '最准，适合碎片和带噪素材', time: '较慢' },
  { id: 'medium', title: 'medium', detail: '速度和精度折中', time: '中等' },
  { id: 'small', title: 'small', detail: '先跑通流程', time: '较快' },
]

let poller: number | undefined
let autoPicked = false

const step = (name: string) => training.value.steps?.[name] || { status: 'pending', title: name }
const statusText = (status: string) => STATUS_LABELS[status] || status

const workflow = computed(() => [
  {
    key: 'slice',
    title: '自然音频切片',
    action: '上传一段自然说话录音，自动切成句级片段。',
    unlocked: true,
    lockReason: '',
  },
  {
    key: 'asr',
    title: '语音识别',
    action: '把每个切片自动转成文字。',
    unlocked: Number(stats.value.sliced || 0) > 0 || step('asr').status === 'done',
    lockReason: '至少需要 1 个切片，先完成自然音频切片。',
  },
  {
    key: 'correct',
    title: '校对',
    action: '只在发现错字时修正，不要求逐字校对。',
    unlocked: step('asr').status === 'done' || Number(stats.value.lines || 0) > 0,
    lockReason: '先完成语音识别。',
  },
  {
    key: 'feat',
    title: '特征提取',
    action: '确认实验名，生成训练数据集。',
    unlocked: step('correct').status === 'done',
    lockReason: '先保存校对结果。',
  },
  {
    key: 'train',
    title: '模型训练',
    action: '先训练韵律（s1），再训练音色（s2）。',
    unlocked: step('feat').status === 'done',
    lockReason: '训练数据集尚未生成，先完成特征提取。',
  },
])

const recommendedStep = computed(() => {
  const failed = workflow.value.find((item) => step(item.key).status === 'fail')
  if (failed) return failed.key
  const next = workflow.value.find((item) => step(item.key).status !== 'done' && item.unlocked)
  return next?.key || 'train'
})

const activeWorkflowStep = computed(
  () => workflow.value.find((item) => item.key === activeStep.value) || workflow.value[0],
)

const progress = computed(() => Number(step('train').progress?.overall_percent || 0))
const stageRow = computed(() => step('train').progress || {})
const metricRows = computed(() => metrics.value)
const checkpointRows = computed(() => checkpoints.value.slice(-4).reverse())
const latestMetric = computed(() => metricRows.value.at(-1) || {})

const lossSeries = computed(() =>
  metricRows.value
    .map((row: any) => ({
      step: Number(row.step || row.step_idx || 0),
      loss_d: Number(row.loss_d || 0),
      loss_g: Number(row.loss_g || 0),
    }))
    .filter((row) => Number.isFinite(row.step) && (row.loss_d > 0 || row.loss_g > 0)),
)

const lossPath = computed(() => {
  const rows = lossSeries.value
  if (!rows.length) return [] as string[]
  const maxLoss = Math.max(...rows.flatMap((row) => [row.loss_d, row.loss_g]), 0.01)
  const range = Math.max(rows[rows.length - 1].step - rows[0].step, 1)
  return ['loss_d', 'loss_g'].map((metric) => {
    return rows
      .map((row, index) => {
        const x = ((row.step - rows[0].step) / range) * 100
        const y = (1 - (metric === 'loss_d' ? row.loss_d : row.loss_g) / maxLoss) * 100
        return `${index ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`
      })
      .join(' ')
  })
})

const runState = computed(() => {
  const status = step('train').status
  if (status === 'running') return 'running'
  if (status === 'done') return 'done'
  if (status === 'fail') return 'recover'
  if (status === 'pending' && checkpointRows.value.length) return 'recover'
  return 'setup'
})

const transcriptStats = computed(() => {
  const total = transcript.value.length
  const valid = transcript.value.filter((row) => row.text?.trim()).length
  return { total, valid, empty: total - valid }
})

const experimentReady = computed(() => /^[A-Za-z0-9_-]+$/.test(experiment.value.trim()))

async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const data = await api.get<any>('/api/training/status')
    training.value = data.state || { steps: {} }
    stats.value = data.stats || {}
    running.value = data.running || []
    logText.value = data.log?.text || '（还没有任务，日志会显示在这里）'
    environment.value = data.environment || []
    if (running.value.length || data.last_job_id) {
      const jobId = running.value[0]?.job_id || data.last_job_id
      metrics.value = (await api.get<any>(`/api/training/metrics?job_id=${jobId}`)).rows ?? []
      checkpoints.value = (await api.get<any>('/api/training/checkpoints')).rows ?? []
    } else {
      metrics.value = []
      checkpoints.value = []
    }
    if (typeof data.state?.exp === 'string' && data.state.exp) experiment.value = data.state.exp
    loaded.value = true
    if (!autoPicked) {
      activeStep.value = recommendedStep.value
      autoPicked = true
    }
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    refreshing.value = false
  }
}

async function runAction(path: string, payload: object = {}) {
  actionBusy.value = true
  try {
    const result = await api.post<JobResult>(path, payload)
    if (result.message) toast.success(result.message)
    if (result.log?.text) logText.value = result.log.text
    await refresh()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    actionBusy.value = false
  }
}

async function startSlice() {
  if (!recording.value) {
    toast.error('请先上传一段自然说话录音。')
    return
  }
  const body = new FormData()
  body.append('recording', recording.value)
  Object.entries(slice).forEach(([key, value]) => body.append(key, String(value)))
  actionBusy.value = true
  try {
    const result = await api.post<JobResult>('/api/training/slice', body)
    if (result.message) toast.success(result.message)
    if (result.log?.text) logText.value = result.log.text
    await refresh()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    actionBusy.value = false
  }
}

async function loadTranscript() {
  try {
    transcript.value = (await api.get<any>('/api/training/transcript')).rows || []
    toast.success(`已加载 ${transcript.value.length} 句。`)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

async function saveTranscript() {
  await runAction('/api/training/transcript', { rows: transcript.value })
}

async function selectModels() {
  try {
    const data = await api.post<any>('/api/training/models/select')
    toast.success(data.message)
    await refresh()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

function failureHint(key: string) {
  return (
    {
      slice: '检查录音格式与切片参数后重试。',
      asr: '检查识别依赖，或换用更小的模型重试。',
      correct: '重新加载识别结果，确认没有空文本后保存。',
      feat: '确认广普补丁和依赖已处理，然后重试提取。',
      train: '检查特征数据、显存和训练日志，再重新启动。',
    }[key] || '查看实时日志后重试。'
  )
}

function focusStep(key: string) {
  activeStep.value = key
}

function resetSliceDefaults() {
  Object.assign(slice, sliceDefaults)
}

function startPolling() {
  stopPolling()
  poller = window.setInterval(() => void refresh(), 2500)
}

function stopPolling() {
  if (poller !== undefined) {
    window.clearInterval(poller)
    poller = undefined
  }
}

export function useTraining() {
  return {
    training,
    stats,
    running,
    logText,
    metrics,
    checkpoints,
    environment,
    actionBusy,
    refreshing,
    loaded,
    recording,
    slice,
    sliceDefaults,
    whisperModel,
    transcript,
    experiment,
    epochs,
    activeStep,
    asrModels,
    step,
    statusText,
    workflow,
    recommendedStep,
    activeWorkflowStep,
    progress,
    stageRow,
    metricRows,
    checkpointRows,
    latestMetric,
    lossPath,
    lossSeries,
    runState,
    transcriptStats,
    experimentReady,
    refresh,
    runAction,
    startSlice,
    loadTranscript,
    saveTranscript,
    selectModels,
    failureHint,
    focusStep,
    resetSliceDefaults,
    startPolling,
    stopPolling,
  }
}
