<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, type Config, type DoctorReport, type JobResult, type SynthesisReadiness, type SynthesisSnapshot } from './api'
const AudioOrb = defineAsyncComponent(() => import('./components/AudioOrb.vue'))
const TrainingOrbit = defineAsyncComponent(() => import('./components/TrainingOrbit.vue'))

type Page = 'dashboard' | 'synthesis' | 'batch' | 'training' | 'history' | 'settings'
type EnvRow = { name: string; status: string; detail: string; fix: string }
type Output = { text: string; rendered_text: string; url: string; error: string }
type Transcript = { path: string; text: string; audio_url?: string }
type DebugLog = { time: string; scope: string; message: string }

const page = ref<Page>('dashboard')
const config = reactive<Config>({ gpt_sovits_root: '', gpt_model: '', sovits_model: '', ref_audio: '', ref_text: '', top_k: 30, top_p: .88, temperature: .95, sample_steps: 64, speed: 1, pause_second: .15, auto_lightmid: true, theme: 'auto' })
const environment = ref<EnvRow[]>([])
const candidates = ref<{ label: string; path: string }[]>([])
const history = ref<any[]>([])
const training = ref<any>({ steps: {} })
const stats = ref<any>({})
const running = ref<any[]>([])
const logText = ref('（还没有任务，日志将显示在这里）')
const notice = ref('')
const busy = ref(false)
const synthesisText = ref('')
const synthMode = ref<'single' | 'batch'>('single')
const samplingOpen = ref(false)
const historyOpen = ref(false)
const historyTouchStartY = ref<number | null>(null)
const synthOutputs = ref<Output[]>([])
const synthesisJob = ref<SynthesisSnapshot | null>(null)
const retryingOutputIndex = ref<number | null>(null)
const synthesisDebugLogs = ref<DebugLog[]>([])
const doctorReport = ref<DoctorReport | null>(null)
const doctorBusy = ref(false)
const synthesisReadiness = ref<SynthesisReadiness>({ status: 'warming', message: '模型预热中。', error: '' })
const orbPlaying = ref(false)
const batchText = ref('')
const batchFileName = ref('')
const batchLineCount = ref(0)
const batchOutputDir = ref('')
const batchZip = ref(true)
const batchResult = ref<any>(null)
const recording = ref<File | null>(null)
const slice = reactive({ silence_threshold: -35, min_length: 4000, min_gap: 300, keep_silence: 500 })
const whisperModel = ref('large-v3')
const transcript = ref<Transcript[]>([])
const experiment = ref('myvoice')
const epochs = reactive({ s1_epochs: 15, s2_epochs: 12 })
let polling: number | undefined
let synthesisPoller: number | undefined
let synthesisEventSource: EventSource | null = null
let synthesisReadinessPoller: number | undefined
let bodyOverflowBeforeHistory = ''
let bodyOverscrollBeforeHistory = ''

watch(historyOpen, (open) => {
  if (open) {
    bodyOverflowBeforeHistory = document.body.style.overflow
    bodyOverscrollBeforeHistory = document.body.style.overscrollBehavior
    document.body.style.overflow = 'hidden'
    document.body.style.overscrollBehavior = 'none'
  } else {
    document.body.style.overflow = bodyOverflowBeforeHistory
    document.body.style.overscrollBehavior = bodyOverscrollBeforeHistory
  }
})

const pageTitle = computed(() => ({ dashboard: ['仪表盘', '环境、训练进度与最近合成，一屏总览'], synthesis: ['合成', '写台词，一键生成带广味的配音'], batch: ['批量合成', '上传台词文件，整批产出并打包'], training: ['训练', '从录音到专属音色：切片、识别、校对、特征、训练'], history: ['历史记录', '最近的合成记录，可回听与下载'], settings: ['设置', '模型与路径配置，保存为本机默认'] })[page.value])
const trainStep = (name: string) => training.value.steps?.[name] || { status: 'pending', title: name }
const pendingTrainingSteps = computed(() => ['slice', 'asr', 'correct', 'feat', 'train'].map((key) => ({ key, ...trainStep(key) })).filter((step) => ['pending', 'running', 'fail'].includes(step.status)))
const environmentAlerts = computed(() => environment.value.filter((row) => ['bad', 'warn'].includes(row.status)))
const todaySynthesisCount = computed(() => {
  const now = new Date()
  const prefix = `${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  return history.value.filter((item) => String(item.time || '').startsWith(prefix)).length
})
const totalDurationLabel = computed(() => {
  const seconds = Number(stats.value.duration || 0)
  return seconds ? `${(seconds / 60).toFixed(1)} 分钟` : '—'
})
const trainingProgress = computed(() => Number(trainStep('train').progress?.overall_percent || 0))
const availableModelCount = computed(() => [config.gpt_model, config.sovits_model].filter((value) => Boolean(String(value || '').trim())).length)
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

function assignConfig(value: Partial<Config>) { Object.assign(config, value) }
function showError(error: unknown) { notice.value = error instanceof Error ? error.message : String(error) }
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
  const hadStream = Boolean(synthesisEventSource)
  if (synthesisPoller) {
    window.clearInterval(synthesisPoller)
    synthesisPoller = undefined
  }
  if (synthesisEventSource) {
    synthesisEventSource.close()
    synthesisEventSource = null
  }
  if (hadPoller) pushSynthesisDebug('poll', '已停止状态轮询')
  if (hadStream) pushSynthesisDebug('sse', '已关闭事件流')
}
function normalizeSynthesisSnapshot(data: Partial<SynthesisSnapshot> & { outputs?: any[] } | null | undefined): SynthesisSnapshot {
  const outputs = (data?.outputs || []).map((item) => ({
    text: String(item?.text || ''),
    rendered_text: String(item?.rendered_text || ''),
    url: String(item?.url || ''),
    error: String(item?.error || ''),
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
function applySynthesisSnapshot(data: Partial<SynthesisSnapshot> & { outputs?: any[] }) {
  const snapshot = normalizeSynthesisSnapshot(data)
  synthesisJob.value = snapshot
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
async function refreshSynthesisStatus(jobId: string) {
  try {
    pushSynthesisDebug('poll', `请求 ${jobId}`)
    const data = await api.get<SynthesisSnapshot>(`/api/synthesis/status/${jobId}`)
    applySynthesisSnapshot(data)
  } catch (error) {
    pushSynthesisDebug('poll', `${jobId} 获取状态失败：${error instanceof Error ? error.message : String(error)}`)
    if (error instanceof Error && !error.message.includes('请求失败')) {
      return
    }
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
  try {
    synthesisReadiness.value = await api.post<SynthesisReadiness>('/api/synthesis/readiness/retry')
    if (synthesisReadiness.value.status === 'warming' && !synthesisReadinessPoller) {
      synthesisReadinessPoller = window.setInterval(() => { void refreshSynthesisReadiness() }, 1000)
    }
  } catch (error) {
    showError(error)
  }
}
function connectSynthesisStream(jobId: string) {
  if (!window.EventSource) {
    pushSynthesisDebug('sse', '当前浏览器不支持 EventSource')
    return
  }
  pushSynthesisDebug('sse', `连接 ${jobId}`)
  synthesisEventSource = new EventSource(`/api/synthesis/events/${jobId}`)
  synthesisEventSource.onopen = () => {
    pushSynthesisDebug('sse', `已连接 ${jobId}`)
  }
  synthesisEventSource.onmessage = (event) => {
    try {
      pushSynthesisDebug('sse', `收到事件 ${jobId}`)
      applySynthesisSnapshot(JSON.parse(event.data))
    } catch {
      pushSynthesisDebug('sse', `事件解析失败 ${jobId}`)
    }
  }
  synthesisEventSource.onerror = () => {
    pushSynthesisDebug('sse', `连接异常或断开 ${jobId}`)
    if (synthesisEventSource) {
      synthesisEventSource.close()
      synthesisEventSource = null
    }
  }
}
function startSynthesisTracking(jobId: string) {
  stopSynthesisTracking()
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
  try { const data = await api.get<any>('/api/training/status'); training.value = data.state; stats.value = data.stats; environment.value = data.environment; running.value = data.running; logText.value = data.log.text } catch (error) { showError(error) }
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
async function synthesize() {
  busy.value = true
  synthOutputs.value = []
  synthesisJob.value = null
  try {
    stopSynthesisTracking()
    synthesisDebugLogs.value = []
    pushSynthesisDebug('start', `提交合成请求，文本长度 ${synthesisText.value.length}`)
    const data = await api.post<SynthesisSnapshot>('/api/synthesis', { ...config, text: synthesisText.value })
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
  retryingOutputIndex.value = index
  synthesisJob.value = null
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
async function loadBatchFile(event: Event) { const file = (event.target as HTMLInputElement).files?.[0]; if (file) batchText.value = await file.text() }
async function loadSynthesisBatchFile(event: Event) { const input = event.target as HTMLInputElement; const file = input.files?.[0]; if (!file) return; batchText.value = await file.text(); batchFileName.value = file.name; batchLineCount.value = batchText.value.split(/\r?\n/).filter((line) => line.trim()).length; pushSynthesisDebug('file', `已导入批量台词 ${file.name}，共 ${batchLineCount.value} 行`); input.value = '' }
async function synthesizeBatch() { if (!batchText.value.trim()) return showError('请先导入 TXT 台词文件。'); busy.value = true; try { batchResult.value = await api.post<any>('/api/batch-synthesis', { ...config, text: batchText.value, output_dir: batchOutputDir.value || null, zip_output: batchZip.value }); synthOutputs.value = batchResult.value.outputs || []; notice.value = `完成 ${batchResult.value.completed}/${batchResult.value.total}`; await refreshOverview(); await refreshHistory() } catch (error) { showError(error) } finally { busy.value = false } }
async function openHistory() { historyOpen.value = true; await refreshHistory() }
function closeHistory() { historyOpen.value = false; historyTouchStartY.value = null }
function historyTouchStart(event: TouchEvent) { historyTouchStartY.value = event.touches[0]?.clientY ?? null }
function historyTouchEnd(event: TouchEvent) { const start = historyTouchStartY.value; const end = event.changedTouches[0]?.clientY; if (start !== null && end !== undefined && end - start > 55) closeHistory() }
async function clearHistory() { try { await api.delete('/api/history'); history.value = []; notice.value = '历史记录已清空。' } catch (error) { showError(error) } }
async function trainingAction(path: string, payload: object = {}) { busy.value = true; try { showResult(await api.post<JobResult>(path, payload)) } catch (error) { showError(error) } finally { busy.value = false } }
async function startSlice() { if (!recording.value) return showError('请先选择录音文件。'); const body = new FormData(); body.append('recording', recording.value); Object.entries(slice).forEach(([key, value]) => body.append(key, String(value))); busy.value = true; try { showResult(await api.post<JobResult>('/api/training/slice', body)) } catch (error) { showError(error) } finally { busy.value = false } }
async function loadTranscript() { try { transcript.value = (await api.get<any>('/api/training/transcript')).rows; notice.value = `已加载 ${transcript.value.length} 句。` } catch (error) { showError(error) } }
async function saveTranscript() { await trainingAction('/api/training/transcript', { rows: transcript.value },) }
async function stopTraining() { await trainingAction('/api/training/stop', {}) }
async function selectModels() { try { const data = await api.post<any>('/api/training/models/select'); assignConfig(data.config); notice.value = data.message } catch (error) { showError(error) } }
function cycleTheme() { config.theme = config.theme === 'dark' ? 'light-green' : config.theme === 'light-green' ? 'light-blue' : 'dark'; saveConfig() }

onMounted(async () => { await refreshOverview(); await refreshHistory(); await scanEnvironment(); await refreshSynthesisReadiness(); if (synthesisReadiness.value.status === 'warming') synthesisReadinessPoller = window.setInterval(() => { void refreshSynthesisReadiness() }, 1000); polling = window.setInterval(() => { if (page.value === 'training' || running.value.length) refreshTraining() }, 2500) })
 onBeforeUnmount(() => {
   if (polling) window.clearInterval(polling)
   if (synthesisReadinessPoller) window.clearInterval(synthesisReadinessPoller)
   stopSynthesisTracking()
   if (historyOpen.value) {
     document.body.style.overflow = bodyOverflowBeforeHistory
     document.body.style.overscrollBehavior = bodyOverscrollBeforeHistory
   }
 })
</script>

<template>
  <main class="app-shell app-glow" :class="{'batch-mode': page === 'synthesis' && synthMode === 'batch', 'history-drawer-open': historyOpen && page === 'synthesis'}" :data-theme="config.theme">
    <aside class="sidebar"><div class="brand"><img src="/logo.png" alt="像话" /><span>像话<br><small>XiangHua</small></span></div>
      <nav><button v-for="item in [['dashboard','仪表盘'],['synthesis','合成'],['training','训练'],['history','历史记录'],['settings','设置']]" :key="item[0]" :class="{active: page === item[0]}" @click="page = item[0] as Page">{{ item[1] }}</button></nav>
      <a class="doc-link" href="https://github.com/MouYuc/Clone-youself/blob/main/docs/WEBUI_GUIDE.md" target="_blank" rel="noreferrer">使用文档</a>
    </aside>
     <section class="content"><header><div><div class="header-kicker">XIANGHUA / {{ page.toUpperCase() }}</div><h1>{{ pageTitle[0] }}</h1><p>{{ pageTitle[1] }}</p></div><div class="header-actions"><button v-if="page === 'synthesis'" class="history-trigger" title="打开合成历史" aria-label="打开合成历史" @click="openHistory"><span class="history-icon" aria-hidden="true"></span><span class="history-trigger-label">合成历史</span><b class="history-count-badge">{{ history.length }}</b></button><span class="live-chip" :class="{ active: running.length || busy }"><i></i>{{ busy ? '处理中' : running.length ? '任务运行中' : '系统就绪' }}</span><button class="doctor-button" :disabled="doctorBusy" @click="runDoctor(false)">{{ doctorBusy ? '自检中…' : '自检' }}</button><button class="primary doctor-button" :disabled="doctorBusy" @click="runDoctor(true)">{{ doctorBusy ? '修复中…' : 'Doctor' }}</button><button class="icon-button" title="切换主题" @click="cycleTheme">◐</button><button class="icon-button" title="刷新当前数据" @click="page === 'training' ? refreshTraining() : refreshOverview()">↻</button></div></header>
      <p v-if="notice" class="notice" role="status" aria-live="polite">{{ notice }}</p><section v-if="doctorReport" class="panel doctor-panel"><div class="panel-head"><div><span class="section-eyebrow">DOCTOR / REPORT</span><h2>{{ doctorReport.repair ? '自动修复结果' : '环境自检结果' }}</h2></div><span class="doctor-summary">{{ doctorReport.summary }}</span></div><div class="doctor-actions"><span v-if="doctorReport.selected" class="doctor-selected">已选环境：{{ doctorReport.selected }}</span><span v-if="doctorReport.actions.length" class="doctor-action-count">{{ doctorReport.actions.length }} 项操作</span></div><div class="env-list"><div v-for="row in doctorReport.rows" :key="row.name"><span><i :class="row.status"></i>{{ row.name }}</span><span>{{ row.detail }}</span></div></div><div v-if="doctorReport.actions.length" class="doctor-action-list"><div v-for="action in doctorReport.actions" :key="`${action.kind}-${action.message}`" class="doctor-action-row"><strong>{{ action.kind }}</strong><span>{{ action.message }}</span></div></div></section>

       <template v-if="page === 'dashboard'"><section class="dashboard-metrics" aria-label="关键指标"><article class="metric-card"><span>今日合成</span><strong>{{ todaySynthesisCount }}</strong><small>条音频</small></article><article class="metric-card"><span>累计音频</span><strong>{{ totalDurationLabel }}</strong><small>训练录音</small></article><article class="metric-card"><span>训练进度</span><strong>{{ trainingProgress }}%</strong><small>{{ trainStep('train').status }}</small></article><article class="metric-card"><span>可用模型</span><strong>{{ availableModelCount }}/2</strong><small>GPT / SoVITS</small></article></section><section class="dashboard-workbench"><section class="panel pending-training"><div class="panel-head"><div><span class="section-eyebrow">NEXT ACTION</span><h2>待处理训练步骤</h2></div><button @click="page = 'training'">进入训练</button></div><div v-if="!pendingTrainingSteps.length" class="empty compact-empty">训练流程已完成。</div><button v-for="step in pendingTrainingSteps" :key="step.key" class="pending-step" @click="page = 'training'"><span>{{ step.title }}</span><small>{{ step.status }}</small><b>→</b></button></section><section class="panel dashboard-alerts"><div class="panel-head"><div><span class="section-eyebrow">SYSTEM WATCH</span><h2>异常提醒</h2></div><span class="alert-count">{{ environmentAlerts.length }}</span></div><div v-if="!environmentAlerts.length" class="alert-empty"><i class="status-dot ok"></i>运行环境正常</div><div v-for="row in environmentAlerts" :key="row.name" class="alert-row"><i class="status-dot" :class="row.status"></i><div><strong>{{ row.name }}</strong><small>{{ row.detail }}</small></div></div></section></section><div class="dashboard-grid"><section class="panel environment"><div class="panel-head"><h2>运行环境</h2><button @click="scanEnvironment">扫描</button></div><div class="env-list"><div v-for="row in environment" :key="row.name"><span><i :class="row.status"></i>{{ row.name }}</span><span>{{ row.detail }}</span></div></div><select v-if="candidates.length" :value="config.gpt_sovits_root" @change="selectEnvironment(($event.target as HTMLSelectElement).value)"><option v-for="item in candidates" :key="item.path" :value="item.path">{{ item.label }}</option></select></section>
        <section class="panel"><h2>训练进度</h2><div class="progress"><span :style="{width: (trainStep('train').progress?.overall_percent || 0) + '%'}"></span></div><strong>{{ trainStep('train').progress?.overall_percent || 0 }}%</strong><p>{{ trainStep('train').title }} · {{ trainStep('train').status }}</p><div class="step-pills"><span v-for="key in ['slice','asr','correct','feat','train']" :key="key" :class="trainStep(key).status">{{ trainStep(key).title }}</span></div><button @click="page = 'training'">进入训练</button></section>
         <section class="panel wide"><div class="panel-head"><h2>最近合成</h2><button @click="page = 'history'">全部记录</button></div><div v-if="!history.length" class="empty">还没有合成记录。</div><div v-for="item in history.slice(0, 5)" :key="item.time + item.name" class="history-row"><span>{{ item.time }}</span><span>{{ item.text }}</span><audio v-if="item.url" controls :src="item.url"></audio></div></section></div></template>

       <template v-else-if="page === 'synthesis'"><div class="synthesis-grid"><section class="panel lyric-panel"><div class="lyric-head"><label>台词<input v-model="synthesisText" class="visually-hidden" aria-label="台词" /></label><label class="synthesis-upload">导入 TXT<input type="file" accept=".txt,text/plain" @change="loadSynthesisBatchFile" /></label><span v-if="batchFileName" class="synthesis-upload-status">{{ batchFileName }} · {{ batchLineCount }} 行</span></div><p v-if="!synthesisReady" class="notice" role="status">{{ synthesisReadiness.status === 'warming' ? '模型预热中，请稍候。' : synthesisReadiness.error || synthesisReadiness.message }} <button v-if="synthesisReadiness.status === 'failed'" type="button" @click="retrySynthesisWarmup">重新预热</button></p><div class="lyric-editor-row" :class="{ 'sampling-open': samplingOpen }"><textarea v-model="synthesisText" placeholder="一行一句，# 开头为注释"></textarea><aside class="sampling-panel" :class="{ 'is-open': samplingOpen }" aria-label="采样参数" :aria-hidden="!samplingOpen" :inert="!samplingOpen"><div class="sampling-panel-head"><strong>采样参数</strong><button type="button" aria-label="关闭采样参数" @click="samplingOpen = false">×</button></div><div class="control-grid"><label>Top K<input v-model.number="config.top_k" type="number" /></label><label>Top P<input v-model.number="config.top_p" type="number" step=".01" /></label><label>温度<input v-model.number="config.temperature" type="number" step=".01" /></label><label>采样步数<input v-model.number="config.sample_steps" type="number" /></label><label>语速<input v-model.number="config.speed" type="number" step=".05" /></label><label>句间停顿<input v-model.number="config.pause_second" type="number" step=".05" /></label></div></aside></div><div class="inline-controls"><button type="button" class="sampling-toggle" :aria-expanded="samplingOpen" @click="samplingOpen = !samplingOpen">{{ samplingOpen ? '收起参数' : '采样参数' }}</button><label><input v-model="config.auto_lightmid" type="checkbox" /> 自动转轻中广普</label><button type="button" class="batch-action" :disabled="busy || !synthesisReady || !batchText.trim()" @click="synthesizeBatch">{{ busy ? '批量合成中…' : '批量合成' }}</button><button class="primary" :disabled="busy || !synthesisReady" @click="synthesize">{{ busy ? '合成中…' : '开始合成' }}</button><button @click="synthesisText = ''; synthOutputs = []; synthesisJob = null">清空</button></div></section><section class="panel output-panel"><h2>输出</h2><div v-if="synthesisJob"><div class="progress"><span :style="{width: synthesisProgress + '%'}"></span></div><p>{{ synthesisJob.completed }}/{{ synthesisJob.total }} · {{ synthesisJob.phase }} · {{ synthesisJob.current_text || synthesisJob.message }}</p></div><AudioOrb :active="busy || orbPlaying" :status="synthesisJob?.status" :phase="synthesisJob?.phase" :completed="synthesisJob?.completed || 0" :total="synthesisJob?.total || 0" :progress="synthesisJob?.progress" :label="synthOutputs.length ? '点击试听结果' : '等待合成'" /><div v-if="!synthOutputs.length" class="empty">合成结果会显示在这里。</div><div v-for="(item, index) in synthOutputs" :key="`${item.text}-${index}`" class="output-row"><span>{{ item.text }}</span><audio v-if="item.url" controls :src="item.url" @play="orbPlaying = true" @pause="orbPlaying = false" @ended="orbPlaying = false"></audio><button type="button" :disabled="busy || !synthesisReady" @click="retryOutput(index)">{{ retryingOutputIndex === index ? '重试中…' : '重试' }}</button><a v-if="item.url" :href="item.url" download>下载</a><small v-if="item.error">{{ item.error }}</small></div><section class="debug-panel" aria-label="合成调试日志"><div class="panel-head"><h2>调试日志</h2><button type="button" @click="clearSynthesisDebugLogs">清空</button></div><div v-if="!synthesisDebugLogs.length" class="empty compact-empty">日志会显示在这里。</div><ul v-else class="debug-log-list"><li v-for="(entry, index) in synthesisDebugLogs" :key="`${entry.time}-${entry.scope}-${index}`"><span class="debug-log-time">{{ entry.time }}</span><b class="debug-log-scope">{{ entry.scope }}</b><span class="debug-log-message">{{ entry.message }}</span></li></ul></section></section><section class="panel model-panel"><h2>模型与参考音频</h2><div class="form-grid"><label>GPT 模型<input v-model="config.gpt_model" /></label><label>SoVITS 模型<input v-model="config.sovits_model" /></label><label>参考音频<input v-model="config.ref_audio" /></label><label>参考文本<input v-model="config.ref_text" /></label></div><button @click="saveConfig">保存为默认</button></section></div></template>
      <template v-else-if="page === 'batch'"><section class="panel batch-panel"><h2>批量台词</h2><input type="file" accept=".txt,text/plain" @change="loadBatchFile" /><textarea v-model="batchText" placeholder="上传 UTF-8 文本文件，或直接粘贴台词"></textarea><div class="form-grid"><label>输出目录（可选）<input v-model="batchOutputDir" placeholder="G:\output\my-batch" /></label><label><input v-model="batchZip" type="checkbox" /> 额外打包 ZIP</label></div><button class="primary" :disabled="busy" @click="synthesizeBatch">开始批量合成</button><div v-if="batchResult" class="output-list"><div v-for="item in batchResult.outputs" :key="item.text" class="output-row"><span>{{ item.text }}</span><audio v-if="item.url" controls :src="item.url"></audio><small v-if="item.error">{{ item.error }}</small></div><a v-if="batchResult.zip_url" :href="batchResult.zip_url" download>下载 ZIP</a></div></section></template>

      <template v-else-if="page === 'training'"><div class="training-top"><section class="panel"><div class="panel-head"><h2>训练环境</h2><div><button @click="trainingAction('/api/training/patch')">应用补丁</button><button @click="trainingAction('/api/training/dependencies')">安装依赖</button></div></div><div class="env-list"><div v-for="row in environment" :key="row.name"><span><i :class="row.status"></i>{{ row.name }}</span><span>{{ row.detail }}</span></div></div></section><section class="panel stats"><h2>训练数据</h2><strong>{{ stats.sliced ?? '—' }}</strong><span>切片数</span><strong>{{ stats.lines ?? '—' }}</strong><span>标注行数</span><strong>{{ stats.duration ? Number(stats.duration / 60).toFixed(1) + ' 分钟' : '—' }}</strong><span>录音总时长</span></section><TrainingOrbit :steps="training.steps || {}" /></div>
        <section class="panel step-card"><div><h2>① 录音与切片</h2><p>{{ trainStep('slice').status }}</p><input type="file" accept="audio/*" @change="recording = ($event.target as HTMLInputElement).files?.[0] || null" /><div class="control-grid compact"><label>静音阈值<input v-model.number="slice.silence_threshold" type="number" /></label><label>最短段长<input v-model.number="slice.min_length" type="number" /></label><label>最短间隔<input v-model.number="slice.min_gap" type="number" /></label><label>保留静音<input v-model.number="slice.keep_silence" type="number" /></label></div></div><button class="primary" :disabled="busy" @click="startSlice">提交切片</button></section>
        <section class="panel step-card"><div><h2>② Whisper 识别</h2><p>{{ trainStep('asr').status }}</p><label>识别模型<select v-model="whisperModel"><option>large-v3</option><option>medium</option><option>small</option></select></label></div><button class="primary" :disabled="busy" @click="trainingAction('/api/training/asr', { model: whisperModel })">开始识别</button></section>
        <section class="panel"><div class="panel-head"><div><h2>③ 人工校对</h2><p>{{ trainStep('correct').status }}</p></div><div><button @click="loadTranscript">加载识别结果</button><button @click="trainingAction('/api/training/transcript/skip')">跳过校对</button><button class="primary" @click="saveTranscript">保存校对</button></div></div><div v-if="transcript.length" class="transcript"><div v-for="(row, index) in transcript" :key="row.path"><span>{{ index + 1 }}</span><audio v-if="row.audio_url" controls :src="row.audio_url"></audio><textarea v-model="row.text" /></div></div></section>
        <section class="panel step-card"><div><h2>④ 特征提取</h2><p>{{ trainStep('feat').status }}</p><label>实验名<input v-model="experiment" /></label></div><button class="primary" :disabled="busy" @click="trainingAction('/api/training/features', { experiment })">开始提取</button></section>
        <section class="panel step-card"><div><h2>⑤ 模型训练</h2><p>{{ trainStep('train').status }}</p><div class="control-grid compact"><label>S1 轮数<input v-model.number="epochs.s1_epochs" type="number" min="1" /></label><label>S2 轮数<input v-model.number="epochs.s2_epochs" type="number" min="1" /></label></div></div><div><button class="primary" :disabled="busy" @click="trainingAction('/api/training/run', { experiment, ...epochs })">开始训练</button><button @click="selectModels">查找模型</button></div></section>
        <section class="panel log"><div class="panel-head"><h2>实时日志</h2><div><button @click="refreshTraining">刷新</button><button :disabled="!running.length" @click="stopTraining">停止任务</button></div></div><pre>{{ logText }}</pre></section></template>

      <template v-else-if="page === 'history'"><section class="panel"><div class="panel-head"><h2>历史记录</h2><button @click="clearHistory">清空</button></div><div v-if="!history.length" class="empty">还没有合成记录。</div><div v-for="item in history" :key="item.time + item.name" class="history-row full"><span>{{ item.time }}</span><span>{{ item.text }}</span><span>{{ item.params }}</span><audio v-if="item.url" controls :src="item.url"></audio><a v-if="item.url" :href="item.url" download>下载</a></div></section></template>

       <template v-else><section class="panel"><h2>路径与默认参数</h2><div class="form-grid settings"><label>GPT-SoVITS 根目录<input v-model="config.gpt_sovits_root" /></label><label>GPT 模型<input v-model="config.gpt_model" /></label><label>SoVITS 模型<input v-model="config.sovits_model" /></label><label>参考音频<input v-model="config.ref_audio" /></label><label>参考文本<input v-model="config.ref_text" /></label><label>主题<select v-model="config.theme"><option value="auto">跟随系统</option><option value="light-green">浅绿</option><option value="light-blue">浅蓝</option><option value="dark">深色</option></select></label></div><button class="primary" @click="saveConfig">保存设置</button></section></template>
         <Transition name="history-backdrop"><div v-if="historyOpen && page === 'synthesis'" class="history-backdrop" aria-hidden="true" @click="closeHistory"></div></Transition><Transition name="history-drawer"><aside v-if="historyOpen && page === 'synthesis'" class="synthesis-history-drawer" aria-label="合成历史" @click.stop @wheel.stop @touchstart="historyTouchStart" @touchmove.stop @touchend="historyTouchEnd"><div class="drawer-head"><div><span class="section-eyebrow">ARCHIVE / ALL</span><h2>合成历史</h2><p>{{ history.length }} 条记录</p></div><button class="drawer-close" aria-label="关闭合成历史" @click="closeHistory">×</button></div><div class="drawer-toolbar"><button @click="refreshHistory">刷新</button><button class="danger-button" :disabled="!history.length" @click="clearHistory">清空记录</button></div><div v-if="!history.length" class="empty drawer-empty">还没有合成记录。</div><div v-else class="drawer-history-list"><article v-for="item in history" :key="item.time + item.name" class="drawer-history-item"><div class="drawer-history-main"><time>{{ item.time }}</time><span :title="item.text">{{ item.text }}</span></div><div class="drawer-history-media"><audio v-if="item.url" controls :src="item.url"></audio><span v-else class="audio-missing">音频不可用</span><a v-if="item.url" :href="item.url" download>下载</a></div></article></div></aside></Transition>
     </section>
  </main>
</template>
