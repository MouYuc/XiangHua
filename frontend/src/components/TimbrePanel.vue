<!-- frontend/src/components/TimbrePanel.vue -->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Activity, Download, Gauge, MicVocal, RefreshCw, Upload, Zap } from '@lucide/vue'
import { api } from '../api'
import {
  DEFAULT_TIMBRE_TOPK,
  TIMBRE_AUTO_RELEASE_NOTE,
  TIMBRE_MODEL_VRAM_GB,
  TIMBRE_PROFILE_ID,
  TIMBRE_TOPK_EXPLANATION,
  TIMBRE_TOPK_OPTIONS,
  TIMBRE_VRAM_CONFLICT_NOTE,
  asTimbreConvertResult,
  asTimbreProfileResult,
  formatTimbreBytes,
  formatTimbreClock,
  formatTimbreCount,
  formatTimbreGb,
  formatTimbreSeconds,
  isTimbreJobSettled,
  pollTimbreJob,
  timbreErrorMessage,
  timbreJobKindLabel,
  timbreJobPhaseLabel,
  timbreJobStatusLabel,
  timbreProgressPercent,
  timbreSourceOriginLabel,
  timbreVramFrom,
  timbreVramSummary,
  type TimbreJob,
  type TimbrePoller,
  type TimbreProfileStatus,
  type TimbreSource,
  type TimbreVram,
  type TimbreVramSource,
} from '../composables/timbre'

/** 个人音色转换（kNN-VC）：音色档案 → 选源 → topk → 转换 → 试听/下载 → 显存与历史。 */

const profile = ref<TimbreProfileStatus | null>(null)
const profileBusy = ref(false)
const profileError = ref('')
const sources = ref<TimbreSource[]>([])
const sourcesBusy = ref(false)
const sourcesError = ref('')
const jobs = ref<TimbreJob[]>([])
const jobsBusy = ref(false)
const jobsError = ref('')
const vram = ref<TimbreVram>(timbreVramFrom(null))
const actionError = ref('')
const notice = ref('')

const sourceMode = ref<'library' | 'upload'>('library')
const selectedSourcePath = ref('')
const uploadFile = ref<File | null>(null)
const topk = ref(DEFAULT_TIMBRE_TOPK)

const activeJob = ref<TimbreJob | null>(null)
const submitting = ref(false)
const unloadBusy = ref(false)
let poller: TimbrePoller | null = null

const selectedSource = computed(() => sources.value.find((row) => row.path === selectedSourcePath.value) || null)
const profileJob = computed(() => (activeJob.value?.kind === 'profile' ? activeJob.value : null))
const convertJob = computed(() => (activeJob.value?.kind === 'convert' ? activeJob.value : null))
const convertResult = computed(() => (convertJob.value?.status === 'done' ? asTimbreConvertResult(convertJob.value.result) : null))
const profileResult = computed(() => asTimbreProfileResult(profileJob.value?.result))
const taskRunning = computed(() => activeJob.value?.status === 'running')
const cacheReady = computed(() => Boolean(profile.value?.cache_ready))
const cacheDigest = computed(() => profile.value?.cache_digest || '')
/**
 * 后端把 `device` 缺省当成 cuda；在没有 CUDA 的机器上会直接加载失败，
 * 所以按档案里探测到的 cuda 标记显式降级到 cpu。
 */
const deviceForRequest = computed(() => (profile.value && profile.value.cuda === false ? 'cpu' : null))
const canConvert = computed(() => {
  if (submitting.value || taskRunning.value) return false
  return sourceMode.value === 'upload' ? Boolean(uploadFile.value) : Boolean(selectedSource.value)
})
const profileButtonLabel = computed(() => {
  if (profileJob.value?.status === 'running') return '建立中…'
  return cacheReady.value ? '刷新音色匹配池' : '建立音色匹配池'
})
const vramUsedPercent = computed(() => {
  const free = vram.value.free_gb
  const total = vram.value.total_gb
  if (free === null || total === null || total <= 0) return 0
  return Math.round(Math.min(100, Math.max(0, ((total - free) / total) * 100)))
})

function readError(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function setVram(source: TimbreVramSource | null | undefined) {
  vram.value = timbreVramFrom(source)
}

async function refreshProfile() {
  profileBusy.value = true
  profileError.value = ''
  try {
    const data = await api.get<TimbreProfileStatus>(`/api/timbre/profile?profile_id=${TIMBRE_PROFILE_ID}`)
    profile.value = data
    setVram(data)
  } catch (error) {
    profileError.value = readError(error)
  } finally {
    profileBusy.value = false
  }
}

async function refreshSources() {
  sourcesBusy.value = true
  sourcesError.value = ''
  try {
    const data = await api.get<{ rows: TimbreSource[] }>('/api/timbre/sources?limit=30')
    sources.value = data.rows || []
    if (!sources.value.some((row) => row.path === selectedSourcePath.value)) {
      selectedSourcePath.value = sources.value[0]?.path || ''
    }
  } catch (error) {
    sourcesError.value = readError(error)
  } finally {
    sourcesBusy.value = false
  }
}

async function refreshJobs() {
  jobsBusy.value = true
  jobsError.value = ''
  try {
    const data = await api.get<{ rows: TimbreJob[] }>('/api/timbre/jobs?limit=20')
    jobs.value = data.rows || []
    if (!activeJob.value) {
      // 切走再切回来时不丢进度：仍在跑的任务继续轮询，否则回到最近一次转换结果。
      const running = jobs.value.find((job) => job.status === 'running')
      const latest = jobs.value.find((job) => job.kind === 'convert' && job.status === 'done' && job.result?.url)
      const resume = running || latest
      if (resume) watchJob(resume)
    }
  } catch (error) {
    jobsError.value = readError(error)
  } finally {
    jobsBusy.value = false
  }
}

async function onJobSettled(job: TimbreJob | null) {
  if (job) activeJob.value = job
  if (job?.status === 'failed') {
    notice.value = ''
    actionError.value = `音色任务失败：${timbreErrorMessage(job)}`
  } else if (job) {
    actionError.value = ''
    notice.value = job.message || '音色任务已完成。'
  }
  await refreshProfile()
  await refreshJobs()
  if (job?.kind === 'convert') await refreshSources()
}

function watchJob(job: TimbreJob) {
  poller?.stop()
  poller = null
  activeJob.value = job
  if (!job?.job_id) {
    actionError.value = '音色任务没有返回 job_id，无法跟踪进度。'
    return
  }
  if (isTimbreJobSettled(job)) {
    void onJobSettled(job)
    return
  }
  poller = pollTimbreJob(
    job.job_id,
    (jobId) => api.get<TimbreJob>(`/api/timbre/status/${jobId}`),
    {
      onUpdate: (next) => { activeJob.value = next },
      onSettled: (next) => { void onJobSettled(next) },
      onError: (message) => {
        poller = null
        actionError.value = `无法继续读取任务进度：${message}`
      },
    },
  )
}

async function buildProfile() {
  if (taskRunning.value || profileJob.value?.status === 'running') return
  actionError.value = ''
  notice.value = ''
  try {
    watchJob(await api.post<TimbreJob>('/api/timbre/profile/build', {
      profile_id: TIMBRE_PROFILE_ID,
      device: deviceForRequest.value,
    }))
    notice.value = '匹配池任务已启动，正在编码参考音频。'
  } catch (error) {
    actionError.value = readError(error)
  }
}

function selectSourceMode(mode: 'library' | 'upload') {
  sourceMode.value = mode
  actionError.value = ''
}

function onPickUpload(event: Event) {
  const input = event.target as HTMLInputElement
  uploadFile.value = input.files?.[0] || null
  actionError.value = ''
}

async function startConvert() {
  if (!canConvert.value) return
  submitting.value = true
  actionError.value = ''
  notice.value = ''
  try {
    if (sourceMode.value === 'upload') {
      const file = uploadFile.value
      if (!file) return
      const body = new FormData()
      body.append('audio', file)
      body.append('topk', String(topk.value))
      body.append('profile_id', TIMBRE_PROFILE_ID)
      if (deviceForRequest.value) body.append('device', deviceForRequest.value)
      watchJob(await api.post<TimbreJob>('/api/timbre/convert', body))
      notice.value = `已提交 ${file.name}，正在转换。`
      return
    }
    const row = selectedSource.value
    if (!row) return
    watchJob(await api.post<TimbreJob>('/api/timbre/convert-file', {
      source_path: row.path,
      download_id: row.download_id,
      profile_id: TIMBRE_PROFILE_ID,
      topk: topk.value,
      device: deviceForRequest.value,
    }))
    notice.value = `已提交 ${row.label}，正在转换。`
  } catch (error) {
    actionError.value = readError(error)
  } finally {
    submitting.value = false
  }
}

async function releaseVram() {
  unloadBusy.value = true
  actionError.value = ''
  try {
    const data = await api.post<{ unloaded: boolean } & TimbreVramSource>('/api/timbre/unload')
    setVram(data)
    notice.value = data.unloaded
      ? `已释放显存，当前空闲 ${formatTimbreGb(vram.value.free_gb)}。`
      : '模型本来就没有加载，显存未被占用。'
    await refreshProfile()
  } catch (error) {
    actionError.value = readError(error)
  } finally {
    unloadBusy.value = false
  }
}

onMounted(() => {
  void refreshProfile()
  void refreshSources()
  void refreshJobs()
})

onBeforeUnmount(() => {
  poller?.stop()
  poller = null
})
</script>

<template>
  <div class="timbre-view">
    <div class="view-header">
      <h2>音色</h2>
      <p>个人音色转换 · 把配音、译制台词甚至歌声换成你自己的音色</p>
    </div>

    <p v-if="notice" class="notice" role="status" aria-live="polite">{{ notice }}</p>
    <p v-if="actionError" class="timbre-error" role="alert">{{ actionError }}</p>

    <div class="timbre-layout">
      <div class="timbre-column">
        <section class="timbre-card" aria-labelledby="timbre-profile-title">
          <header class="timbre-card-head">
            <div>
              <div id="timbre-profile-title" class="panel-section-title">音色档案</div>
              <p class="timbre-card-desc">匹配池取自训练页切片（没有切片时退回设置页的参考音频），所有转换共用这一份音色指纹。</p>
            </div>
            <button class="btn-outline" :disabled="profileBusy" @click="refreshProfile">{{ profileBusy ? '读取中…' : '刷新状态' }}</button>
          </header>

          <div v-if="profileError" class="training-fail-note" role="alert">读取音色档案失败：{{ profileError }}</div>

          <div class="timbre-data-grid">
            <div><strong>{{ formatTimbreCount(profile?.reference_files) }}</strong><small>参考文件数</small></div>
            <div><strong>{{ formatTimbreBytes(profile?.reference_bytes) }}</strong><small>参考音频体积</small></div>
            <div><strong>{{ formatTimbreCount(profile?.cached_frames) }}</strong><small>已缓存帧数</small></div>
            <div><strong>{{ formatTimbreSeconds(profile?.cached_seconds) }}</strong><small>已缓存秒数</small></div>
          </div>

          <div class="timbre-kv-list">
            <div class="timbre-kv">
              <span>缓存状态</span>
              <small :class="cacheReady ? 'timbre-ok' : 'timbre-idle'">{{ cacheReady ? '已就绪，可直接转换' : '尚未建立，首次转换会自动建立' }}</small>
            </div>
            <div class="timbre-kv">
              <span>缓存摘要</span>
              <code class="timbre-code" :title="cacheDigest">{{ cacheDigest || '—' }}</code>
            </div>
            <div class="timbre-kv">
              <span>空闲显存</span>
              <small>{{ formatTimbreGb(vram.free_gb) }} / {{ formatTimbreGb(vram.total_gb) }}</small>
            </div>
            <div class="timbre-kv">
              <span>运行设备</span>
              <small>{{ vram.cuda ? `GPU · ${vram.device}` : 'CPU（未检测到 CUDA）' }}</small>
            </div>
          </div>

          <div class="training-action-row">
            <button class="btn-primary" :disabled="taskRunning" @click="buildProfile">{{ profileButtonLabel }}</button>
            <span class="timbre-note">参考音越多越稳：论文建议 30 秒以上，超过 5 分钟收益饱和。</span>
          </div>

          <div v-if="profileJob" class="timbre-task" :class="{ running: profileJob.status === 'running' }">
            <div class="training-progress-bar"><span :style="{ width: timbreProgressPercent(profileJob) + '%' }" /></div>
            <p class="timbre-task-line">
              <span class="timbre-task-state">{{ timbreJobStatusLabel(profileJob.status) }}</span>
              · {{ timbreJobPhaseLabel(profileJob.phase) }} · {{ profileJob.message }}
            </p>
            <div v-if="profileJob.status === 'failed'" class="training-fail-note" role="alert">{{ timbreErrorMessage(profileJob) }}</div>
            <p v-else-if="profileResult" class="timbre-note">
              最新匹配池：{{ formatTimbreCount(profileResult.frames) }} 帧 / {{ formatTimbreSeconds(profileResult.seconds) }}，来自 {{ profileResult.reference_files }} 个参考文件。
            </p>
          </div>
        </section>

        <section class="timbre-card" aria-labelledby="timbre-convert-title">
          <header class="timbre-card-head">
            <div>
              <div id="timbre-convert-title" class="panel-section-title">音色转换</div>
              <p class="timbre-card-desc">源音频是一条单独的人声（合成结果、录音或清唱），长音频按 15 秒分块编码。</p>
            </div>
            <button class="btn-outline" :disabled="sourcesBusy" @click="refreshSources">{{ sourcesBusy ? '读取中…' : '刷新列表' }}</button>
          </header>

          <div class="timbre-mode-switch" role="radiogroup" aria-label="源音频来源">
            <button
              class="timbre-mode-btn" :class="{ active: sourceMode === 'library' }"
              type="button" role="radio" :aria-checked="sourceMode === 'library'"
              @click="selectSourceMode('library')"
            >最近音频</button>
            <button
              class="timbre-mode-btn" :class="{ active: sourceMode === 'upload' }"
              type="button" role="radio" :aria-checked="sourceMode === 'upload'"
              @click="selectSourceMode('upload')"
            >上传本地文件</button>
          </div>

          <template v-if="sourceMode === 'library'">
            <div v-if="sourcesError" class="training-fail-note" role="alert">读取音频列表失败：{{ sourcesError }}</div>
            <p v-if="!sources.length && !sourcesBusy" class="training-status-note">
              还没有可转换的音频：先在合成页生成一条配音，或切换到「上传本地文件」。
            </p>
            <div v-else class="timbre-source-list" role="radiogroup" aria-label="可转换的音频">
              <button
                v-for="row in sources" :key="row.path"
                class="timbre-source-row" :class="{ active: selectedSourcePath === row.path }"
                type="button" role="radio" :aria-checked="selectedSourcePath === row.path"
                :title="row.path" @click="selectedSourcePath = row.path"
              >
                <span class="timbre-source-name">{{ row.label }}</span>
                <span class="timbre-source-origin" :class="row.origin">{{ timbreSourceOriginLabel(row.origin) }}</span>
                <time class="timbre-source-meta">{{ row.created_at }}</time>
                <small class="timbre-source-meta">{{ formatTimbreBytes(row.bytes) }}</small>
              </button>
            </div>
          </template>

          <template v-else>
            <div class="timbre-upload-row">
              <label class="reference-audio-upload">
                <Upload class="tool-glyph" aria-hidden="true" />选择音频文件
                <input type="file" accept=".wav,.mp3,.m4a,.flac,.ogg,audio/*" @change="onPickUpload" />
              </label>
              <span v-if="uploadFile" class="timbre-upload-name">{{ uploadFile.name }} · {{ formatTimbreBytes(uploadFile.size) }}</span>
              <span v-else class="timbre-note">支持 wav / mp3 / m4a / flac / ogg。</span>
            </div>
          </template>

          <div class="timbre-topk" role="radiogroup" aria-label="topk 选择">
            <span class="panel-label">topk（k 近邻数量，当前 {{ topk }}）</span>
            <div class="timbre-topk-options">
              <button
                v-for="option in TIMBRE_TOPK_OPTIONS" :key="option"
                class="timbre-topk-btn" :class="{ active: topk === option }"
                type="button" role="radio" :aria-checked="topk === option"
                @click="topk = option"
              >{{ option }}</button>
              <span class="timbre-note">默认 {{ DEFAULT_TIMBRE_TOPK }}。</span>
            </div>
            <p class="timbre-note">{{ TIMBRE_TOPK_EXPLANATION }}</p>
          </div>

          <div class="timbre-actions">
            <button class="btn-primary" :disabled="!canConvert" @click="startConvert">
              <Zap class="tool-glyph" aria-hidden="true" />{{ submitting ? '提交中…' : taskRunning ? '转换中…' : '开始转换' }}
            </button>
            <span v-if="taskRunning && convertJob" class="timbre-running-badge">
              <Activity class="tool-glyph" aria-hidden="true" />{{ timbreJobPhaseLabel(convertJob.phase) }}
            </span>
          </div>
          <p class="timbre-note">{{ TIMBRE_VRAM_CONFLICT_NOTE }}</p>

          <div v-if="convertJob" class="timbre-task" :class="{ running: convertJob.status === 'running' }">
            <div class="training-progress-bar"><span :style="{ width: timbreProgressPercent(convertJob) + '%' }" /></div>
            <p class="timbre-task-line">
              <span class="timbre-task-state">{{ timbreJobStatusLabel(convertJob.status) }}</span>
              · {{ convertJob.source_name || '—' }} · topk {{ convertJob.topk || topk }} · {{ convertJob.message }}
            </p>
            <div v-if="convertJob.status === 'failed'" class="training-fail-note" role="alert">转换失败：{{ timbreErrorMessage(convertJob) }}</div>
          </div>
        </section>
      </div>

      <div class="timbre-column">
        <section class="timbre-card" aria-labelledby="timbre-result-title">
          <header class="timbre-card-head">
            <div>
              <div id="timbre-result-title" class="panel-section-title">转换结果</div>
              <p class="timbre-card-desc">转换后的音频只在本地生成，可试听、可下载。</p>
            </div>
          </header>

          <template v-if="convertResult">
            <div class="timbre-result-head">
              <MicVocal class="tool-glyph" aria-hidden="true" />
              <strong class="timbre-result-name" :title="convertResult.file_name">{{ convertResult.file_name }}</strong>
              <span class="training-step-badge ready">已完成</span>
            </div>
            <audio controls :src="convertResult.url" class="audio-player" />
            <div class="timbre-data-grid">
              <div><strong>{{ formatTimbreSeconds(convertResult.duration) }}</strong><small>音频时长</small></div>
              <div><strong>{{ formatTimbreClock(convertResult.seconds_taken) }}</strong><small>转换耗时</small></div>
              <div><strong>{{ formatTimbreCount(convertResult.matching_frames) }}</strong><small>匹配帧数</small></div>
              <div><strong>topk {{ convertResult.topk }}</strong><small>k 近邻</small></div>
            </div>
            <div class="training-action-row">
              <a class="btn-outline timbre-download" :href="convertResult.url" download><Download class="tool-glyph" aria-hidden="true" />下载转换结果</a>
            </div>
          </template>
          <p v-else-if="convertJob?.status === 'running'" class="training-status-note">转换进行中，完成后这里可以试听和下载。</p>
          <p v-else class="training-status-note">还没有转换结果。选一段音频、确认 topk，然后点击「开始转换」。</p>
        </section>

        <section class="timbre-card" aria-labelledby="timbre-vram-title">
          <header class="timbre-card-head">
            <div>
              <div id="timbre-vram-title" class="panel-section-title"><Gauge class="tool-glyph" aria-hidden="true" />显存管理</div>
              <p class="timbre-card-desc">{{ timbreVramSummary(vram) }}</p>
            </div>
            <button class="btn-outline" :disabled="unloadBusy" @click="releaseVram">{{ unloadBusy ? '释放中…' : '释放显存' }}</button>
          </header>

          <div class="timbre-vram-bar" role="img" :aria-label="`已占用显存约 ${vramUsedPercent}%`">
            <span :style="{ width: vramUsedPercent + '%' }" />
          </div>
          <div class="timbre-kv-list">
            <div class="timbre-kv"><span>空闲显存</span><small>{{ formatTimbreGb(vram.free_gb) }}</small></div>
            <div class="timbre-kv"><span>显存总量</span><small>{{ formatTimbreGb(vram.total_gb) }}</small></div>
            <div class="timbre-kv"><span>模型占用</span><small>约 {{ TIMBRE_MODEL_VRAM_GB }} GB（实测 6.94 GB → 3.99 GB）</small></div>
          </div>
          <p class="timbre-note">{{ TIMBRE_AUTO_RELEASE_NOTE }}</p>
          <p class="timbre-note">合成页需要 GPU 时也可以先在这里点「释放显存」。</p>
        </section>

        <section class="timbre-card" aria-labelledby="timbre-history-title">
          <header class="timbre-card-head">
            <div>
              <div id="timbre-history-title" class="panel-section-title">最近音色任务</div>
              <p class="timbre-card-desc">最近 20 条音色转换与匹配池任务的进度。</p>
            </div>
            <button class="btn-outline" :disabled="jobsBusy" @click="refreshJobs">{{ jobsBusy ? '读取中…' : '刷新' }}</button>
          </header>

          <div v-if="jobsError" class="training-fail-note" role="alert">读取任务历史失败：{{ jobsError }}</div>
          <p v-if="!jobs.length" class="training-status-note">还没有音色任务。</p>
          <div v-else class="timbre-history-list">
            <div v-for="job in jobs" :key="job.job_id" class="timbre-history-row" :class="job.status">
              <span class="timbre-history-state">{{ timbreJobStatusLabel(job.status) }}</span>
              <span class="timbre-history-name" :title="job.source_name || job.job_id">
                {{ timbreJobKindLabel(job.kind) }}<template v-if="job.source_name"> · {{ job.source_name }}</template>
              </span>
              <span class="timbre-history-message" :title="timbreErrorMessage(job)">{{ job.status === 'failed' ? timbreErrorMessage(job) : job.message }}</span>
              <time class="timbre-history-meta">{{ job.updated_at || job.started_at }}</time>
            </div>
          </div>
        </section>

        <section class="timbre-card">
          <div class="panel-section-title">说明</div>
          <div class="timbre-kv-list">
            <div class="timbre-kv"><span>音色档案 ID</span><small>{{ TIMBRE_PROFILE_ID }}</small></div>
            <div class="timbre-kv"><span>缓存摘要变化</span><small>新增或重录参考音频后摘要会变，匹配池会自动重建。</small></div>
            <div class="timbre-kv"><span>分块窗口</span><small>15 秒 / 块（WavLM 全注意力，长音频需要分块，否则会爆显存）。</small></div>
          </div>
          <p class="timbre-note"><RefreshCw class="tool-glyph" aria-hidden="true" />页面上的进度由后端任务快照轮询得到，切换页面不会中断后端任务。</p>
        </section>
      </div>
    </div>
  </div>
</template>