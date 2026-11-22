import { computed, reactive, ref } from 'vue'
import {
  api,
  errorMessage,
  type CheckpointRow,
  type EnvironmentRow,
  type JobResult,
  type TrainingJobRow,
  type TrainingMetricRow,
  type TrainingState,
  type TrainingStatusResponse,
  type TranscriptRow,
} from '@/lib/api'
import { createPolling } from './usePolling'
import { useConfig } from './useConfig'
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
const configStore = useConfig()

const training = ref<TrainingState>({ steps: {} })
const stats = ref<Record<string, number | string | null>>({})
const running = ref<TrainingJobRow[]>([])
const logText = ref('（还没有任务，日志会显示在这里）')
const metrics = ref<TrainingMetricRow[]>([])
const checkpoints = ref<CheckpointRow[]>([])
const environment = ref<EnvironmentRow[]>([])
const actionBusy = ref(false)
const refreshing = ref(false)
const loaded = ref(false)

const recording = ref<File | null>(null)
const sliceDefaults = { silence_threshold: -35, min_length: 4000, min_gap: 300, keep_silence: 500 }
const slice = reactive({ ...sliceDefaults })
const whisperModel = ref('large-v3')
const transcript = ref<TranscriptRow[]>([])
const transcriptLoaded = ref(false)
const experiment = ref('myvoice')
const epochs = reactive({ s1_epochs: 15, s2_epochs: 12 })
const activeStep = ref('slice')
const asrModels = [
  { id: 'large-v3', title: 'large-v3', detail: '最准，适合碎片和带噪素材', time: '较慢' },
  { id: 'medium', title: 'medium', detail: '速度和精度折中', time: '中等' },
  { id: 'small', title: 'small', detail: '先跑通流程', time: '较快' },
]

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
    .map((row) => ({
      // 引擎上报的全局步数才是 x 轴（曾误读不存在的 step 字段导致所有点挤在 x=0，图是空的）
      step: Number(row.global_step ?? row.step ?? 0),
      loss_d: Number(row.loss_d ?? 0),
      loss_g: Number(row.loss_g ?? 0),
    }))
    .filter((row) => Number.isFinite(row.step) && (row.loss_d > 0 || row.loss_g > 0)),
)

const lossPath = computed(() => {
  const rows = lossSeries.value
  if (!rows.length) return [] as string[]
  const maxLoss = Math.max(...rows.flatMap((row) => [row.loss_d, row.loss_g]), 0.01)
  const range = Math.max(rows[rows.length - 1].step - rows[0].step, 1)
  // Coordinates are emitted for the consumer's viewBox of 100 x 40, and each
  // string already starts with a moveto — the template used to prepend another
  // "M", which made the path data invalid and left the chart blank.
  const height = 40
  return ['loss_d', 'loss_g'].map((metric) => {
    return rows
      .map((row, index) => {
        const x = ((row.step - rows[0].step) / range) * 100
        const y = (1 - (metric === 'loss_d' ? row.loss_d : row.loss_g) / maxLoss) * height
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
    const data = await api.get<TrainingStatusResponse>('/api/training/status')
    training.value = data.state || { steps: {} }
    stats.value = data.stats || {}
    running.value = data.running || []
    logText.value = data.log?.text || '（还没有任务，日志会显示在这里）'
    environment.value = data.environment || []
    if (running.value.length || data.last_job_id) {
      const jobId = running.value[0]?.job_id || data.last_job_id
      metrics.value = (
        await api.get<{ rows?: TrainingMetricRow[] }>(`/api/training/metrics?job_id=${jobId}`)
      ).rows ?? []
      checkpoints.value =
        (await api.get<{ rows?: CheckpointRow[] }>('/api/training/checkpoints')).rows ?? []
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

/**
 * Run one training action and report whether it actually succeeded.
 *
 * Callers used to `await runAction(...)` and then unconditionally toast success
 * and navigate away, so a failed "select model" still told the user their new
 * voice was ready. The boolean return is the whole point of this signature.
 */
async function runAction(path: string, payload: object = {}): Promise<boolean> {
  actionBusy.value = true
  try {
    const result = await api.post<JobResult & { config?: Record<string, unknown> }>(path, payload)
    if (result.message) toast.success(result.message)
    if (result.log?.text) logText.value = result.log.text
    // 后端在「填入最新模型」里直接改了 config。前端手里的 Config 还是旧的，
    // 而合成请求会把它整份带回去：旧路径若已不存在、或落在允许范围之外，
    // 之后每次生成都会被 config_guard 拒绝（400），直到刷新页面才恢复。
    // 服务器既然把 config 回传了，就以它为准重新读一次。
    if (result.config) await configStore.loadConfig()
    await refresh()
    return true
  } catch (error) {
    toast.error(errorMessage(error))
    return false
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
    transcript.value =
      (await api.get<{ rows?: TranscriptRow[] }>('/api/training/transcript')).rows || []
    transcriptLoaded.value = true
    toast.success(`已加载 ${transcript.value.length} 句。`)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

async function saveTranscript(silent = false) {
  // 后端 /api/training/transcript 是 PUT（POST 会 405），不能走 runAction 的 post。
  if (!transcriptLoaded.value) {
    if (!silent) toast.error('转写还没加载，先点「重新加载」或「跳过校对直接继续」。')
    return
  }
  actionBusy.value = true
  try {
    const result = await api.put<JobResult>('/api/training/transcript', { rows: transcript.value })
    if (!silent && result.message) toast.success(result.message)
    if (result.log?.text) logText.value = result.log.text
    await refresh()
  } catch (error) {
    toast.error(errorMessage(error))
  } finally {
    actionBusy.value = false
  }
}

async function selectModels() {
  try {
    const data = await api.post<{ message: string; config?: Record<string, unknown> }>(
      '/api/training/models/select',
    )
    toast.success(data.message)
    // 同 runAction：后端改了模型路径，前端必须同步，否则下一次合成会用旧路径。
    if (data.config) await configStore.loadConfig()
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

// One shared loop for the whole training page. `createPolling` (rather than
// `usePolling`) because this composable is a module singleton, not a component:
// the timer belongs to the module and is stopped explicitly by the view.
const poller_ = createPolling({
  interval: 2500,
  immediate: false,
  failLimit: 0, // training status is long-lived; never give up on a blip
  run: async () => {
    await refresh()
    return true
  },
})

function startPolling() {
  poller_.start()
}

function stopPolling() {
  poller_.stop()
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
    transcriptLoaded,
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
