<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, type Config, type DoctorReport, type JobResult, type SynthesisLanguage, type SynthesisOutput, type SynthesisReadiness, type SynthesisSnapshot, type SynthesisStreamEvent } from './api'
import { createSynthesisStream, shouldApplySynthesisRevision, type SynthesisStreamController } from './synthesisStream'
import LanguageMarkerInput from './components/LanguageMarkerInput.vue'
import { FileUp, HelpCircle } from '@lucide/vue'
const AudioOrb = defineAsyncComponent(() => import('./components/AudioOrb.vue'))
import IconRail from './components/IconRail.vue'
import OrbSwallow from './components/OrbSwallow.vue'
import TimbrePanel from './components/TimbrePanel.vue'
import { createDraftsStore, type Draft } from './composables/drafts'

type Page = 'synthesis' | 'training' | 'history' | 'settings' | 'timbre'
type EnvRow = { name: string; status: string; detail: string; fix: string }
type Transcript = { path: string; text: string; audio_url?: string }
type DebugLog = { time: string; scope: string; message: string }
type OrbSemanticToken = { outputIndex: number; tokenIndex: number; semanticFrameRate: number }
type OrbPlayback = { outputIndex: number; currentTime: number; duration: number; tokenCount: number; frameRate: number; speed: number; paused: boolean }

const languageOptions: { value: SynthesisLanguage; label: string }[] = [
  { value: 'zh', label: '中文' },
  { value: 'yue', label: '粤语' },
  { value: 'en', label: '英语' },
  { value: 'ja', label: '日语' },
  { value: 'ko', label: '韩语' },
]

type Theme = 'workbench' | 'focus'
type ColorMode = 'light' | 'dark'
const THEME_ALIASES: Record<string, Theme> = { dark: 'workbench', 'light-blue': 'workbench', 'light-green': 'workbench', auto: 'workbench' }
function normalizeTheme(value: unknown): Theme {
  if (value === 'workbench' || value === 'focus') return value
  return THEME_ALIASES[String(value ?? '')] ?? 'workbench'
}

const page = ref<Page>('synthesis')
const colorMode = ref<ColorMode>('light')
const config = reactive<Config>({ gpt_sovits_root: '', gpt_model: '', sovits_model: '', ref_audio: '', ref_text: '', text_lang: 'zh', prompt_lang: 'zh', top_k: 30, top_p: .88, temperature: .95, sample_steps: 64, speed: 1, pause_second: .15, mixed_eos_floor: null, mixed_token_cap: null, auto_lightmid: true, theme: 'workbench', orb_synthesis_token_color: '#75BBFF', orb_settled_color: '#69DDA4', orb_playback_token_color: '#F8FBFF' })
const environment = ref<EnvRow[]>([])
const candidates = ref<{ label: string; path: string }[]>([])
const history = ref<any[]>([])
const training = ref<any>({ steps: {} })
const trainMetrics = ref<any[]>([])
const trainCheckpoints = ref<any[]>([])
const stats = ref<any>({})
const running = ref<any[]>([])
const logText = ref('（还没有任务，日志将显示在这里）')
const notice = ref('')
const busy = ref(false)
const synthesisText = ref('')
const synthOutputs = ref<SynthesisOutput[]>([])
const synthesisJob = ref<SynthesisSnapshot | null>(null)
const retryingOutputIndex = ref<number | null>(null)
const synthesisDebugLogs = ref<DebugLog[]>([])
const doctorReport = ref<DoctorReport | null>(null)
const doctorBusy = ref(false)
const synthesisReadiness = ref<SynthesisReadiness>({ status: 'warming', message: '模型预热中。', error: '' })
const synthesisWarmupBusy = ref(false)
const orbSemanticToken = ref<OrbSemanticToken | null>(null)
const completedOrbOutputs = ref<number[]>([])
const failedOrbOutputs = ref<number[]>([])
const orbPlayback = ref<OrbPlayback | null>(null)
const batchFileName = ref('')
const batchLineCount = ref(0)
const referenceAudioUploading = ref(false)
const pronunciationPatchBusy = ref(false)
const recording = ref<File | null>(null)
const slice = reactive({ silence_threshold: -35, min_length: 4000, min_gap: 300, keep_silence: 500 })
const sliceDefaults = reactive({ silence_threshold: -35, min_length: 4000, min_gap: 300, keep_silence: 500 })
function resetSliceDefaults() {
  Object.assign(slice, sliceDefaults)
}
const whisperModel = ref('large-v3')
const transcript = ref<Transcript[]>([])
const experiment = ref('myvoice')
const epochs = reactive({ s1_epochs: 15, s2_epochs: 12 })
const SYNTHESIS_ONBOARDING_STORAGE_KEY = 'xianghua.synthesis-onboarded'
const onboardingAudioName = ref('')
const onboardingAudioSize = ref('')
const onboardingSaving = ref(false)
const synthesisOnboardingOpen = ref(false)
const synthesisOnboardingStep = ref(1)
const synthesisOnboarded = ref(readSynthesisOnboardingState())
const synthesisOnboardingCloseRef = ref<HTMLButtonElement | null>(null)
let polling: number | undefined
let synthesisPoller: number | undefined
let synthesisStream: SynthesisStreamController | null = null
let latestSynthesisRevision = -1
let synthesisReadinessPoller: number | undefined
const SYNTHESIS_POLL_FAILURE_LIMIT = 5
let synthesisPollFailures = 0

const trainStep = (name: string) => training.value.steps?.[name] || { status: 'pending', title: name }
const trainingStepLabels: Record<string, string> = { slice: '录音切片', asr: 'Whisper 识别', correct: '人工校对', feat: '特征提取', train: '模型训练' }
const trainingStatusLabels: Record<string, string> = { pending: '未开始', running: '进行中', done: '已完成', fail: '失败' }
const stepLabel = (key: string) => trainingStepLabels[key] || key
const statusText = (status: string) => trainingStatusLabels[status] || status
const trainingProgress = computed(() => Number(trainStep('train').progress?.overall_percent || 0))
const trainingMetricRows = computed(() => trainMetrics.value)
const trainingLatestMetric = computed(() => trainingMetricRows.value.at(-1) || {})
const trainingMetricCards = computed(() => [
  { label: 'loss_d', value: trainingLatestMetric.value.loss_d },
  { label: 'loss_g', value: trainingLatestMetric.value.loss_g },
  { label: 'lr', value: trainingLatestMetric.value.learning_rate },
])
const trainingStageRow = computed(() => trainStep('train').progress || {})
const trainingCheckpointRows = computed(() => trainCheckpoints.value.slice(-4).reverse())
const trainingLossSeries = computed(() => trainingMetricRows.value
  .map((row: any) => ({ step: Number(row.step || row.step_idx || 0), loss_d: Number(row.loss_d || 0), loss_g: Number(row.loss_g || 0) }))
  .filter((row: any) => Number.isFinite(row.step) && Number.isFinite(row.loss_d) && Number.isFinite(row.loss_g) && (row.loss_d > 0 || row.loss_g > 0)))
const trainingLossPoints = computed(() => {
  const rows = trainingLossSeries.value
  if (!rows.length) return []
  const maxLoss = Math.max(...rows.flatMap((row: any) => [row.loss_d, row.loss_g]), 0.01)
  const stepRange = Math.max(rows[rows.length - 1].step - rows[0].step, 1)
  return rows.map((row: any, index: number) => ({
    x: (row.step - rows[0].step) / stepRange,
    y: row.loss_d / maxLoss,
    metric: 'loss_d',
    title: `step ${row.step} · loss_d ${row.loss_d.toFixed(3)}`,
  })).concat(rows.map((row: any) => ({
    x: (row.step - rows[0].step) / stepRange,
    y: row.loss_g / maxLoss,
    metric: 'loss_g',
    title: `step ${row.step} · loss_g ${row.loss_g.toFixed(3)}`,
  })))
})
const trainingLossPath = computed(() => {
  const metricRows = ['loss_d', 'loss_g'].map((metric) => {
    const points = trainingLossPoints.value.filter((point: any) => point.metric === metric)
    if (!points.length) return ''
    const path = points.map((point: any, index: number) => `${index ? 'L' : 'M'}${Number((point.x * 100).toFixed(2))} ${Number(((1 - point.y) * 100).toFixed(2))}`)
    return path.join(' ')
  })
  return metricRows
})
const trainingSampleInterval = ref(1)
const asrModelCards = [
  { id: 'large-v3', title: 'large-v3', detail: '最准，碎片/带噪', time: '较慢' },
  { id: 'medium', title: 'medium', detail: '速度和精度折中', time: '中等' },
  { id: 'small', title: 'small', detail: '先看流程，速度快', time: '较快' },
]
const trainingRunState = computed(() => {
  const status = trainStep('train').status
  if (status === 'running') return 'running'
  if (status === 'done') return 'done'
  if (status === 'fail') return 'recover'
  if (status === 'pending' && trainingCheckpointRows.value.length) return 'recover'
  return 'setup'
})
const trainingWorkflow = computed(() => [
  {
    key: 'slice',
    title: '录音切片',
    action: '选择清晰录音并提交切片',
    output: '切片音频',
    unlocked: true,
    lockReason: '',
  },
  {
    key: 'asr',
    title: 'Whisper 识别',
    action: '自动把切片转成文字',
    output: '识别文本清单',
    unlocked: Number(stats.value.sliced || 0) > 0 || trainStep('asr').status === 'done',
    lockReason: '至少需要 1 个切片，先完成第 1 步录音切片。',
  },
  {
    key: 'correct',
    title: '人工校对',
    action: '边听切片，修正自动识别的文字',
    output: '校对清单',
    unlocked: trainStep('asr').status === 'done' || Number(stats.value.lines || 0) > 0,
    lockReason: '先完成第 2 步 Whisper 识别。',
  },
  {
    key: 'feat',
    title: '特征提取',
    action: '确认实验名并开始提取',
    output: '训练数据集',
    unlocked: trainStep('correct').status === 'done',
    lockReason: '需要先应用广普发音补丁并完成第 3 步人工校对。',
  },
  {
    key: 'train',
    title: '模型训练',
    action: '设置轮数并开始训练',
    output: 'GPT / SoVITS 模型',
    unlocked: trainStep('feat').status === 'done',
    lockReason: '训练数据集尚未生成，先完成第 4 步特征提取。',
  },
])
const recommendedTrainingStep = computed(() => {
  const failed = trainingWorkflow.value.find((step) => trainStep(step.key).status === 'fail')
  if (failed) return failed.key
  const firstOpen = trainingWorkflow.value.find((step) => trainStep(step.key).status !== 'done' && step.unlocked)
  return firstOpen?.key || 'train'
})
const activeTrainingStep = computed(() => trainingWorkflow.value.find((step) => step.key === expandedStep.value) || trainingWorkflow.value[0])
const transcriptStats = computed(() => {
  const valid = transcript.value.filter((row) => (row.text || '').trim()).length
  return { total: transcript.value.length, valid, empty: transcript.value.length - valid }
})
const experimentReady = computed(() => /^[A-Za-z0-9_-]+$/.test(experiment.value.trim()))
const orbColors = computed(() => ({
  synthesisToken: config.orb_synthesis_token_color,
  settled: config.orb_settled_color,
  playbackToken: config.orb_playback_token_color,
}))
const orbColorState = computed(() => [config.orb_synthesis_token_color, config.orb_settled_color, config.orb_playback_token_color].join('|'))
const synthesisProgress = computed(() => {
  const total = Number(synthesisJob.value?.total || 0)
  const completed = Number(synthesisJob.value?.completed || 0)
  const streamProgress = synthesisJob.value?.progress
  if (typeof streamProgress === 'number' && Number.isFinite(streamProgress)) {
    return Math.round(Math.min(100, Math.max(0, streamProgress * 100)))
  }
  return total ? Math.round(Math.min(100, Math.max(0, (completed / total) * 100))) : 0
})
const synthesisReady = computed(() => synthesisReadiness.value.status === 'ready')

const statusLabel = computed(() => {
  if (busy.value) return '处理中'
  if (running.value.length) return '任务运行中'
  if (synthesisReadiness.value.status === 'warming') return '模型预热中'
  if (synthesisReadiness.value.status === 'failed') return '模型未就绪'
  if (synthesisReadiness.value.status === 'ready') return '就绪'
  return '系统待机'
})

const statusClass = computed(() => {
  if (busy.value || running.value.length) return 'active'
  if (synthesisReadiness.value.status === 'ready') return 'ready'
  return ''
})

const draftsStore = createDraftsStore()
const drafts = ref<Draft[]>(draftsStore.load())
function addDraft(text: string) { drafts.value = draftsStore.add(text) }
function removeDraft(id: string) { drafts.value = draftsStore.remove(id) }
function clearDrafts() { draftsStore.clear(); drafts.value = [] }

const swallowPlay = ref(0)
const swallowText = ref('')
const synthSummaryDismissed = ref(false)
const resultsOpen = ref(false)
const resultsOverlayRef = ref<HTMLDivElement | null>(null)
const resultsCloseButtonRef = ref<HTMLButtonElement | null>(null)
const referencePanelOpen = ref(false)
const referenceDrawerRef = ref<HTMLElement | null>(null)
const referenceDrawerCloseRef = ref<HTMLButtonElement | null>(null)
const voiceSampleHelpOpen = ref(false)
const orbColorPanelOpen = ref(false)
const voiceStageOrbRef = ref<HTMLDivElement | null>(null)
const orbColorPanelRef = ref<HTMLElement | null>(null)
const orbColorCloseRef = ref<HTMLButtonElement | null>(null)
const synthesisParamsOpen = ref(false)
const synthesisParamsDrawerRef = ref<HTMLElement | null>(null)
const synthesisParamsCloseRef = ref<HTMLButtonElement | null>(null)
let resultsTriggerElement: HTMLElement | null = null
let referenceTriggerElement: HTMLElement | null = null
let synthesisParamsTriggerElement: HTMLElement | null = null

const stageVoiceName = computed(() => {
  const raw = config.ref_audio || config.gpt_model
  return raw ? raw.split(/[\\/]/).pop() || raw : '未配置音色'
})
const hasSynthResults = computed(() => synthOutputs.value.length > 0)
const voiceConfigured = computed(() => config.ref_audio.trim().length > 0)
const showFocusEditorGuide = computed(() => config.theme === 'focus' && synthStage.value === 'idle' && !synthesisText.value.trim())
const synthesisOnboardingTitle = computed(() => ['', '选择样本', '填写原文', '生成试听'][synthesisOnboardingStep.value] || '')
const synthesisOnboardingDescription = computed(() => ({
  1: '选一段清晰人声，推荐 10–30 秒。它决定最终音色。',
  2: '逐字填写样本里实际说出的内容，这是模型理解样本的关键。',
  3: '选一句示例台词，在主工作台快速生成第一条试听。',
})[synthesisOnboardingStep.value] || '')
const synthesisOnboardingPrimaryLabel = computed(() => ({
  1: referenceAudioUploading.value ? '上传中…' : '进入填写原文',
  2: onboardingSaving.value ? '保存中…' : '进入生成试听',
  3: busy.value ? '生成中…' : '开始生成试听',
})[synthesisOnboardingStep.value] || '下一步')
const synthesisOnboardingPrimaryDisabled = computed(() => {
  if (synthesisOnboardingStep.value === 1) return busy.value || referenceAudioUploading.value || !voiceConfigured.value
  if (synthesisOnboardingStep.value === 2) return busy.value || onboardingSaving.value || !config.ref_text.trim()
  return busy.value || !synthesisReady.value || !synthesisText.value.trim()
})
const synthesisOnboardingAlert = computed(() => {
  if (synthesisOnboardingStep.value !== 3 || busy.value || synthesisReady.value) return null
  return synthesisReadiness.value.status === 'failed'
    ? synthesisReadiness.value.message
    : '模型预热中，可先完成样本和原文，稍后再生成。'
})
const synthesisVoiceHintVisible = computed(() => synthesisOnboarded.value && !voiceConfigured.value)
const actionReason = computed(() => {
  if (busy.value) return { text: '正在生成 · 台词已锁定', tone: 'running' }
  if (!voiceConfigured.value) return { text: '音色未配置 · 请上传音色样本', tone: 'warn' }
  if (synthesisReadiness.value.status === 'warming') return { text: '模型预热中 · 稍后可生成', tone: 'warn' }
  if (synthesisReadiness.value.status === 'failed') return { text: '模型未就绪 · 请重新预热', tone: 'danger' }
  if (!synthesisText.value.trim()) return { text: '请输入台词', tone: 'muted' }
  return { text: '中文 · 自动标记', tone: 'ready' }
})
const stageLine1 = computed(() => {
  const job = synthesisJob.value
  if (busy.value && job) return `声场 · 正在生成 ${job.completed}/${job.total}`
  if (synthOutputs.value.length) return `声场 · 合成完成 ${synthOutputs.value.length}`
  return '声场 · 待命'
})
const stageLine2 = computed(() => {
  const job = synthesisJob.value
  if (busy.value && job) {
    const next = Math.min(job.completed + 1, job.total)
    return `${synthesisProgress.value}% · ${job.phase || `第 ${next} 段 token 驱动粒子`}`
  }
  if (synthOutputs.value.length) {
    const failed = failedOutputCount.value
    return failed ? `${synthOutputs.value.length - failed} 条成功 · ${failed} 条失败` : `${synthOutputs.value.length} 条成功 · 可试听`
  }
  return voiceConfigured.value ? `${stageVoiceName.value} · 正在连接本地引擎` : '未配置音色 · 请在右上角设置音色样本'
})
const queueBadge = computed(() => {
  const job = synthesisJob.value
  if (!job || job.status !== 'running') return null
  return `${job.completed}/${job.total}`
})

type SynthStage = 'idle' | 'running' | 'done'
const synthStage = computed<SynthStage>(() => {
  if (busy.value || synthesisJob.value?.status === 'running') return 'running'
  if (synthOutputs.value.length) return 'done'
  return 'idle'
})
const failedOutputCount = computed(() => synthOutputs.value.filter((item) => Boolean(item.error)).length)
const themeLabel = computed(() => (config.theme === 'workbench' ? '工作台' : '活页'))
const SAMPLE_LINES: { label: string; text: string }[] = [
  { label: '中文开场', text: '你好，我是你的专属配音。今天天气很好，我们一起来试试吧。' },
  { label: '中英混说', text: '欢迎使用 <en>XiangHua</en>，接下来测试一下中英混说的效果。' },
  { label: '粤语问候', text: '<yue>今日天气唔错，一齐试下啦。</yue>' },
]
function readSynthesisOnboardingState() {
  try {
    return window.localStorage.getItem(SYNTHESIS_ONBOARDING_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}
function writeSynthesisOnboardingState() {
  synthesisOnboarded.value = true
  try {
    window.localStorage.setItem(SYNTHESIS_ONBOARDING_STORAGE_KEY, '1')
  } catch (error) {
    console.debug('[onboarding]', error instanceof Error ? error.message : String(error))
  }
}
function formatAudioSize(size: number) {
  return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`
}
function focusSynthesisOnboardingStep(step: number) {
  if (step >= 2 && !voiceConfigured.value) return
  if (step >= 3 && !config.ref_text.trim()) return
  synthesisOnboardingStep.value = step
}
function openSynthesisOnboarding() {
  if (voiceConfigured.value) {
    synthesisOnboardingStep.value = config.ref_text.trim() ? 3 : 2
  } else {
    synthesisOnboardingStep.value = 1
  }
  synthesisOnboardingOpen.value = true
  void nextTick(() => { synthesisOnboardingCloseRef.value?.focus() })
}
function completeSynthesisOnboarding(message: string) {
  writeSynthesisOnboardingState()
  synthesisOnboardingOpen.value = false
  notice.value = message
}
function skipSynthesisOnboarding() {
  completeSynthesisOnboarding('已跳过首次引导。生成前仍需配置音色，可从顶部「引导」重新打开。')
}
async function continueOnboardingFromSample() {
  if (synthesisOnboardingPrimaryDisabled.value) return
  onboardingSaving.value = true
  try {
    await saveConfig()
    synthesisOnboardingStep.value = 3
  } finally {
    onboardingSaving.value = false
  }
}
function startOnboardingTrial() {
  if (synthesisOnboardingPrimaryDisabled.value) return
  completeSynthesisOnboarding('已进入主工作台，试听生成会显示在结果里。')
  synthesize()
}
function dismissSynthSummary() {
  synthSummaryDismissed.value = true
}
function outputTaskState(item: SynthesisOutput) {
  if (item.error) return 'failed'
  if (item.url) return 'done'
  return item.text === synthesisJob.value?.current_text ? 'active' : 'pending'
}
function activateSynthesisStage() {
  toggleOrbColorPanel()
}

function openOrbColorPanel() {
  referencePanelOpen.value = false
  voiceSampleHelpOpen.value = false
  referenceTriggerElement = null
  synthesisParamsOpen.value = false
  synthesisParamsTriggerElement = null
  orbColorPanelOpen.value = true
  void nextTick(() => { orbColorCloseRef.value?.focus() })
}

function closeOrbColorPanel() {
  orbColorPanelOpen.value = false
  voiceStageOrbRef.value?.focus()
}

function toggleOrbColorPanel() {
  if (orbColorPanelOpen.value) closeOrbColorPanel()
  else openOrbColorPanel()
}

function onOrbColorPanelKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  event.preventDefault()
  closeOrbColorPanel()
}
function draftPreview(text: string) {
  const firstLine = text.split(/\r?\n/).find((line) => line.trim()) || text
  return firstLine.length > 14 ? `${firstLine.slice(0, 14)}…` : firstLine
}

function assignConfig(value: Partial<Config>) {
  Object.assign(config, value)
  config.text_lang = 'zh'
  config.theme = normalizeTheme(config.theme)
}

let orbColorPersistTimer: number | null = null
let orbColorHydrating = false
function orbColorConfig(): Partial<Config> {
  return {
    orb_synthesis_token_color: config.orb_synthesis_token_color,
    orb_settled_color: config.orb_settled_color,
    orb_playback_token_color: config.orb_playback_token_color,
  }
}
async function refreshConfig() {
  try {
    const saved = await api.get<Config>('/api/config')
    orbColorHydrating = true
    assignConfig(saved)
    await nextTick()
  } catch (error) {
    console.debug('[config]', error instanceof Error ? error.message : String(error))
  } finally {
    orbColorHydrating = false
  }
}
async function persistOrbColorsNow() {
  if (orbColorPersistTimer !== null) {
    window.clearTimeout(orbColorPersistTimer)
    orbColorPersistTimer = null
  }
  try {
    await api.put<Config>('/api/config', orbColorConfig())
  } catch (error) {
    showError(error)
  }
}
function scheduleOrbColorPersistence() {
  if (orbColorPersistTimer !== null) window.clearTimeout(orbColorPersistTimer)
  orbColorPersistTimer = window.setTimeout(() => { orbColorPersistTimer = null; void persistOrbColorsNow() }, 300)
}
watch(orbColorState, () => {
  if (!orbColorHydrating) scheduleOrbColorPersistence()
})

function showError(error: unknown) { notice.value = error instanceof Error ? error.message : String(error) }

function readOptionalNumber(event: Event): number | null {
  const value = (event.target as HTMLInputElement).value.trim()
  if (!value) return null
  const number = Number(value)
  return Number.isFinite(number) && number >= 0 ? number : null
}

function readBoundedNumber(event: Event, fallback: number, min: number, max: number, integer = false): number {
  const raw = Number((event.target as HTMLInputElement).value.trim())
  const bounded = Math.min(max, Math.max(min, Number.isFinite(raw) ? raw : fallback))
  return integer ? Math.round(bounded) : bounded
}

function showResult(result: JobResult) { notice.value = result.message; if (result.log) logText.value = result.log.text; refreshTraining() }

function pushSynthesisDebug(scope: string, message: string) {
  const entry = { time: new Date().toLocaleTimeString('zh-CN', { hour12: false }), scope, message }
  synthesisDebugLogs.value = [...synthesisDebugLogs.value, entry].slice(-12)
  console.debug(`[synthesis:${scope}]`, message)
}
function clearSynthesisDebugLogs() {
  synthesisDebugLogs.value = []
}

function applyDoctorReport(report: DoctorReport) {
  doctorReport.value = report
  assignConfig(report.config)
  if (report.rows?.length) {
    environment.value = report.rows
  }
  notice.value = `${report.repair ? 'Doctor' : '自检'}：${report.summary}`
}

function snapshotLabel(snapshot: SynthesisSnapshot) {
  const progress = typeof snapshot.progress === 'number' ? `${Math.round(snapshot.progress * 100)}%` : ''
  return [snapshot.status, `${snapshot.completed}/${snapshot.total}`, progress, snapshot.phase, snapshot.current_text].filter(Boolean).join(' · ')
}

function stopSynthesisTracking() {
  const hadPoller = Boolean(synthesisPoller)
  const hadStream = Boolean(synthesisStream)
  if (synthesisPoller) {
    window.clearInterval(synthesisPoller)
    synthesisPoller = undefined
  }
  if (synthesisStream) {
    synthesisStream.close()
    synthesisStream = null
  }
  if (hadPoller) pushSynthesisDebug('poll', '已停止状态轮询')
  if (hadStream) pushSynthesisDebug('sse', '已关闭事件流')
}

function normalizeSynthesisSnapshot(data: Partial<SynthesisSnapshot> & { outputs?: any[] } | null | undefined): SynthesisSnapshot {
  const outputs = (data?.outputs || []).map((item, index) => ({
    text: String(item?.text || ''),
    rendered_text: String(item?.rendered_text || ''),
    url: String(item?.url || ''),
    error: String(item?.error || ''),
    output_index: Number.isFinite(Number(item?.output_index)) ? Number(item.output_index) : index,
    semantic_token_count: Math.max(0, Number(item?.semantic_token_count) || 0),
    semantic_frame_rate: Math.max(0, Number(item?.semantic_frame_rate) || 0),
  }))
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
    outputs,
    error: String(data?.error || ''),
    message: data?.message,
  }
}

function addOrbOutput(target: typeof completedOrbOutputs, outputIndex: number) {
  if (outputIndex < 0 || target.value.includes(outputIndex)) return
  target.value = [...target.value, outputIndex]
}

function syncOrbOutputs(outputs: SynthesisOutput[]) {
  completedOrbOutputs.value = outputs.filter((output) => Boolean(output.url) && !output.error).map((output) => output.output_index)
  failedOrbOutputs.value = outputs.filter((output) => Boolean(output.error)).map((output) => output.output_index)
}

function resetOrbState() {
  latestSynthesisRevision = -1
  orbSemanticToken.value = null
  completedOrbOutputs.value = []
  failedOrbOutputs.value = []
  orbPlayback.value = null
}

function applySynthesisSnapshot(data: Partial<SynthesisSnapshot> & { outputs?: any[] }) {
  const snapshot = normalizeSynthesisSnapshot(data)
  synthesisJob.value = snapshot
  syncOrbOutputs(snapshot.outputs)
  const retryIndex = retryingOutputIndex.value
  if (retryIndex === null) {
    synthOutputs.value = snapshot.outputs
  } else if (snapshot.status === 'done' || snapshot.status === 'failed') {
    const replacement = snapshot.outputs[0]
    if (replacement) {
      synthOutputs.value = synthOutputs.value.map((item, index) => index === retryIndex ? replacement : item)
    }
    retryingOutputIndex.value = null
  }
  pushSynthesisDebug('snapshot', snapshotLabel(snapshot))
  if (snapshot.status === 'done' || snapshot.status === 'failed') {
    busy.value = false
    stopSynthesisTracking()
    notice.value = snapshot.status === 'done'
      ? snapshot.message || `完成 ${snapshot.completed}/${snapshot.total}，输出目录：${snapshot.output_dir}`
      : snapshot.message || snapshot.error || '合成失败。'
    void refreshOverview()
    void refreshHistory()
  } else if (snapshot.current_text) {
    notice.value = `正在处理第 ${snapshot.current}/${snapshot.total} 句：${snapshot.current_text}`
  } else if (snapshot.message) {
    notice.value = snapshot.message
  }
}

function applySynthesisStreamEvent(event: SynthesisStreamEvent) {
  if (event.type === 'snapshot') {
    applySynthesisSnapshot(event.snapshot)
    return
  }
  if (event.type === 'semantic_token') {
    orbSemanticToken.value = {
      outputIndex: event.output_index,
      tokenIndex: event.token_index,
      semanticFrameRate: event.semantic_frame_rate,
    }
    return
  }
  if (event.type === 'output_completed') {
    addOrbOutput(completedOrbOutputs, event.output_index)
    failedOrbOutputs.value = failedOrbOutputs.value.filter((index) => index !== event.output_index)
    return
  }
  if (event.type === 'output_failed') {
    addOrbOutput(failedOrbOutputs, event.output_index)
    completedOrbOutputs.value = completedOrbOutputs.value.filter((index) => index !== event.output_index)
  }
}

async function refreshSynthesisStatus(jobId: string) {
  try {
    pushSynthesisDebug('poll', `请求 ${jobId}`)
    const data = await api.get<SynthesisSnapshot>(`/api/synthesis/status/${jobId}`)
    synthesisPollFailures = 0
    if (!shouldApplySynthesisRevision(data.revision, latestSynthesisRevision)) return
    if (Number.isFinite(Number(data.revision))) latestSynthesisRevision = Number(data.revision)
    applySynthesisSnapshot(data)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    pushSynthesisDebug('poll', `${jobId} 获取状态失败：${message}`)
    synthesisPollFailures += 1
    const missingJob = message.includes('合成任务不存在')
    if (!missingJob && synthesisPollFailures < SYNTHESIS_POLL_FAILURE_LIMIT) return
    const reason = missingJob
      ? `合成任务不存在（${jobId}），已停止状态跟踪。`
      : `连续 ${synthesisPollFailures} 次获取合成状态失败，已停止状态跟踪。`
    stopSynthesisTracking()
    busy.value = false
    synthesisJob.value = null
    notice.value = reason
    pushSynthesisDebug('poll', reason)
  }
}

async function refreshSynthesisReadiness() {
  try {
    synthesisReadiness.value = await api.get<SynthesisReadiness>('/api/synthesis/readiness')
    if (synthesisReadiness.value.status !== 'warming' && synthesisReadinessPoller) {
      window.clearInterval(synthesisReadinessPoller)
      synthesisReadinessPoller = undefined
    }
  } catch (error) {
    showError(error)
  }
}

async function retrySynthesisWarmup() {
  if (synthesisWarmupBusy.value) return
  synthesisWarmupBusy.value = true
  try {
    synthesisReadiness.value = await api.post<SynthesisReadiness>('/api/synthesis/readiness/retry')
    if (synthesisReadiness.value.status === 'warming' && !synthesisReadinessPoller) {
      synthesisReadinessPoller = window.setInterval(() => { void refreshSynthesisReadiness() }, 1000)
    }
  } catch (error) {
    showError(error)
  } finally {
    synthesisWarmupBusy.value = false
  }
}

function connectSynthesisStream(jobId: string) {
  if (!window.EventSource) {
    pushSynthesisDebug('sse', '当前浏览器不支持 EventSource')
    return
  }
  pushSynthesisDebug('sse', `连接 ${jobId}`)
  synthesisStream = createSynthesisStream({
    jobId,
    createEventSource: url => new EventSource(url),
    onOpen: () => pushSynthesisDebug('sse', `已连接 ${jobId}`),
    onEvent: raw => {
      try {
        pushSynthesisDebug('sse', `收到事件 ${jobId}`)
        const streamEvent = JSON.parse(raw) as SynthesisStreamEvent & { snapshot?: SynthesisSnapshot }
        const revision = streamEvent.revision ?? streamEvent.snapshot?.revision
        if (!shouldApplySynthesisRevision(revision, latestSynthesisRevision)) return
        if (Number.isFinite(Number(revision))) latestSynthesisRevision = Number(revision)
        applySynthesisStreamEvent(streamEvent)
      } catch {
        pushSynthesisDebug('sse', `事件解析失败 ${jobId}`)
      }
    },
    onError: () => pushSynthesisDebug('sse', `连接异常或断开 ${jobId}`),
    onRetry: (attempt, delay) => pushSynthesisDebug('sse', `${delay}ms 后第 ${attempt} 次重连`),
  })
}

function startSynthesisTracking(jobId: string) {
  stopSynthesisTracking()
  synthesisPollFailures = 0
  pushSynthesisDebug('track', `开始跟踪 ${jobId}`)
  synthesisPoller = window.setInterval(() => { void refreshSynthesisStatus(jobId) }, 500)
  void refreshSynthesisStatus(jobId)
  connectSynthesisStream(jobId)
}

async function refreshOverview() {
  try {
    const data = await api.get<any>('/api/overview')
    assignConfig(data.config); environment.value = data.environment; training.value = data.training; stats.value = data.training_stats
  } catch (error) { showError(error) }
}
async function refreshHistory() { try { history.value = await api.get<any[]>('/api/history') } catch (error) { showError(error) } }
async function refreshTraining() {
  try {
    const data = await api.get<any>('/api/training/status')
    training.value = data.state
    stats.value = data.stats
    environment.value = data.environment
    running.value = data.running
    logText.value = data.log.text
    if (running.value.length || data.last_job_id) {
      trainMetrics.value = await api.get<any>(`/api/training/metrics?job_id=${running.value[0]?.job_id || data.last_job_id}`).then((r) => r.rows ?? [])
      trainCheckpoints.value = await api.get<any>('/api/training/checkpoints').then((r) => r.rows ?? [])
    } else {
      trainMetrics.value = []
      trainCheckpoints.value = []
    }
    if (typeof data.state?.exp === 'string' && data.state.exp) experiment.value = data.state.exp
  } catch (error) { showError(error) }
}
async function saveConfig() { try { assignConfig(await api.put<Config>('/api/config', config)); notice.value = '已保存为默认配置。' } catch (error) { showError(error) } }
async function scanEnvironment() { try { const data = await api.post<any>('/api/environment/scan'); assignConfig(data.config); environment.value = data.rows; candidates.value = data.candidates; notice.value = data.selected ? `已选择环境：${data.selected}` : '未找到可用 GPT-SoVITS 环境。' } catch (error) { showError(error) } }
async function selectEnvironment(path: string) { if (!path) return; try { const data = await api.post<any>('/api/environment/select', { path }); assignConfig(data.config); environment.value = data.rows; notice.value = `已选择环境：${data.selected}` } catch (error) { showError(error) } }

async function runDoctor(repair: boolean) {
  doctorBusy.value = true
  try {
    applyDoctorReport(await api.post<DoctorReport>('/api/doctor', { repair }))
  } catch (error) {
    showError(error)
  } finally {
    doctorBusy.value = false
  }
}

async function startSynthesis(text: string) {
  if (!text.trim()) return showError('请先输入台词，或导入 TXT。')
  busy.value = true
  synthSummaryDismissed.value = false
  pauseActiveResultsAudio()
  orbPlayback.value = null
  synthOutputs.value = []
  synthesisJob.value = null
  resetOrbState()
  try {
    stopSynthesisTracking()
    synthesisDebugLogs.value = []
    pushSynthesisDebug('start', `提交合成请求，文本长度 ${text.length}`)
    const data = await api.post<SynthesisSnapshot>('/api/synthesis', { ...config, text })
    pushSynthesisDebug('start', `收到任务 ${data.job_id || 'unknown'}，状态 ${data.status}`)
    applySynthesisSnapshot(data)
    if (data.job_id && data.status !== 'done' && data.status !== 'failed') {
      startSynthesisTracking(data.job_id)
    }
    notice.value = data.message || '合成任务已启动。'
  } catch (error) {
    pushSynthesisDebug('start', `提交失败：${error instanceof Error ? error.message : String(error)}`)
    showError(error)
    busy.value = false
  }
}

async function retryOutput(index: number) {
  const item = synthOutputs.value[index]
  if (!item || busy.value || !item.text.trim()) return
  busy.value = true
  synthSummaryDismissed.value = false
  retryingOutputIndex.value = index
  pauseActiveResultsAudio()
  orbPlayback.value = null
  synthesisJob.value = null
  resetOrbState()
  stopSynthesisTracking()
  pushSynthesisDebug('retry', `重新合成第 ${index + 1} 句：${item.text}`)
  try {
    const data = await api.post<SynthesisSnapshot>('/api/synthesis', { ...config, text: item.text })
    applySynthesisSnapshot(data)
    if (data.job_id && data.status !== 'done' && data.status !== 'failed') startSynthesisTracking(data.job_id)
  } catch (error) {
    retryingOutputIndex.value = null
    busy.value = false
    showError(error)
  }
}

function playbackOutputIndex(index: number) {
  return synthOutputs.value[index]?.output_index ?? index
}
function readOrbPlayback(index: number, audio: HTMLAudioElement, paused: boolean): OrbPlayback {
  const output = synthOutputs.value[index]
  return {
    outputIndex: playbackOutputIndex(index),
    currentTime: Number.isFinite(audio.currentTime) ? audio.currentTime : 0,
    duration: Number.isFinite(audio.duration) ? audio.duration : 0,
    tokenCount: Math.max(0, output?.semantic_token_count || 0),
    frameRate: Math.max(0, output?.semantic_frame_rate || 0),
    speed: Math.max(0, Number(config.speed) || 1),
    paused,
  }
}
function startOrbPlayback(index: number, event: Event) {
  const audio = event.currentTarget as HTMLAudioElement
  orbPlayback.value = readOrbPlayback(index, audio, false)
}
function updateOrbPlayback(index: number, event: Event) {
  const audio = event.currentTarget as HTMLAudioElement
  orbPlayback.value = readOrbPlayback(index, audio, audio.paused)
}
function pauseOrbPlayback(index: number) {
  if (orbPlayback.value?.outputIndex === playbackOutputIndex(index)) {
    orbPlayback.value = { ...orbPlayback.value, paused: true }
  }
}
function endOrbPlayback(index: number) {
  pauseOrbPlayback(index)
}

async function uploadReferenceAudio(event: Event) {
  const input = event.target as HTMLInputElement
  const audio = input.files?.[0]
  input.value = ''
  if (!audio) return
  onboardingAudioName.value = audio.name
  onboardingAudioSize.value = formatAudioSize(audio.size)
  const body = new FormData()
  body.append('audio', audio)
  referenceAudioUploading.value = true
  try {
    const result = await api.post<{ path: string }>('/api/reference-audio', body)
    config.ref_audio = result.path
    notice.value = '音色样本已上传。'
  } catch (error) {
    showError(error)
    onboardingAudioName.value = ''
    onboardingAudioSize.value = ''
  } finally {
    referenceAudioUploading.value = false
  }
}

async function importPronunciationPatch(event: Event) {
  const input = event.target as HTMLInputElement
  const patch = input.files?.[0]
  input.value = ''
  if (!patch) return
  const body = new FormData()
  body.append('patch', patch)
  pronunciationPatchBusy.value = true
  try {
    const result = await api.post<{ message: string }>('/api/engine/pronunciation-patch', body)
    notice.value = result.message
  } catch (error) {
    showError(error)
  } finally {
    pronunciationPatchBusy.value = false
  }
}

function synthesize() {
  if (busy.value || !synthesisReady.value) return
  const text = synthesisText.value
  if (!text.trim()) return
  swallowText.value = text
  swallowPlay.value += 1
}
function onSwallowDone() {
  addDraft(swallowText.value)
  void startSynthesis(swallowText.value)
}

function pauseActiveResultsAudio() {
  resultsOverlayRef.value?.querySelectorAll('audio').forEach((audio) => {
    if (!audio.paused) audio.pause()
  })
}

function openResults(trigger?: HTMLElement) {
  if (trigger) {
    resultsTriggerElement = trigger
  } else if (document.activeElement instanceof HTMLElement) {
    resultsTriggerElement = document.activeElement
  }
  resultsOpen.value = true
  void nextTick(() => { resultsCloseButtonRef.value?.focus() })
}

function closeResults() {
  pauseActiveResultsAudio()
  orbPlayback.value = null
  resultsOpen.value = false
  resultsTriggerElement?.focus()
  resultsTriggerElement = null
}

function openReferencePanel(trigger?: HTMLElement) {
  if (trigger) referenceTriggerElement = trigger
  else if (document.activeElement instanceof HTMLElement) referenceTriggerElement = document.activeElement
  referencePanelOpen.value = true
  voiceSampleHelpOpen.value = false
  synthesisParamsOpen.value = false
  synthesisParamsTriggerElement = null
  void nextTick(() => { referenceDrawerCloseRef.value?.focus() })
}

function closeReferencePanel() {
  referencePanelOpen.value = false
  voiceSampleHelpOpen.value = false
  referenceTriggerElement?.focus()
  referenceTriggerElement = null
}

function toggleVoiceSampleHelp() {
  voiceSampleHelpOpen.value = !voiceSampleHelpOpen.value
}

function onReferenceDrawerKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  event.preventDefault()
  if (voiceSampleHelpOpen.value) {
    voiceSampleHelpOpen.value = false
    return
  }
  closeReferencePanel()
}

function openSynthesisParamsPanel(trigger?: HTMLElement) {
  if (trigger) synthesisParamsTriggerElement = trigger
  else if (document.activeElement instanceof HTMLElement) synthesisParamsTriggerElement = document.activeElement
  synthesisParamsOpen.value = true
  referencePanelOpen.value = false
  referenceTriggerElement = null
  void nextTick(() => { synthesisParamsCloseRef.value?.focus() })
}

function closeSynthesisParamsPanel() {
  synthesisParamsOpen.value = false
  synthesisParamsTriggerElement?.focus()
  synthesisParamsTriggerElement = null
}

function onSynthesisParamsKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  event.preventDefault()
  closeSynthesisParamsPanel()
}

function onResultsKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeResults()
    return
  }
  if (event.key !== 'Tab') return
  const overlay = resultsOverlayRef.value
  if (!overlay) return
  const focusables = Array.from(overlay.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, audio, [tabindex]:not([tabindex="-1"])')).filter((element) => !element.hasAttribute('disabled'))
  if (!focusables.length) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const active = document.activeElement
  if (event.shiftKey) {
    if (active === first || !overlay.contains(active)) {
      event.preventDefault()
      last.focus()
    }
  } else if (active === last || !overlay.contains(active)) {
    event.preventDefault()
    first.focus()
  }
}

function applyDraftText(text: string) {
  synthesisText.value = text
  batchFileName.value = ''
  batchLineCount.value = 0
}

async function loadSynthesisBatchFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    synthesisText.value = await file.text()
    batchFileName.value = file.name
    batchLineCount.value = synthesisText.value.split(/\r?\n/).filter((line) => line.trim()).length
    pushSynthesisDebug('file', `已导入批量台词 ${file.name}，共 ${batchLineCount.value} 行`)
  } catch (error) {
    showError(error)
    batchFileName.value = ''
    batchLineCount.value = 0
  } finally {
    input.value = ''
  }
}

async function clearHistory() { try { await api.delete('/api/history'); history.value = []; notice.value = '历史记录已清空。' } catch (error) { showError(error) } }

async function trainingAction(path: string, payload: object = {}) { busy.value = true; try { showResult(await api.post<JobResult>(path, payload)) } catch (error) { showError(error) } finally { busy.value = false } }
async function startSlice() { if (!recording.value) return showError('请先选择录音文件。'); const body = new FormData(); body.append('recording', recording.value); Object.entries(slice).forEach(([key, value]) => body.append(key, String(value))); busy.value = true; try { showResult(await api.post<JobResult>('/api/training/slice', body)) } catch (error) { showError(error) } finally { busy.value = false } }
async function loadTranscript() { try { transcript.value = (await api.get<any>('/api/training/transcript')).rows; notice.value = `已加载 ${transcript.value.length} 句。` } catch (error) { showError(error) } }
async function saveTranscript() { await trainingAction('/api/training/transcript', { rows: transcript.value }) }
async function stopTraining() { await trainingAction('/api/training/stop', {}) }
async function selectModels() { try { const data = await api.post<any>('/api/training/models/select'); assignConfig(data.config); notice.value = data.message } catch (error) { showError(error) } }

function cycleTheme() { config.theme = config.theme === 'workbench' ? 'focus' : 'workbench'; saveConfig() }

const expandedStep = ref<string | null>('slice')
function focusTrainingStep(key: string) {
  expandedStep.value = key
}
function stopRunningTraining() {
  if (running.value.length) stopTraining()
}
function trainingFailureAction(key: string) {
  return {
    slice: '检查录音格式与切片参数后，重新选择文件提交。',
    asr: '检查识别依赖或换用 smaller / medium 模型后重试。',
    correct: '重新加载识别结果，确认没有空文本后保存。',
    feat: '确认广普补丁和依赖已处理，然后重试提取。',
    train: '检查特征数据、GPU 显存和训练日志，再重新启动。',
  }[key] || '查看实时日志后重试。'
}

onMounted(async () => {
  await refreshConfig()
  await refreshOverview()
  await refreshHistory()
  await scanEnvironment()
  await refreshSynthesisReadiness()
  if (synthesisReadiness.value.status === 'warming') synthesisReadinessPoller = window.setInterval(() => { void refreshSynthesisReadiness() }, 1000)
  polling = window.setInterval(() => { if (page.value === 'training' || running.value.length) refreshTraining() }, 2500)
  if (!synthesisOnboarded.value && !voiceConfigured.value) openSynthesisOnboarding()
})

onBeforeUnmount(() => {
  if (orbColorPersistTimer !== null) {
    window.clearTimeout(orbColorPersistTimer)
    orbColorPersistTimer = null
  }
  if (polling) window.clearInterval(polling)
  if (synthesisReadinessPoller) window.clearInterval(synthesisReadinessPoller)
  stopSynthesisTracking()
})
</script>

<template>
  <main class="app-shell" :data-theme="config.theme" :data-mode="colorMode">
    <header class="top-bar">
      <div class="top-bar-brand">
        <img src="/logo.png" alt="像话" />
        <span>像话<br /><small>XIANGHUA</small></span>
      </div>
      <span class="top-bar-spacer" />
      <span class="top-bar-status" :class="statusClass"><i />{{ statusLabel }}</span>
      <div class="top-bar-actions">
        <button class="icon-btn" title="新手引导" @click="openSynthesisOnboarding">
          <HelpCircle class="tool-glyph" aria-hidden="true" />
        </button>
        <button class="icon-btn" title="环境自检" :disabled="doctorBusy" @click="runDoctor(false)">✓</button>
        <button class="icon-btn" title="自动修复" :disabled="doctorBusy" @click="runDoctor(true)">⚡</button>
        <button class="icon-btn" :title="`切换布局 · 当前：${themeLabel}`" @click="cycleTheme">◐</button>
      </div>
    </header>

    <p v-if="notice" class="notice" role="status" aria-live="polite">{{ notice }}</p>

    <section v-if="doctorReport" class="doctor-panel">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px">
        <h2 style="font-size:13px;font-weight:600">{{ doctorReport.repair ? '自动修复结果' : '环境自检结果' }}</h2>
        <span class="doctor-summary">{{ doctorReport.summary }}</span>
        <button class="icon-btn" style="width:22px;height:22px;font-size:14px" @click="doctorReport = null">×</button>
      </div>
      <div class="env-list">
        <div v-for="row in doctorReport.rows" :key="row.name">
          <span><i class="status-dot" :class="row.status" />{{ row.name }}</span>
          <small>{{ row.detail }}</small>
        </div>
      </div>
      <div v-if="doctorReport.actions.length">
        <div v-for="action in doctorReport.actions" :key="`${action.kind}-${action.message}`" class="doctor-action-row">
          <strong>{{ action.kind }}</strong><span>{{ action.message }}</span>
        </div>
      </div>
    </section>

    <div class="app-body">
      <div class="content-area">
      <template v-if="page === 'synthesis'">
        <div class="synthesis-view">
            <div v-if="synthesisVoiceHintVisible" class="synthesis-voice-hint">
              <strong>音色样本未配置</strong>
              <span>生成配音前需要选择样本；可以稍后从顶部「引导」继续。</span>
              <button class="task-head-action" @click="openSynthesisOnboarding">继续设置</button>
            </div>
            <div class="editor-main" :data-synth-stage="synthStage">
              <div class="voice-stage">
                <div
                  ref="voiceStageOrbRef"
                  class="voice-stage-orb interactive"
                  role="button"
                  tabindex="0"
                  aria-label="设置粒子颜色"
                  @click="activateSynthesisStage"
                  @keydown.enter.prevent="activateSynthesisStage"
                  @keydown.space.prevent="activateSynthesisStage"
                >
                  <AudioOrb :active="busy" :total="synthesisJob?.total || 0" :completed-output-indexes="completedOrbOutputs" :failed-output-indexes="failedOrbOutputs" :semantic-token="orbSemanticToken" :playback="orbPlayback" :colors="orbColors" label="点击设置粒子颜色" />
                </div>
                <section v-if="orbColorPanelOpen" ref="orbColorPanelRef" class="orb-color-panel" role="group" aria-label="粒子颜色设置" @click.stop @keydown="onOrbColorPanelKeydown">
                  <div class="orb-color-panel-head">
                    <strong>粒子颜色</strong>
                    <button ref="orbColorCloseRef" class="icon-btn" aria-label="关闭粒子颜色设置" @click="closeOrbColorPanel()">×</button>
                  </div>
                  <div class="orb-color-controls">
                    <label>合成色<input v-model="config.orb_synthesis_token_color" type="color" /></label>
                    <label>聚合色<input v-model="config.orb_settled_color" type="color" /></label>
                    <label>播放色<input v-model="config.orb_playback_token_color" type="color" /></label>
                  </div>
                  <p class="orb-color-note">颜色修改后自动保存。</p>
                </section>
                <div class="voice-stage-status">
                  <strong class="stage-line1">{{ stageLine1 }}</strong>
                  <small class="stage-line2">{{ stageLine2 }}</small>
                <div class="voice-stage-progress" :class="{ visible: synthesisJob && synthesisJob.status === 'running' }">
                    <div class="training-progress-bar"><span :style="{ width: synthesisProgress + '%' }" /></div>
                  </div>
                </div>
                <template v-if="config.theme === 'workbench' && synthStage === 'idle'">
                <div class="stage-state stage-quickstart">
                  <span class="stage-state-title">快速开始</span>
                  <div class="stage-chip-row">
                    <button v-for="sample in SAMPLE_LINES" :key="sample.label" class="stage-chip" @click="applyDraftText(sample.text)">{{ sample.label }}</button>
                  </div>
                  <div v-if="drafts.length" class="stage-chip-row">
                    <button v-for="item in drafts.slice(0, 4)" :key="item.id" class="stage-chip" :title="item.text" @click="applyDraftText(item.text)">{{ draftPreview(item.text) }}</button>
                  </div>
                </div>
                </template>
                <div v-if="config.theme === 'workbench' && synthStage === 'running'" class="stage-state stage-running">
                  <div class="stage-task-row">
                    <span class="stage-task-count">{{ synthesisJob ? `${synthesisJob.completed}/${synthesisJob.total}` : '准备中' }}</span>
                    <div class="stage-task-bar"><span :style="{ width: synthesisProgress + '%' }" /></div>
                  </div>
                  <p v-if="synthesisJob?.current_text" class="stage-current-text" :title="synthesisJob.current_text">{{ synthesisJob.current_text }}</p>
                </div>
                <div v-if="config.theme === 'workbench' && synthStage === 'done'" class="stage-state stage-summary">
                  <div class="stage-chip-row">
                    <button class="stage-chip primary" @click="openResults()">打开结果 · {{ synthOutputs.length }} 条</button>
                    <button class="stage-chip" @click="dismissSynthSummary()">继续编辑</button>
                    <span v-if="failedOutputCount" class="stage-chip warn">{{ failedOutputCount }} 条失败</span>
                  </div>
                </div>
            </div>
            <div class="editor-canvas" :class="{ locked: config.theme === 'focus' && busy, 'has-tray': config.theme === 'focus' && synthStage === 'done' && !synthSummaryDismissed }">
              <header v-if="config.theme === 'focus'" class="task-head">
                <strong>{{ busy ? '当前批次' : '台词台' }}</strong>
                <span>{{ busy ? `${synthesisJob?.completed || 0}/${synthesisJob?.total || 0} · ${synthesisJob?.phase || '准备中'}` : `${synthesisText.length} 字${batchFileName ? ` · ${batchFileName}` : ''}` }}</span>
                <div class="task-tools">
                  <label class="file-trigger text-trigger">
                    <FileUp class="tool-glyph" aria-hidden="true" />
                    <span>导入 TXT</span>
                    <input type="file" accept=".txt" @change="loadSynthesisBatchFile" />
                  </label>
                  <button
                    class="task-head-action"
                    :class="{ attention: !voiceConfigured }"
                    :aria-expanded="referencePanelOpen"
                    aria-controls="reference-drawer"
                    @click="referencePanelOpen ? closeReferencePanel() : openReferencePanel($event.currentTarget as HTMLElement)"
                  >音色样本</button>
                </div>
              </header>
              <div class="editor-scroll">
                <div v-if="showFocusEditorGuide" class="editor-empty-guide" aria-label="台词引导">
                  <p>输入台词，或从草稿开始<small>支持 &lt;en&gt;英语&lt;/en&gt; 等语言标记</small></p>
                  <div class="stage-chip-row">
                    <button v-for="sample in SAMPLE_LINES" :key="sample.label" class="stage-chip" @click="applyDraftText(sample.text)">{{ sample.label }}</button>
                    <button v-for="item in drafts.slice(0, 3)" :key="item.id" class="stage-chip" :title="item.text" @click="applyDraftText(item.text)">{{ draftPreview(item.text) }}</button>
                  </div>
                </div>
                <LanguageMarkerInput v-model="synthesisText" :disabled="busy" :language-options="languageOptions" :placeholder="showFocusEditorGuide ? '' : '输入台词，每行一句。支持 <en>英语</en> 等语言标记，输入 < 即可弹出提示。'" />
                <div v-if="config.theme !== 'focus'" class="editor-meta">
                  <span>{{ synthesisText.length }} 字</span>
                  <span v-if="batchFileName">· {{ batchFileName }}（{{ batchLineCount }} 行）</span>
                  <span class="flex-1" />
                  <label class="file-trigger">
                    导入 TXT
                    <input type="file" accept=".txt" @change="loadSynthesisBatchFile" />
                  </label>
                </div>
              </div>
              <section v-if="synthesisParamsOpen" id="synthesis-params-drawer" ref="synthesisParamsDrawerRef" class="synthesis-params-drawer" aria-label="合成参数" @keydown="onSynthesisParamsKeydown">
                <div class="params-drawer-head">
                  <strong>合成参数</strong>
                  <button ref="synthesisParamsCloseRef" class="icon-btn" aria-label="关闭合成参数" @click="closeSynthesisParamsPanel()">×</button>
                </div>
                <div class="params-drawer-body">
                  <p class="params-note">调整后立即用于下一次生成；保存默认后重启仍会保留。</p>
                  <div class="panel-row panel-row-3">
                    <label class="panel-label">Top-K<input :value="config.top_k" type="number" min="1" max="100" step="1" class="panel-input" @change="config.top_k = readBoundedNumber($event, 30, 1, 100, true)" /></label>
                    <label class="panel-label">Top-P<input :value="config.top_p" type="number" min="0.01" max="1" step="0.01" class="panel-input" @change="config.top_p = readBoundedNumber($event, 0.88, 0.01, 1)" /></label>
                    <label class="panel-label">温度<input :value="config.temperature" type="number" min="0.01" max="2" step="0.01" class="panel-input" @change="config.temperature = readBoundedNumber($event, 0.95, 0.01, 2)" /></label>
                  </div>
                  <div class="panel-row panel-row-3">
                    <label class="panel-label">步数<input :value="config.sample_steps" type="number" min="1" max="256" step="1" class="panel-input" @change="config.sample_steps = readBoundedNumber($event, 64, 1, 256, true)" /></label>
                    <label class="panel-label">语速<input :value="config.speed" type="number" min="0.1" max="3" step="0.1" class="panel-input" @change="config.speed = readBoundedNumber($event, 1, 0.1, 3)" /></label>
                    <label class="panel-label">停顿(s)<input :value="config.pause_second" type="number" min="0" max="10" step="0.05" class="panel-input" @change="config.pause_second = readBoundedNumber($event, 0.15, 0, 10)" /></label>
                  </div>
                  <label class="panel-label">Mixed EOS 下限<input :value="config.mixed_eos_floor ?? ''" type="number" @change="config.mixed_eos_floor = readOptionalNumber($event)" class="panel-input" /></label>
                  <label class="panel-label">Mixed Token 上限<input :value="config.mixed_token_cap ?? ''" type="number" @change="config.mixed_token_cap = readOptionalNumber($event)" class="panel-input" /></label>
                  <label class="params-check"><input v-model="config.auto_lightmid" type="checkbox" />自动 LightMid</label>
                </div>
                <div class="params-drawer-footer">
                  <button class="btn-outline" @click="saveConfig()">保存为默认</button>
                </div>
              </section>
              <section v-if="config.theme === 'focus' && referencePanelOpen" id="reference-drawer" ref="referenceDrawerRef" class="reference-drawer" aria-label="音色样本设置" @keydown="onReferenceDrawerKeydown">
                <div class="reference-drawer-head">
                  <div class="reference-drawer-title">
                    <strong>音色样本</strong>
                    <button
                      class="help-trigger"
                      type="button"
                      aria-label="查看音色样本说明"
                      :aria-expanded="voiceSampleHelpOpen"
                      aria-controls="voice-sample-help"
                      @click="toggleVoiceSampleHelp"
                    >
                      <HelpCircle class="help-glyph" aria-hidden="true" />
                    </button>
                  </div>
                  <button ref="referenceDrawerCloseRef" class="icon-btn" aria-label="关闭音色样本" @click="closeReferencePanel()">×</button>
                </div>
                <div v-if="voiceSampleHelpOpen" id="voice-sample-help" class="sample-explainer" role="note" aria-label="音色样本说明">
                  <strong>这段样本决定最终音色</strong>
                  <p>上传后，系统会模仿它的音色和说话方式。</p>
                  <ul>
                    <li><b>干净人声</b><span>尽量使用清晰人声，避开背景音乐和噪音。</span></li>
                    <li><b>文本一致</b><span>逐字填写音频中实际说出的内容。</span></li>
                  </ul>
                </div>
                <div class="reference-drawer-body">
                  <label class="panel-label" for="reference-path-input">音频文件</label>
                  <div class="reference-audio-row">
                    <input id="reference-path-input" v-model="config.ref_audio" class="panel-input" :disabled="busy" />
                    <label class="reference-audio-upload">{{ referenceAudioUploading ? '上传中…' : '上传音频' }}<input type="file" accept=".wav,.mp3,.m4a,.flac,.ogg,audio/*" :disabled="referenceAudioUploading || busy" @change="uploadReferenceAudio" /></label>
                  </div>
                  <label class="panel-label" for="reference-text-input">这段音频说了什么</label>
                  <textarea id="reference-text-input" v-model="config.ref_text" class="panel-input" rows="3" :disabled="busy" />
                  <label class="panel-label" for="reference-language-input">音频语言</label>
                  <select id="reference-language-input" v-model="config.prompt_lang" class="panel-input" :disabled="busy"><option v-for="option in languageOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select>
                  <label class="panel-label" for="reference-patch-input">发音补丁 · 高级</label>
                  <label class="reference-audio-upload" for="reference-patch-input">{{ pronunciationPatchBusy ? '导入中…' : '导入补丁' }}<input id="reference-patch-input" type="file" accept=".patch,.diff" :disabled="pronunciationPatchBusy || busy" @change="importPronunciationPatch" /></label>
                </div>
                <div class="reference-drawer-footer">
                  <button class="btn-primary" :disabled="busy" @click="saveConfig(); closeReferencePanel()">保存音色样本</button>
                </div>
              </section>
              <section v-if="config.theme === 'focus' && busy" class="focus-queue" aria-label="当前生成队列">
                <div class="queue-run">
                  <div class="training-progress-bar"><span :style="{ width: synthesisProgress + '%' }" /></div>
                  <p class="queue-detail">{{ synthesisProgress }}% · {{ synthesisJob?.completed || 0 }}/{{ synthesisJob?.total || 0 }} · {{ synthesisJob?.phase || '准备中' }}</p>
                </div>
                <div class="focus-queue-list">
                  <div v-for="(item, index) in synthOutputs.slice(0, 4)" :key="`focus-q-${index}`" class="focus-queue-row" :class="outputTaskState(item)" :title="item.text">
                    <i aria-hidden="true" />
                    <span>{{ item.text }}</span>
                    <small>{{ item.error ? '失败' : item.url ? '完成' : outputTaskState(item) === 'active' ? '生成中' : '等待' }}</small>
                  </div>
                  <p v-if="!synthOutputs.length" class="empty-state">正在准备第一批语义 token。</p>
                </div>
              </section>
              <section v-if="config.theme === 'focus' && synthStage === 'done' && !synthSummaryDismissed" class="result-tray" aria-label="合成结果托盘">
                <div class="result-tray-head">
                  <strong>结果托盘 · {{ synthOutputs.length }} 条</strong>
                  <div class="result-tray-actions">
                    <button class="btn-ghost" @click="openResults()">打开结果</button>
                    <button class="btn-ghost" @click="dismissSynthSummary()">继续编辑</button>
                  </div>
                </div>
                <div class="result-tray-body">
                  <div v-for="(item, index) in synthOutputs" :key="`focus-result-${index}`" class="focus-result-row">
                    <div class="focus-result-text" :title="item.text">{{ item.text }}</div>
                    <div class="focus-result-actions">
                      <audio v-if="item.url" controls :src="item.url" class="audio-player" @play="startOrbPlayback(index, $event)" @timeupdate="updateOrbPlayback(index, $event)" @pause="pauseOrbPlayback(index)" @ended="endOrbPlayback(index)" />
                      <span v-if="item.error" class="output-item-error">{{ item.error }}</span>
                      <a v-if="item.url" :href="item.url" download>下载</a>
                      <button class="btn-ghost" :disabled="busy || !synthesisReady" @click="retryOutput(index)">{{ retryingOutputIndex === index ? '重试中…' : '重试' }}</button>
                    </div>
                  </div>
                </div>
              </section>
            </div>
            <div class="action-bar">
              <span v-if="config.theme === 'workbench'" class="lang-chip">中文 · 自动标记</span>
              <span v-else class="action-reason" :class="actionReason.tone" role="status">{{ actionReason.text }}</span>
              <span class="action-bar-spacer" />
              <button
                class="sampling-toggle"
                :class="{ active: synthesisParamsOpen }"
                :aria-expanded="synthesisParamsOpen"
                aria-controls="synthesis-params-drawer"
                @click="synthesisParamsOpen ? closeSynthesisParamsPanel() : openSynthesisParamsPanel($event.currentTarget as HTMLElement)"
              >合成参数</button>
              <button class="btn-primary" :disabled="busy || !synthesisReady || !synthesisText.trim()" @click="synthesize()">{{ busy ? '生成中…' : '生成配音' }}</button>
            </div>
            <div v-if="synthesisReadiness.status === 'failed'" class="synthesis-alert" role="alert" style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 14px;border-top:1px solid var(--danger);background:var(--danger-soft)">
              <div style="min-width:0">
                <strong style="display:block;font-size:13px">模型未就绪</strong>
                <small style="color:var(--text-secondary)">{{ synthesisReadiness.message }}{{ synthesisReadiness.error ? '：' + synthesisReadiness.error : '' }}</small>
              </div>
              <button class="btn-outline" :disabled="synthesisWarmupBusy" @click="retrySynthesisWarmup">{{ synthesisWarmupBusy ? '预热中…' : '重新预热' }}</button>
            </div>
            <OrbSwallow :play="swallowPlay" :text="swallowText" @done="onSwallowDone" />
            <div v-if="resultsOpen" class="results-overlay" ref="resultsOverlayRef" role="dialog" aria-modal="true" aria-label="合成结果" @click.self="closeResults()" @keydown="onResultsKeydown">
              <div class="results-card">
                <div class="results-head">
                  <strong>合成结果<template v-if="synthOutputs.length"> · {{ synthOutputs.length }} 条</template></strong>
                  <button ref="resultsCloseButtonRef" class="icon-btn" aria-label="关闭结果" @click="closeResults()">×</button>
                </div>
                <div class="results-body">
                  <div v-if="synthesisJob && synthesisJob.status === 'running'" style="margin:10px 0 14px">
                    <div style="height:4px;border-radius:2px;background:var(--border);overflow:hidden"><span :style="{ display:'block', height:'100%', width: synthesisProgress + '%', background:'var(--accent)', borderRadius:'2px', transition:'width .3s ease' }" /></div>
                    <p style="font-size:11px;color:var(--text-secondary);margin-top:4px">{{ synthesisJob.completed }}/{{ synthesisJob.total }} · {{ synthesisJob.phase }}{{ synthesisJob.current_text ? ' · ' + synthesisJob.current_text : '' }}</p>
                  </div>
                  <div v-if="!synthOutputs.length" class="empty-state">合成结果会显示在这里。</div>
                  <div v-for="(item, index) in synthOutputs" :key="`${item.text}-${index}`" class="output-item">
                    <div class="output-item-text" :title="item.text">{{ item.text }}</div>
                    <audio v-if="item.url" controls :src="item.url" class="audio-player" @play="startOrbPlayback(index, $event)" @timeupdate="updateOrbPlayback(index, $event)" @pause="pauseOrbPlayback(index)" @ended="endOrbPlayback(index)" />
                    <div class="output-item-actions">
                      <button class="btn-ghost" :disabled="busy || !synthesisReady" @click="retryOutput(index)">{{ retryingOutputIndex === index ? '重试中…' : '重试' }}</button>
                      <a v-if="item.url" :href="item.url" download>下载</a>
                      <span v-if="item.error" class="output-item-error">{{ item.error }}</span>
                    </div>
                  </div>
                  <details style="margin-top:12px">
                    <summary class="btn-ghost" style="cursor:pointer;user-select:none">调试日志</summary>
                    <div style="margin-top:6px;display:flex;gap:6px"><button class="btn-ghost" @click="clearSynthesisDebugLogs">清空</button></div>
                    <ul v-if="synthesisDebugLogs.length" class="debug-log-list" style="margin-top:6px">
                      <li v-for="(entry, index) in synthesisDebugLogs" :key="`${entry.time}-${entry.scope}-${index}`">
                        <span class="debug-log-time">{{ entry.time }}</span><b class="debug-log-scope">{{ entry.scope }}</b><span class="debug-log-message">{{ entry.message }}</span>
                      </li>
                    </ul>
                  </details>
                </div>
              </div>
            </div>
            <div v-if="synthesisOnboardingOpen" class="synthesis-onboarding" role="dialog" aria-modal="true" aria-labelledby="synthesis-onboarding-title">
              <section class="onboarding-card">
                <header class="onboarding-head">
                  <div>
                    <span class="onboarding-eyebrow">首次使用 · {{ synthesisOnboardingStep }} / 3</span>
                    <h2 id="synthesis-onboarding-title">{{ synthesisOnboardingTitle }}</h2>
                    <p>{{ synthesisOnboardingDescription }}</p>
                  </div>
                  <button ref="synthesisOnboardingCloseRef" class="icon-btn" aria-label="跳过新手引导" @click="skipSynthesisOnboarding()">×</button>
                </header>
                <div class="onboarding-body">
                  <nav class="onboarding-progress" aria-label="引导步骤">
                    <button
                      v-for="(step, index) in ['选择样本', '填写原文', '生成试听']"
                      :key="step"
                      class="onboarding-step"
                      :class="{ active: synthesisOnboardingStep === index + 1, complete: synthesisOnboardingStep > index + 1 }"
                      :aria-current="synthesisOnboardingStep === index + 1 ? 'step' : undefined"
                      @click="focusSynthesisOnboardingStep(index + 1)"
                    >
                      <i>{{ synthesisOnboardingStep > index + 1 ? '✓' : index + 1 }}</i>
                      <span>{{ step }}</span>
                    </button>
                  </nav>
                  <div class="onboarding-task">
                    <template v-if="synthesisOnboardingStep === 1">
                      <div class="onboarding-upload">
                        <span class="onboarding-upload-icon" aria-hidden="true">+</span>
                        <div>
                          <strong>{{ onboardingAudioName || (voiceConfigured ? stageVoiceName : '还没有选择音频') }}</strong>
                          <small v-if="onboardingAudioSize">{{ onboardingAudioSize }} · 已上传</small>
                          <small v-else-if="referenceAudioUploading">正在上传…</small>
                          <small v-else>支持 wav、mp3、m4a、flac、ogg</small>
                        </div>
                        <label class="btn-outline onboarding-upload-btn">
                          {{ referenceAudioUploading ? '上传中…' : '选择音频' }}
                          <input type="file" accept=".wav,.mp3,.m4a,.flac,.ogg,audio/*" :disabled="referenceAudioUploading || busy" @change="uploadReferenceAudio" />
                        </label>
                      </div>
                    </template>
                    <template v-else-if="synthesisOnboardingStep === 2">
                      <label class="onboarding-field">
                        <span>这段音频说了什么</span>
                        <textarea v-model="config.ref_text" rows="5" :disabled="busy || onboardingSaving" placeholder="逐字输入样本中的语音内容" />
                      </label>
                    </template>
                    <template v-else>
                      <div class="onboarding-sample">
                        <span>示例台词</span>
                        <div class="onboarding-chips">
                          <button v-for="sample in SAMPLE_LINES" :key="sample.label" type="button" :class="{ active: synthesisText === sample.text }" @click="applyDraftText(sample.text)">{{ sample.label }}</button>
                        </div>
                        <textarea v-model="synthesisText" rows="4" :disabled="busy" placeholder="输入想要快速试听的一句台词" />
                      </div>
                      <div v-if="synthesisOnboardingAlert" class="onboarding-alert">{{ synthesisOnboardingAlert }}</div>
                    </template>
                  </div>
                  <aside class="onboarding-guide" aria-label="引导原则">
                    <strong>引导原则</strong>
                    <ul>
                      <li>只保留当前步骤需要的信息。</li>
                      <li>关闭或跳过入口始终可用。</li>
                      <li>条件不足时直接说明缺什么。</li>
                      <li>完成后进入主工作台。</li>
                    </ul>
                  </aside>
                </div>
                <footer class="onboarding-footer">
                  <button class="btn-ghost" @click="skipSynthesisOnboarding()">跳过引导</button>
                  <button class="btn-primary" :disabled="synthesisOnboardingPrimaryDisabled" @click="synthesisOnboardingStep === 2 ? continueOnboardingFromSample() : synthesisOnboardingStep === 3 ? startOnboardingTrial() : focusSynthesisOnboardingStep(2)">
                    {{ synthesisOnboardingPrimaryLabel }}
                  </button>
                </footer>
              </section>
            </div>
          </div>
        </div>
      </template>

      <template v-else-if="page === 'training'">
        <div class="training-view">
          <div class="view-header">
            <h2>训练</h2>
            <p>从录音到专属音色：切片、识别、校对、特征、训练</p>
          </div>
          <div class="training-workbench">
            <aside class="training-rail" aria-label="训练步骤轨道">
              <div class="training-rail-head">
                <span class="panel-section-title">流程位置</span>
                <button class="btn-ghost" @click="refreshTraining">刷新</button>
              </div>
              <div class="training-rail-progress">
                <div class="training-progress-bar"><span :style="{ width: trainingProgress + '%' }" /></div>
                <small>{{ trainingProgress }}% · {{ statusText(trainStep('train').status) }}</small>
              </div>
              <ol>
                <li v-for="(step, index) in trainingWorkflow" :key="step.key">
                  <button class="training-rail-step" :class="[trainStep(step.key).status, { active: expandedStep === step.key, locked: !step.unlocked }]" @click="focusTrainingStep(step.key)">
                    <span class="training-step-indicator" aria-hidden="true">{{ trainStep(step.key).status === 'done' ? '✓' : trainStep(step.key).status === 'fail' ? '!' : index + 1 }}</span>
                    <span class="training-rail-copy">
                      <strong>{{ step.title }}</strong>
                      <small>{{ step.unlocked ? statusText(trainStep(step.key).status) : '未解锁' }}</small>
                    </span>
                  </button>
                </li>
              </ol>
            </aside>

            <section class="training-panel" aria-labelledby="training-panel-title">
              <header class="training-panel-head">
                <div class="training-panel-title">
                  <span>第 {{ trainingWorkflow.findIndex((step) => step.key === activeTrainingStep.key) + 1 }} / 5 步</span>
                  <h2 id="training-panel-title">{{ activeTrainingStep.title }}</h2>
                  <p>{{ activeTrainingStep.action }}</p>
                </div>
                <div class="training-panel-meta">
                  <div><small>输入</small><span>{{ activeTrainingStep.output === '切片音频' ? '录音文件' : activeTrainingStep.output === '识别文本清单' ? '切片音频' : activeTrainingStep.output === '校对清单' ? '识别文本' : activeTrainingStep.output === '训练数据集' ? '校对清单' : '训练数据集' }}</span></div>
                  <div><small>产出</small><span>{{ activeTrainingStep.output }}</span></div>
                </div>
              </header>
              <div class="training-panel-body">
                <template v-if="activeTrainingStep.key === 'slice'">
                  <div v-if="!activeTrainingStep.unlocked" class="training-lock-note">{{ activeTrainingStep.lockReason }}</div>
                  <template v-else>
                    <div v-if="trainStep('slice').status === 'running'" class="training-status-note">切片进行中，输出见右侧实时日志。</div>
                    <div v-else-if="trainStep('slice').status === 'fail'" class="training-fail-note">切片失败：{{ trainingFailureAction('slice') }}</div>
                    <div v-else-if="trainStep('slice').status === 'done'" class="training-output-note">
                      <span>已生成 {{ Number(stats.sliced || 0) }} 个切片，可进入识别。</span>
                      <button class="btn-outline" @click="focusTrainingStep('asr')">下一步：识别</button>
                    </div>
                    <div class="training-step-card">
                      <header>
                        <strong>录音</strong>
                        <span class="training-step-badge" :class="recording ? 'ready' : 'idle'">{{ recording ? recording.name : '还没选文件' }}</span>
                      </header>
                      <div class="recording-upload-row">
                        <label class="reference-audio-upload">选择录音文件<input type="file" accept="audio/*" @change="recording = ($event.target as HTMLInputElement).files?.[0] || null" /></label>
                        <span v-if="recording" class="recording-file-name">{{ recording.name }}</span>
                      </div>
                    </div>
                    <div class="training-step-card">
                      <header>
                        <strong>切片参数</strong>
                        <span>先按标准起步，切片不理想再微调。</span>
                      </header>
                      <div class="panel-row slice-params">
                        <label class="panel-label">静音阈值<input v-model.number="slice.silence_threshold" type="number" class="panel-input" /></label>
                        <label class="panel-label">最短段长<input v-model.number="slice.min_length" type="number" class="panel-input" /></label>
                        <label class="panel-label">最短间隔<input v-model.number="slice.min_gap" type="number" class="panel-input" /></label>
                        <label class="panel-label">保留静音<input v-model.number="slice.keep_silence" type="number" class="panel-input" /></label>
                      </div>
                      <div class="training-action-row">
                        <button v-if="trainStep('slice').status !== 'running'" class="btn-primary" :disabled="busy" @click="startSlice">{{ trainStep('slice').status === 'done' ? '重新切片' : '提交切片' }}</button>
                        <button class="btn-outline" type="button" @click="resetSliceDefaults">恢复默认</button>
                      </div>
                      <div class="training-status-note">如果切片过碎，先放宽最短间隔；如果单句被切开，再调小静音阈值。</div>
                    </div>
                  </template>
                </template>
                <template v-else-if="activeTrainingStep.key === 'asr'">
                  <div v-if="!activeTrainingStep.unlocked" class="training-lock-note">{{ activeTrainingStep.lockReason }}</div>
                  <template v-else>
                    <div class="training-step-card">
                      <header>
                        <strong>输入数据</strong>
                        <span class="training-step-badge" :class="Number(stats.sliced || 0) > 0 ? 'ready' : 'idle'">{{ Number(stats.sliced || 0) }} 个切片</span>
                      </header>
                      <p>这一步把切片自动转成文字。选一个平衡档；一次识别后集中校对更省时间。</p>
                    </div>
                    <div class="training-step-choice">
                      <button
                        v-for="model in asrModelCards"
                        :key="model.id"
                        :class="{ active: whisperModel === model.id }"
                        type="button"
                        @click="whisperModel = model.id"
                      >
                        <span>{{ model.title }}</span>
                        <small>{{ model.detail }}</small>
                        <i>{{ model.time }}</i>
                      </button>
                    </div>
                    <div class="training-action-row">
                      <button v-if="trainStep('asr').status !== 'running'" class="btn-primary" :disabled="busy" @click="trainingAction('/api/training/asr', { model: whisperModel })">{{ trainStep('asr').status === 'done' ? '重新识别' : '开始识别' }}</button>
                    </div>
                    <div v-if="trainStep('asr').status === 'running'" class="training-status-note">Whisper 识别进行中，长录音可能需要几分钟。</div>
                    <div v-else-if="trainStep('asr').status === 'fail'" class="training-fail-note">识别失败：{{ trainingFailureAction('asr') }}</div>
                    <div v-else-if="trainStep('asr').status === 'done'" class="training-output-note">
                      <span>已生成 {{ Number(stats.lines || 0) }} 条待校对文本。</span>
                      <button class="btn-outline" @click="focusTrainingStep('correct')">下一步：校对</button>
                    </div>
                  </template>
                </template>
                <template v-else-if="activeTrainingStep.key === 'correct'">
                  <div v-if="!activeTrainingStep.unlocked" class="training-lock-note">{{ activeTrainingStep.lockReason }}</div>
                  <template v-else>
                    <div v-if="trainStep('correct').status === 'running'" class="training-status-note">正在保存校对结果…</div>
                    <div v-else-if="trainStep('correct').status === 'fail'" class="training-fail-note">保存失败：{{ trainingFailureAction('correct') }}</div>
                    <div v-else-if="trainStep('correct').status === 'done'" class="training-output-note">
                      <span>校对已保存，共 {{ Number(stats.lines || 0) }} 条文本。</span>
                      <button class="btn-outline" @click="focusTrainingStep('feat')">下一步：特征提取</button>
                    </div>
                    <div class="training-step-card">
                      <header>
                        <strong>校对清单</strong>
                        <span class="training-step-badge" :class="transcriptStats.total ? 'ready' : 'idle'">
                          {{ transcriptStats.total ? `${transcriptStats.total} 句 · ${transcriptStats.empty} 句空文本` : '还没加载' }}
                        </span>
                      </header>
                      <p>加载后每行左边听音，右边改字。这一步是为了让后面特征提取知道每段音频实际说了什么。</p>
                    </div>
                    <div class="training-action-row">
                      <button class="btn-outline" @click="loadTranscript">加载识别结果</button>
                      <button class="btn-outline" :disabled="busy" @click="trainingAction('/api/training/transcript/skip')">跳过校对</button>
                      <button class="btn-primary" :disabled="busy || !transcriptStats.total" @click="saveTranscript">保存校对</button>
                    </div>
                    <div v-if="transcript.length" class="transcript-list">
                      <div v-for="(row, index) in transcript" :key="row.path" class="transcript-row">
                        <span>{{ index + 1 }}</span>
                        <audio v-if="row.audio_url" controls :src="row.audio_url" />
                        <textarea v-model="row.text" />
                      </div>
                    </div>
                    <p v-else class="training-status-note">还没有识别文本，先点击「加载识别结果」。</p>
                  </template>
                </template>
                <template v-else-if="activeTrainingStep.key === 'feat'">
                  <div v-if="!activeTrainingStep.unlocked" class="training-lock-note">{{ activeTrainingStep.lockReason }}</div>
                  <template v-else>
                    <div v-if="trainStep('feat').status === 'running'" class="training-status-note">特征提取进行中，完成后才能开始训练。</div>
                    <div v-else-if="trainStep('feat').status === 'fail'" class="training-fail-note">提取失败：{{ trainingFailureAction('feat') }}</div>
                    <div v-else-if="trainStep('feat').status === 'done'" class="training-output-note">
                      <span>训练数据集已生成，实验名「{{ experiment }}」。</span>
                      <button class="btn-outline" @click="focusTrainingStep('train')">下一步：训练</button>
                    </div>
                    <div class="training-step-card">
                      <header>
                        <strong>实验准备</strong>
                        <span class="training-step-badge" :class="Number(stats.lines || 0) > 0 ? 'ready' : 'idle'">{{ Number(stats.lines || 0) }} 条文本</span>
                      </header>
                      <p>实验名会用于模型输出和后续追溯。如果这是正式版本，建议改为有日期的独立名称，避免覆盖旧实验。</p>
                      <label class="panel-label">实验名<input v-model="experiment" class="panel-input" style="max-width:220px" /></label>
                      <div class="training-action-row">
                        <button v-if="trainStep('feat').status !== 'running'" class="btn-primary" :disabled="busy || !experimentReady" @click="trainingAction('/api/training/features', { experiment: experiment.trim() })">{{ trainStep('feat').status === 'done' ? '重新提取' : '开始提取' }}</button>
                        <span v-if="!experimentReady" class="training-status-note">实验名只能包含字母、数字、下划线或连字符。</span>
                      </div>
                    </div>
                  </template>
                </template>
                <template v-else-if="activeTrainingStep.key === 'train'">
                  <div class="training-observe" aria-label="训练调节台">
                    <div class="training-observe-status">
                      <span class="training-observe-chip" :class="trainingRunState">{{ statusText(trainStep('train').status) }}</span>
                      <span class="training-observe-detail">
                        <i aria-hidden="true">global_step</i><b>{{ trainingStageRow.global_step || '—' }}</b>
                        <i>/mini_batch</i><b>{{ trainingStageRow.batch || '—' }}</b>
                      </span>
                    </div>

                    <template v-if="trainingRunState === 'setup'">
                      <div class="training-param-grid">
                        <label class="panel-label">S1 轮数<input v-model.number="epochs.s1_epochs" type="number" min="1" class="panel-input" /></label>
                        <label class="panel-label">S2 轮数<input v-model.number="epochs.s2_epochs" type="number" min="1" class="panel-input" /></label>
                      </div>
                      <div class="training-param-note">先按当前数据量估一段轮数，训练中可随时改 S2 目标或调低试听频率。</div>
                      <div class="training-action-row">
                        <button class="btn-primary" :disabled="busy || !activeTrainingStep.unlocked" @click="trainingAction('/api/training/run', { experiment, ...epochs })">开始训练</button>
                      </div>
                    </template>

                    <template v-else-if="trainingRunState === 'recover' || trainingRunState === 'running' || trainingRunState === 'done'">
                      <div v-if="trainingRunState === 'recover'" class="training-fail-note">
                        训练已停止或失败：{{ trainingFailureAction('train') }}
                      </div>
                      <div v-else-if="trainingRunState === 'done'" class="training-output-note">
                        训练完成。
                        <span class="training-output-actions">
                          <button class="btn-outline" @click="selectModels">查找模型</button>
                          <button class="btn-outline" @click="page = 'synthesis'">去合成试听</button>
                        </span>
                      </div>
                      <div v-if="trainingRunState === 'running'" class="training-progress-label">
                        <strong>{{ trainingProgress }}%</strong>
                        <span>{{ trainingStageRow.stage || '—' }} · {{ trainingStageRow.epoch || '—' }} / {{ trainingStageRow.epochs || '—' }} epoch</span>
                      </div>
                      <div class="training-progress-bar"><span :style="{ width: trainingProgress + '%' }" /></div>

                      <div class="training-metric-grid">
                        <div v-for="metric in trainingMetricCards" :key="metric.label" class="training-metric-card">
                          <small>{{ metric.label }}</small>
                          <strong>{{ metric.value ?? '—' }}</strong>
                        </div>
                        <div class="training-metric-card">
                          <small>试听</small>
                          <strong>{{ trainingStageRow.sample_interval ? `${trainingStageRow.sample_interval}/step` : '待开始' }}</strong>
                        </div>
                      </div>

                      <div class="training-loss-chart">
                        <header><strong>loss_d / loss_g</strong><span>最近 {{ trainingMetricRows.length }} 个记录点</span></header>
                        <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="训练 loss 曲线">
                          <g class="grid"><line x1="0" y1="25" x2="100" y2="25"/><line x1="0" y1="50" x2="100" y2="50"/><line x1="0" y1="75" x2="100" y2="75"/></g>
                          <path v-for="(path, pathIndex) in trainingLossPath" :key="pathIndex" :d="path" :class="`series-${pathIndex}`" />
                        </svg>
                        <div v-if="!trainingLossSeries.length" class="empty-state">训练开始后，这里有 loss 曲线。</div>
                      </div>

                      <div v-if="trainingRunState !== 'running'" class="training-checkpoint-list">
                        <div class="panel-section-title">最近 Checkpoint</div>
                        <div v-for="(row, index) in trainingCheckpointRows" :key="`${row.job_id}-${index}`" class="training-checkpoint-row">
                          <div>
                            <strong>{{ row.stage ? `S${row.stage}` : 'CP' }} · {{ row.epoch ?? row.stage_epoch ?? '—' }} epoch</strong>
                            <small>{{ row.time || '—' }} · {{ row.path || 'checkpoint 文件' }}</small>
                          </div>
                          <span class="training-best-tag">{{ trainingCheckpointRows[0] === row ? '最近保存' : '备选' }}</span>
                        </div>
                      </div>

                      <div class="training-adjust-grid">
                        <label class="panel-label">S2 目标轮数<input v-model.number="epochs.s2_epochs" type="number" min="1" class="panel-input" /></label>
                        <label class="panel-label">试听频率<input v-model.number="trainingSampleInterval" type="number" min="0" step="1" class="panel-input" /></label>
                      </div>
                      <div class="training-adjust-note">训练前/后可以改所有轮数；运行中建议只调 S2 目标和试听频率。</div>
                      <div class="training-action-row">
                        <button v-if="trainingRunState === 'running'" class="btn-outline" :disabled="busy" @click="stopTraining">停止训练</button>
                        <button v-if="trainingRunState !== 'running'" class="btn-primary" :disabled="busy || !activeTrainingStep.unlocked" @click="trainingAction('/api/training/run', { experiment, ...epochs })">继续训练</button>
                      </div>
                    </template>
                  </div>
                </template>
              </div>
            </section>

            <aside class="training-context" aria-label="训练辅助信息">
              <div class="training-context-block">
                <div class="panel-section-title">训练数据</div>
                <div class="training-data-grid">
                  <div><strong>{{ stats.sliced ?? '—' }}</strong><small>切片数</small></div>
                  <div><strong>{{ stats.lines ?? '—' }}</strong><small>标注行数</small></div>
                  <div><strong>{{ stats.duration ? (Number(stats.duration) / 60).toFixed(1) + ' 分' : '—' }}</strong><small>录音总时长</small></div>
                </div>
              </div>
              <div v-if="environment.length" class="training-context-block">
                <div class="panel-section-title">训练环境</div>
                <div class="env-list">
                  <div v-for="row in environment" :key="row.name">
                    <span><i class="status-dot" :class="row.status" />{{ row.name }}</span>
                    <small>{{ row.detail }}</small>
                  </div>
                </div>
                <div class="training-action-row">
                  <button class="btn-outline" @click="trainingAction('/api/training/patch')">应用补丁</button>
                  <button class="btn-outline" @click="trainingAction('/api/training/dependencies')">安装依赖</button>
                </div>
              </div>
              <div class="training-context-block training-log-block">
                <div class="training-log-head">
                  <span class="panel-section-title">实时日志</span>
                  <button class="btn-ghost" :disabled="!running.length" @click="stopTraining">停止任务</button>
                </div>
                <pre>{{ logText }}</pre>
              </div>
            </aside>
          </div>
        </div>
      </template>

      <template v-else-if="page === 'history'">
        <div class="history-view">
          <div class="view-header">
            <h2>历史记录</h2>
            <p>最近的合成记录，可回听与下载</p>
          </div>
          <div class="history-toolbar">
            <button class="btn-outline" @click="refreshHistory">刷新</button>
            <button class="btn-ghost danger" :disabled="!history.length" @click="clearHistory">清空</button>
          </div>
          <div v-if="!history.length" class="empty-state">
            还没有合成记录。
            <button class="btn-outline" @click="page = 'synthesis'">去生成第一条配音</button>
          </div>
          <div v-for="item in history" :key="item.time + item.name" class="history-item">
            <div class="history-item-meta"><time>{{ item.time }}</time><span v-if="item.params">{{ item.params }}</span></div>
            <div class="history-item-text" :title="item.text">{{ item.text }}</div>
            <audio v-if="item.url" controls :src="item.url" class="audio-player" />
            <span v-else style="font-size:11px;color:var(--text-placeholder)">音频不可用</span>
            <a v-if="item.url" :href="item.url" download style="font-size:11px">下载</a>
          </div>
        </div>
      </template>

      <template v-else-if="page === 'timbre'">
        <TimbrePanel />
      </template>

      <template v-else>
        <div class="settings-view">
          <div class="view-header">
            <h2>设置</h2>
            <p>模型与路径配置，保存为本机默认</p>
          </div>
          <div class="settings-group">
            <div class="settings-group-title">模型路径</div>
            <div class="settings-fields">
              <label class="settings-field">GPT-SoVITS 根目录<input v-model="config.gpt_sovits_root" class="panel-input" /></label>
              <label class="settings-field">GPT 模型<input v-model="config.gpt_model" class="panel-input" /></label>
              <label class="settings-field">SoVITS 模型<input v-model="config.sovits_model" class="panel-input" /></label>
            </div>
          </div>
          <div class="settings-group">
            <div class="settings-group-title">音色样本</div>
            <div class="settings-fields">
              <label class="settings-field">音频文件路径
                <div class="reference-audio-row">
                  <input v-model="config.ref_audio" class="panel-input" />
                  <label class="reference-audio-upload">{{ referenceAudioUploading ? '上传中…' : '上传' }}<input type="file" accept=".wav,.mp3,.m4a,.flac,.ogg,audio/*" :disabled="referenceAudioUploading" @change="uploadReferenceAudio" /></label>
                </div>
              </label>
              <label class="settings-field">这段音频说了什么<input v-model="config.ref_text" class="panel-input" /></label>
            </div>
          </div>
          <div class="settings-group">
            <div class="settings-group-title">运行环境</div>
            <div class="env-list" style="margin-bottom:10px">
              <div v-for="row in environment" :key="row.name">
                <span><i class="status-dot" :class="row.status" />{{ row.name }}</span>
                <small>{{ row.detail }}</small>
              </div>
            </div>
            <div style="display:flex;gap:6px;flex-wrap:wrap">
              <button class="btn-outline" @click="scanEnvironment">扫描环境</button>
              <select v-if="candidates.length" :value="config.gpt_sovits_root" class="panel-input" style="max-width:320px" @change="selectEnvironment(($event.target as HTMLSelectElement).value)">
                <option v-for="item in candidates" :key="item.path" :value="item.path">{{ item.label }}</option>
              </select>
            </div>
          </div>
          <div class="settings-group">
            <div class="settings-group-title">外观</div>
            <div class="settings-fields">
              <label class="settings-field">布局主题<select v-model="config.theme" class="panel-input" style="max-width:240px" @change="saveConfig"><option value="workbench">工作台 · 双栏常驻上下文</option><option value="focus">活页 · 状态驱动主区</option></select></label>
            </div>
          </div>
          <button class="btn-primary" @click="saveConfig">保存设置</button>
        </div>
      </template>
      </div>
      <aside v-if="page === 'synthesis' && config.theme === 'workbench'" class="workbench-aside" aria-label="工作台上下文">
        <section class="wb-section">
          <div class="wb-section-head">
            <span class="panel-section-title">草稿</span>
            <button v-if="drafts.length" class="btn-ghost danger" @click="clearDrafts">清空</button>
          </div>
          <div v-if="!drafts.length" class="empty-state">还没有草稿。点击「生成配音」会自动保存台词。</div>
          <div v-for="item in drafts" :key="item.id" class="draft-item">
            <button class="draft-text" :title="item.text" @click="applyDraftText(item.text)">{{ item.text }}</button>
            <button class="btn-ghost danger" aria-label="删除草稿" @click="removeDraft(item.id)">删</button>
          </div>
        </section>
        <section class="wb-section">
          <div class="wb-section-head"><span class="panel-section-title">队列</span></div>
          <div v-if="synthesisJob && synthesisJob.status === 'running'" class="queue-run">
            <div class="training-progress-bar"><span :style="{ width: synthesisProgress + '%' }" /></div>
            <p class="queue-detail">{{ synthesisJob.completed }}/{{ synthesisJob.total }} · {{ synthesisJob.phase }}</p>
          </div>
          <p v-else class="empty-state">当前没有进行中的任务。</p>
          <div v-if="synthOutputs.length" class="panel-section-title wb-sub">本次结果</div>
          <div v-for="(item, index) in synthOutputs.slice(0, 5)" :key="`q-${index}`" class="queue-row" :title="item.text">
            <span class="queue-row-index">{{ index + 1 }}</span>
            <span class="queue-row-text">{{ item.text }}</span>
            <span class="queue-row-state">{{ item.url ? '完成' : item.error ? '失败' : '待生成' }}</span>
          </div>
          <div v-if="history.length" class="panel-section-title wb-sub">最近完成</div>
          <div v-for="item in history.slice(0, 3)" :key="item.time + item.name" class="queue-row" :title="item.text">
            <span class="queue-row-text">{{ item.text }}</span>
            <time class="queue-row-state">{{ item.time }}</time>
          </div>
        </section>
        <section class="wb-section">
          <div class="wb-section-head"><span class="panel-section-title">音色样本</span></div>
          <label class="panel-label">音频文件<input v-model="config.ref_audio" class="panel-input" /></label>
          <label class="reference-audio-upload">{{ referenceAudioUploading ? '上传中…' : '上传音频' }}<input type="file" accept=".wav,.mp3,.m4a,.flac,.ogg,audio/*" :disabled="referenceAudioUploading" @change="uploadReferenceAudio" /></label>
          <label class="panel-label">音频语言<select v-model="config.prompt_lang" class="panel-input"><option v-for="option in languageOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
          <label class="panel-label">这段音频说了什么<textarea v-model="config.ref_text" class="panel-input" rows="3" /></label>
          <label class="reference-audio-upload">{{ pronunciationPatchBusy ? '导入中…' : '导入发音补丁' }}<input type="file" accept=".patch,.diff" :disabled="pronunciationPatchBusy" @change="importPronunciationPatch" /></label>
          <button class="btn-outline wb-save" @click="saveConfig">保存音色样本</button>
        </section>
      </aside>
      <IconRail
        :page="page"
        :queue-badge="queueBadge"
        :queue-progress="synthesisJob?.status === 'running' ? synthesisProgress : null"
        @update:page="page = $event"
      />
    </div>
  </main>
</template>
