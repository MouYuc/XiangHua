<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, UPLOAD_TIMEOUT_MS } from '@/lib/api'
import { usePage } from '@/composables/usePage'
import { useTraining } from '@/composables/useTraining'
import { useToast } from '@/composables/useToast'
import { usePolling } from '@/composables/usePolling'
import AudioPlayer from '@/components/AudioPlayer.vue'

defineOptions({ name: 'WorkshopView' })

const { setPage } = usePage()
const training = useTraining()
const toast = useToast()

type MediaJob = { status: string; stage: string; progress: number; message: string; error: string }
type MediaSpeaker = {
  speaker_id: string
  label: string
  duration_seconds: number
  segment_count: number
  sample_url: string
  confidence: number
}
type MediaSegment = {
  segment_id: string
  speaker_id: string
  start: number
  end: number
  duration_seconds: number
  text: string
  audio_url: string
}
type MediaDataset = {
  dataset_id: string
  stats?: { segment_count?: number; duration_seconds?: number; train?: number; val?: number; rejected?: number }
  train_ready?: boolean
}
type MediaSession = {
  session_id: string
  name: string
  language: string
  source: { filename: string; duration_seconds: number; codec: string; sample_rate: number; channels: number } | null
  job: MediaJob
  speakers: MediaSpeaker[]
  segments: MediaSegment[]
  selected_speaker: string
  dataset: MediaDataset | null
}

const session = ref<MediaSession | null>(null)
const activated = ref(false)
const rights = ref(false)
const uploadLanguage = ref('zh')
const busy = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
// 当前分析的会话 ID；轮询控制器在下面统一创建。
const pollingSessionId = ref('')

const job = computed<MediaJob>(() => session.value?.job || { status: 'idle', stage: '', progress: 0, message: '', error: '' })
const selectedSpeaker = computed(() => session.value?.speakers.find((row) => row.speaker_id === session.value?.selected_speaker) || null)
const selectedSegments = computed(() => {
  const rows = session.value?.segments || []
  return session.value?.selected_speaker ? rows.filter((row) => row.speaker_id === session.value?.selected_speaker) : rows
})

type Step = 1 | 2 | 3 | 4 | 5
const step = computed<Step>(() => {
  if (!session.value) return 1
  if (job.value.status === 'running') return 1
  if (!session.value.selected_speaker) return 2
  if (!activated.value) return 2
  if (training.runState.value === 'running') return 4
  if (training.step('train').status === 'done') return 5
  return 3
})

const steps = [
  { key: 1, label: '上传素材' },
  { key: 2, label: '选择说话人' },
  { key: 3, label: '校对' },
  { key: 4, label: '训练' },
  { key: 5, label: '完成' },
]

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (!rights.value) {
    toast.error('请先确认你有权将该素材用于声音训练。')
    return
  }
  busy.value = true
  try {
    const body = new FormData()
    body.append('recording', file)
    body.append('name', file.name.replace(/\.[^.]+$/, ''))
    body.append('language', uploadLanguage.value)
    body.append('rights_declared', 'true')
    body.append('transcribe', 'true')
    const data = await api.post<{ message: string; session: MediaSession }>('/api/media/sessions', body, {
      timeoutMs: UPLOAD_TIMEOUT_MS,
    })
    session.value = data.session
    startPolling(data.session.session_id)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    busy.value = false
  }
}

// 分析进度。以前是一段自己写的 setInterval：请求一失败就永久停表，
// 且没有在途保护，慢响应会覆盖更新的会话状态。现在统一走 usePolling：
// 瞬时错误重试，结束返回 false 收表，卸载时自动停下。
const sessionPoller = usePolling({
  interval: 1600,
  run: async () => {
    const sessionId = pollingSessionId.value
    if (!sessionId) return false
    const data = await api.get<{ session: MediaSession }>(
      `/api/media/sessions/${encodeURIComponent(sessionId)}`,
    )
    session.value = data.session
    // ``uploading`` is the transcoding stage that now happens on a worker; it
    // still needs polling, otherwise the UI freezes at "转码中".
    const status = data.session.job.status
    if (status === 'uploading' || status === 'running') return true
    void training.refresh()
    return false
  },
  onGiveUp: () => {
    toast.error('读取分析进度失败，请刷新页面后重试。')
  },
})

function startPolling(sessionId: string) {
  pollingSessionId.value = sessionId
  sessionPoller.start()
}

function stopPolling() {
  sessionPoller.stop()
}

async function selectSpeaker(speakerId: string) {
  if (!session.value) return
  busy.value = true
  try {
    const data = await api.post<{ session: MediaSession }>(
      `/api/media/sessions/${session.value.session_id}/select-speaker`,
      { speaker_id: speakerId },
    )
    session.value = data.session
    toast.success(`已选择 ${data.session.selected_speaker}。`)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    busy.value = false
  }
}

async function activate() {
  if (!session.value?.selected_speaker) return
  busy.value = true
  try {
    const data = await api.post<{ message: string; copied: number }>(
      `/api/media/sessions/${session.value.session_id}/activate`,
      { speaker_id: session.value.selected_speaker },
    )
    activated.value = true
    toast.success(data.message || `已写入训练工作区（${data.copied} 个切片）。`)
    await training.refresh()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    busy.value = false
  }
}

async function skipProofread() {
  await training.runAction('/api/training/transcript/skip')
}

async function scanModels() {
  // Only claim success — and only leave the page — when the backend agreed.
  if (!(await training.runAction('/api/training/models/select'))) return
  toast.success('训练产物已填入模型配置，回工作台即可用新声音生成。')
  setPage('workbench')
}

// 训练运行时轮询（同一套轮询器：失败重试、无重叠、卸载自动停）
const trainPoller = usePolling({
  interval: 3000,
  immediate: false,
  run: async () => {
    if (training.runState.value !== 'running') return true
    await training.refresh()
    return true
  },
})

function ensureTrainPolling() {
  if (!trainPoller.active()) trainPoller.start()
}

onMounted(() => {
  void training.refresh()
  ensureTrainPolling()
})

onUnmounted(() => {
  stopPolling()
  trainPoller.stop()
})
</script>

<template>
  <div class="flow" style="max-width:720px">
    <button class="backlink" type="button" @click="setPage('voices')">
      <svg class="ic sm" viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6" /></svg>返回声音库
    </button>

    <div class="page-h" style="margin-bottom:10px">
      <div>
        <h1>声音工作坊</h1>
        <p class="sub">从多人素材到训练出模，一条引导流。</p>
      </div>
    </div>

    <div class="flow-steps">
      <template v-for="(item, index) in steps" :key="item.key">
        <span class="s" :class="step > item.key ? 'done' : step === item.key ? 'cur' : ''">
          <span class="n">{{ step > item.key ? '✓' : item.key }}</span>{{ item.label }}
        </span>
        <span v-if="index < steps.length - 1" class="ln"></span>
      </template>
    </div>

    <!-- 第 1 步：上传素材 -->
    <div v-if="step === 1" class="dropzone">
      <svg class="ic" viewBox="0 0 24 24" style="width:26px;height:26px;color:var(--text3)"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" /></svg>
      <h3>上传含多人对话的视频或音频</h3>
      <p class="d">mp4 / mov / mp3 / wav / m4a · 先提取音轨，再自动说话人分离与切片<br>⚠ BGM、重叠说话会降低准确度</p>
      <label style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--text2);margin-top:10px">
        <input v-model="rights" type="checkbox">
        我确认有权将该素材用于声音训练
      </label>
      <div style="margin-top:10px">
        <button class="btn-primary" type="button" :disabled="busy" @click="fileInput?.click()">{{ busy ? '分析中…' : '选择文件' }}</button>
      </div>
      <input ref="fileInput" type="file" accept="video/*,audio/*,.mp4,.mov,.mp3,.wav,.m4a" style="display:none" @change="onFile">
      <div v-if="session && (job.status === 'running' || job.status === 'uploading')" style="margin-top:14px">
        <div style="display:flex;align-items:center;gap:9px">
          <span class="gen-progress" style="height:4px"><span :style="{ width: `${Math.round((job.progress || 0) * 100)}%` }"></span></span>
          <span class="mono" style="font-size:10.5px;color:var(--text3)">{{ job.stage || '分析中' }} {{ Math.round((job.progress || 0) * 100) }}%</span>
        </div>
      </div>
      <p v-if="job.error" style="font-size:11.5px;color:var(--err);margin-top:8px">{{ job.error }}</p>
    </div>

    <!-- 第 2 步：选择说话人 + 写入 -->
    <template v-else-if="step === 2 && session">
      <div class="panel">
        <h3>
          谁的声音？
          <span class="tag mut">{{ session.source?.filename }} · {{ Math.round(session.source?.duration_seconds || 0) }} 秒素材</span>
          <span class="tag" :class="job.status === 'done' ? 'ok' : 'mut'">{{ job.status === 'done' ? '分析完成' : job.stage }}</span>
        </h3>
        <p class="ph">自动分清了 {{ session.speakers.length }} 位说话人，选一个建声。</p>
        <div class="spk-grid">
          <button v-for="speaker in session.speakers" :key="speaker.speaker_id" class="spk-card" :class="{ on: session.selected_speaker === speaker.speaker_id }" type="button" @click="selectSpeaker(speaker.speaker_id)">
            <span class="spk-head">
              <span class="v-avatar">{{ speaker.label?.slice(-1) || speaker.speaker_id.slice(-1) }}</span>
              <b>{{ speaker.label || speaker.speaker_id }}</b>
              <span class="tag" :class="session.selected_speaker === speaker.speaker_id ? 'ok' : 'mut'">
                {{ session.selected_speaker === speaker.speaker_id ? '已选择' : `${speaker.segment_count} 段` }}
              </span>
            </span>
            <AudioPlayer v-if="speaker.sample_url" :src="speaker.sample_url" compact :title="`${speaker.label} 样本`" />
            <span class="spk-meta">{{ speaker.segment_count }} 段 · {{ Math.round(speaker.duration_seconds) }} 秒</span>
          </button>
        </div>
        <div style="display:flex;gap:8px;margin-top:12px;align-items:center">
          <button class="btn-primary" type="button" :disabled="!selectedSpeaker || busy" @click="activate">写入训练工作区</button>
          <span style="font-size:11.5px;color:var(--text3)">写入后自动完成切片/识别步骤，校对与训练在下面继续</span>
        </div>
      </div>
    </template>

    <!-- 第 3 步：校对（写入后） -->
    <template v-else-if="step === 3">
      <div class="panel">
        <h3>校对转写 <span class="tag mut">只在发现错字时修正</span></h3>
        <p class="ph">自动识别可能有个别错字；没有问题就直接跳过。</p>
        <div v-if="training.transcript.value.length" style="margin-top:6px">
          <div v-for="(row, index) in training.transcript.value.slice(0, 8)" :key="row.path" class="check-row">
            <span class="mono" style="font-size:10.5px;color:var(--text3);width:52px;flex:none">{{ row.path.split(/[\\/]/).pop()?.slice(0, 8) }}</span>
            <input v-model="row.text" style="flex:1;height:26px;border:1px solid var(--border);border-radius:5px;background:var(--surface);padding:0 8px;font:12px var(--font);color:var(--text);outline:none">
            <AudioPlayer v-if="row.audio_url" :src="row.audio_url" compact :title="row.path" />
          </div>
          <p v-if="training.transcript.value.length > 8" class="ph" style="margin-top:6px">其余 {{ training.transcript.value.length - 8 }} 句在保存后于训练工作区继续。</p>
        </div>
        <div v-else class="ph" style="margin-top:6px">转写还没加载，可先跳过或稍后重试。</div>
        <div style="display:flex;gap:8px;margin-top:12px">
          <button class="btn-primary" type="button" :disabled="training.actionBusy.value" @click="training.saveTranscript()">保存并继续</button>
          <button class="btn-secondary" type="button" :disabled="training.actionBusy.value" @click="skipProofread">跳过校对直接继续</button>
          <button class="btn-ghost" type="button" @click="training.loadTranscript()">重新加载</button>
        </div>
      </div>
      <div class="panel">
        <h3>特征提取 → 训练</h3>
        <p class="ph">实验名只允许字母、数字、下划线。</p>
        <div class="input-line" style="margin-top:9px;max-width:360px">
          <input v-model="training.experiment.value" placeholder="实验名，如 myvoice">
        </div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn-secondary" type="button" :disabled="training.actionBusy.value || !training.experimentReady.value" @click="training.runAction('/api/training/features', { experiment: training.experiment.value })">生成训练特征</button>
          <button class="btn-primary" type="button" :disabled="training.actionBusy.value || training.step('feat').status !== 'done'" @click="training.runAction('/api/training/run', { experiment: training.experiment.value, s1_epochs: training.epochs.s1_epochs, s2_epochs: training.epochs.s2_epochs })">开始训练</button>
        </div>
      </div>
    </template>

    <!-- 第 4 步：训练中 -->
    <template v-else-if="step === 4">
      <div class="panel">
        <h3>训练进行中 <span class="tag mut">{{ training.statusText(training.step('train').status) }}</span></h3>
        <div class="stat-row">
          <div><b>{{ training.stats.value?.lines ?? '—' }}</b><span>训练句数</span></div>
          <div><b>{{ training.latestMetric.value?.loss_g ? Number(training.latestMetric.value.loss_g).toFixed(3) : '—' }}</b><span>loss_g</span></div>
          <div><b>{{ training.checkpointRows.value.length }}</b><span>检查点</span></div>
        </div>
        <svg v-if="training.lossPath.value.length" class="loss" viewBox="0 0 100 40" preserveAspectRatio="none">
          <path :d="training.lossPath.value[0]" fill="none" stroke="var(--text2)" stroke-width="1" />
          <path v-if="training.lossPath.value[1]" :d="training.lossPath.value[1]" fill="none" stroke="var(--border2)" stroke-width="1" stroke-dasharray="2 2" />
        </svg>
        <pre class="logbox">{{ training.logText.value.slice(-600) }}</pre>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn-secondary" style="color:var(--err);border-color:var(--err-bd)" type="button" :disabled="training.actionBusy.value" @click="training.runAction('/api/training/stop')">停止训练</button>
          <button class="btn-ghost" type="button" @click="training.refresh()">刷新状态</button>
          <span style="align-self:center;font-size:11.5px;color:var(--text3)">可关闭页面，任务中心持续跟进</span>
        </div>
      </div>
    </template>

    <!-- 第 5 步：完成 -->
    <template v-else>
      <div class="panel">
        <h3>训练完成 <span class="tag ok">出模</span></h3>
        <p class="ph">训练产物已生成。扫描并把新模型填入引擎配置后，工作台就会用新声音合成。</p>
        <div style="display:flex;gap:8px;margin-top:12px">
          <button class="btn-primary" type="button" :disabled="training.actionBusy.value" @click="scanModels">扫描训练产物并启用</button>
          <button class="btn-secondary" type="button" @click="setPage('workbench')">先回工作台</button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.flow{max-width:720px;margin:0 auto;padding:20px 20px 50px}
.spk-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.spk-card{border:1px solid var(--border);border-radius:var(--r);padding:12px 13px;cursor:pointer;background:var(--surface);text-align:left;display:flex;flex-direction:column;gap:8px}
.spk-card.on{border-color:var(--text);box-shadow:0 0 0 1px var(--text)}
.spk-head{display:flex;align-items:center;gap:8px}
.spk-head b{font-size:13px;flex:1}
.spk-meta{font-size:11px;color:var(--text3)}
.loss{width:100%;height:72px;display:block;border:1px solid var(--border);border-radius:6px;background:var(--surface2);margin-top:10px}
.logbox{font:10.5px/1.8 var(--mono);color:var(--text2);background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 11px;max-height:140px;overflow:auto;white-space:pre-wrap;word-break:break-all;margin-top:10px}
@media (max-width:900px){.spk-grid{grid-template-columns:1fr}}
</style>
