<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, UPLOAD_TIMEOUT_MS } from '@/lib/api'
import { usePage } from '@/composables/usePage'
import { useTraining } from '@/composables/useTraining'
import { useToast } from '@/composables/useToast'
import { usePolling } from '@/composables/usePolling'
import AudioPlayer from '@/components/AudioPlayer.vue'
import LossChart from '@/components/LossChart.vue'

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
  created_at?: string
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
  // 训练失败时留在训练步并给出失败原因与日志，绝不默默踢回校对步；
  // 其余状态（含 activate 重置后的 pending/未开始）一律回第 3 步走正常流程
  if (training.step('train').status === 'fail') return 4
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
  // 支持一次选择多个音频文件：同一会话内逐个转码再拼接成一个音源
  const files = [...(input.files ?? [])]
  input.value = ''
  if (!files.length) return
  if (!rights.value) {
    toast.error('请先确认你有权将该素材用于声音训练。')
    return
  }
  busy.value = true
  try {
    const body = new FormData()
    for (const file of files) body.append('recording', file)
    body.append('name', files.length > 1 ? `${files[0]!.name.replace(/\.[^.]+$/, '')} 等 ${files.length} 个文件` : files[0]!.name.replace(/\.[^.]+$/, ''))
    body.append('language', uploadLanguage.value)
    body.append('rights_declared', 'true')
    body.append('transcribe', 'true')
    const data = await api.post<{ message: string; session: MediaSession }>('/api/media/sessions', body, {
      timeoutMs: UPLOAD_TIMEOUT_MS,
    })
    session.value = data.session
    startPolling(data.session.session_id)
    toast.success(data.message)
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
    // 写入完成后立刻把转写装进内存：第 3 步的「保存并继续」以它为准，
    // 不加载就点保存会把 corrected 清单覆盖成空（曾导致特征提取拿到 0 句）。
    await training.loadTranscript()
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

// —— 训练运行时轮询：任何训练类任务（特征提取/训练）在跑就持续拉取，
// 状态跳变（如特征提取完成 → 开始训练点亮）不再错过。页面卸载自动停。
// 注意契约：run 返回 false = 停止轮询；这里什么都不返回（= 持续轮询）。
const trainPoller = usePolling({
  interval: 3000,
  immediate: false,
  run: async () => {
    await training.refresh()
  },
})

function ensureTrainPolling() {
  if (!trainPoller.active()) trainPoller.start()
}

// 多文件会话的源文件名是「a.mp3 + b.mp3 + …」全量拼接，超长会撑爆标题行。
// 展示层收敛：前两个 + 「共 N 个」；完整名单保留在 title 悬停里。
const sourceDisplay = computed(() => {
  const name = session.value?.source?.filename || ''
  const parts = name.split(' + ')
  if (parts.length <= 3) return name
  return `${parts[0]} + ${parts[1]} + … 共 ${parts.length} 个文件`
})

// 校对自动保存：编辑后 800ms 防抖静默保存，刷新/误关不再丢稿。
// 行为对齐配置抽屉的 650ms 防抖模式；空清单交给后端守卫拒绝。
let transcriptSaveTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => training.transcript.value.map((row) => row.text),
  () => {
    if (!training.transcriptLoaded.value || training.actionBusy.value) return
    if (!training.transcript.value.length) return
    if (transcriptSaveTimer) clearTimeout(transcriptSaveTimer)
    transcriptSaveTimer = setTimeout(() => {
      transcriptSaveTimer = null
      void training.saveTranscript(true)
    }, 800)
  },
  { deep: true },
)

onMounted(() => {
  void training.refresh()
  ensureTrainPolling()
  // 刷新后恢复：校对步骤已到过 done 而内存里没有转写时，自动重新加载。
  if (training.step('correct').status === 'done' && !training.transcriptLoaded.value) {
    void training.loadTranscript()
  }
  // 从任务中心/别的页面跳转过来时恢复采集会话：优先正在转码/分析的会话，
  // 否则恢复最近的一个——否则这里永远是第 1 步，看不到任何任务进度。
  void restoreSession()
})

async function restoreSession() {
  if (session.value) return
  try {
    const data = await api.get<{ rows: MediaSession[] }>('/api/media/sessions')
    const rows = [...(data.rows || [])].sort((a, b) =>
      String(b.created_at || '').localeCompare(String(a.created_at || '')),
    )
    if (!rows.length) return
    const active = rows.find((row) => ['uploading', 'running'].includes(String(row.job?.status || ''))) || rows[0]
    const full = await api.get<{ session: MediaSession }>(
      `/api/media/sessions/${encodeURIComponent(active.session_id)}`,
    )
    session.value = full.session
    if (full.session.selected_speaker) activated.value = true
    if (['uploading', 'running'].includes(String(full.session.job?.status || ''))) {
      startPolling(active.session_id)
    }
  } catch {
    // 会话恢复失败按「新上传」处理，不打扰用户
  }
}

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
        <span class="lbl pg-title">声音工作坊</span>
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
      <p class="d">mp4 / mov / mp3 / wav / m4a · 可一次选择多个文件，自动合并为一个音源<br>先提取音轨，再自动说话人分离与切片<br>⚠ BGM、重叠说话会降低准确度</p>
      <label style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--text2);margin-top:10px">
        <input v-model="rights" type="checkbox">
        我确认有权将该素材用于声音训练
      </label>
      <div style="margin-top:10px">
        <button class="btn-primary" type="button" :disabled="busy" @click="fileInput?.click()">{{ busy ? '分析中…' : '选择文件（可多选）' }}</button>
      </div>
      <input ref="fileInput" type="file" multiple accept="video/*,audio/*,.mp4,.mov,.mp3,.wav,.m4a" style="display:none" @change="onFile">
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
          <span class="tag mut" :title="session.source?.filename">{{ sourceDisplay }} · {{ Math.round(session.source?.duration_seconds || 0) }} 秒素材</span>
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
            <input v-model="row.text" style="flex:1;height:26px;border:1px solid var(--border);border-radius:var(--rs);background:var(--surface);padding:0 8px;font:12px var(--font);color:var(--text);outline:none">
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
        <h3>训练进行中 <span class="tag" :class="training.step('train').status === 'fail' ? 'err' : 'mut'">{{ training.statusText(training.step('train').status) }}</span></h3>
        <div v-if="training.step('train').status === 'fail'" class="fail-note">
          <b>训练失败。</b> 常见原因在下方日志末尾（Traceback 的最后几行）。修正后可点「重新开始训练」重试。
        </div>
        <div class="stat-row">
          <div><b>{{ training.stats.value?.lines ?? '—' }}</b><span>训练句数</span></div>
          <div><b>{{ training.latestMetric.value?.loss_g ? Number(training.latestMetric.value.loss_g).toFixed(3) : '—' }}</b><span>loss_g</span></div>
          <div><b>{{ training.checkpointRows.value.length }}</b><span>检查点</span></div>
        </div>
        <LossChart v-if="training.metricRows.value.length" :rows="training.metricRows.value" />
        <pre class="logbox">{{ training.logText.value.slice(-600) }}</pre>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button v-if="training.step('train').status === 'running'" class="btn-secondary" style="color:var(--err);border-color:var(--err-bd)" type="button" :disabled="training.actionBusy.value" @click="training.runAction('/api/training/stop')">停止训练</button>
          <button v-else class="btn-primary" type="button" :disabled="training.actionBusy.value || !training.experimentReady.value" @click="training.runAction('/api/training/run', { experiment: training.experiment.value, s1_epochs: training.epochs.s1_epochs, s2_epochs: training.epochs.s2_epochs })">{{ training.step('train').status === 'fail' ? '重新开始训练' : '开始训练' }}</button>
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
        <div class="stat-row">
          <div><b>{{ training.stats.value?.lines ?? '—' }}</b><span>训练句数</span></div>
          <div><b>{{ training.latestMetric.value?.loss_g ? Number(training.latestMetric.value.loss_g).toFixed(3) : '—' }}</b><span>loss_g</span></div>
          <div><b>{{ training.checkpointRows.value.length }}</b><span>检查点</span></div>
        </div>
        <LossChart v-if="training.metricRows.value.length" :rows="training.metricRows.value" />
        <p v-else class="ph" style="margin-top:8px">这一轮还没有可绘制的 loss 曲线（点「刷新状态」重取）。</p>
        <div style="display:flex;gap:8px;margin-top:12px">
          <button class="btn-primary" type="button" :disabled="training.actionBusy.value" @click="scanModels">扫描训练产物并启用</button>
          <button class="btn-secondary" type="button" @click="setPage('workbench')">先回工作台</button>
          <button class="btn-ghost" type="button" @click="training.refresh()">刷新状态</button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.flow{max-width:720px;margin:0 auto;padding:20px 20px 50px}
/* 皮肤对齐 design/D-focus.html：标题与分区标题用 .lbl（mono 11px），卡片直角 2px。 */
.pg-title{font-size:12px;color:var(--text2);display:block;padding-bottom:2px}
.page-h .sub{font-family:var(--mono);font-size:11px;color:var(--text3);letter-spacing:.04em;margin-top:3px}
.panel h3{font-family:var(--mono);font-size:11px;font-weight:400;letter-spacing:.14em;color:var(--text3)}
.panel h3 .tag{letter-spacing:normal}
.stat-row{display:flex;gap:26px;margin-top:10px;font-size:12px;color:var(--text3)}
.stat-row b{display:block;font-family:var(--mono);font-size:15px;color:var(--text);font-variant-numeric:tabular-nums}
.stat-row span{font-size:11px}
.fail-note{
  margin-top:10px;padding:8px 10px;font-size:12px;line-height:1.6;color:var(--err);
  background:var(--err-bg);border:1px solid var(--err-bd);border-radius:var(--r);
}
.fail-note b{font-weight:600}
.spk-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.spk-card{border:1px solid var(--border);border-radius:var(--r);padding:12px 13px;cursor:pointer;background:var(--surface);text-align:left;display:flex;flex-direction:column;gap:8px}
.spk-card:hover{border-color:var(--border2)}
.spk-card.on{border-color:var(--text)}
.spk-head{display:flex;align-items:center;gap:8px}
.spk-head b{font-size:13px;flex:1}
.spk-meta{font-size:11px;color:var(--text3)}
.loss{width:100%;height:72px;display:block;border:1px solid var(--border);border-radius:var(--r);background:var(--surface2);margin-top:10px}
.logbox{font:10.5px/1.8 var(--mono);color:var(--text2);background:var(--surface2);border:1px solid var(--border);border-radius:var(--r);padding:8px 11px;max-height:140px;overflow:auto;white-space:pre-wrap;word-break:break-all;margin-top:10px}
@media (max-width:900px){.spk-grid{grid-template-columns:1fr}}
</style>
