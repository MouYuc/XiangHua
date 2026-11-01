<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { activateVoiceCard, api, errorMessage, type VoiceCard } from '@/lib/api'
import {
  asTimbreConvertResult,
  formatTimbreSeconds,
  TIMBRE_TOPK_EXPLANATION,
  TIMBRE_TOPK_OPTIONS,
  timbreErrorMessage,
} from '@/composables/timbre'
import type { TimbreJob, TimbreSource } from '@/composables/timbre'
import { usePage } from '@/composables/usePage'
import { useVoices } from '@/composables/useVoices'
import { useConfig } from '@/composables/useConfig'
import { useToast } from '@/composables/useToast'
import { usePolling } from '@/composables/usePolling'
import AudioPlayer from '@/components/AudioPlayer.vue'
import WaveForm from '@/components/WaveForm.vue'

defineOptions({ name: 'VoicesView' })

const { setPage } = usePage()
const { profiles, cards, loadVoiceOptions, voiceCount } = useVoices()
const { config, loadConfig } = useConfig()
const toast = useToast()

// —— 声音列表：声线卡（可命名建档）+ 内置当前音色 ——
type VoiceEntry = {
  key: string
  name: string
  sub: string
  kind: 'card' | 'system'
  tone: 'ok' | 'warn' | 'mut'
  note: string
  voiceId: string
  versionLabel: string
}

const search = ref('')
const filter = ref<'all' | 'card' | 'system'>('all')
const activating = ref('')

const entries = computed<VoiceEntry[]>(() => {
  const list: VoiceEntry[] = []
  for (const card of cards.value) {
    const retired = card.status !== 'active'
    list.push({
      key: card.voice_id,
      name: card.display_name,
      sub: `声线卡 · ${card.language} · ${card.updated_at?.slice(0, 10) || ''}`,
      kind: 'card',
      tone: retired ? 'mut' : card.timbre_available ? 'ok' : 'warn',
      note: retired ? '已停用' : card.timbre_available ? `可用 · v${card.active_version}` : '参考音缺失',
      voiceId: card.voice_id,
      versionLabel: `${card.version_count} 个版本 · ${card.updated_at?.slice(0, 10) || ''}`,
    })
  }
  const current = profiles.value.find((item) => item.id === 'current')
  if (current) {
    list.push({
      key: 'current',
      name: current.display_name,
      sub: '内置 · 跟随当前参考音',
      kind: 'system',
      tone: config.ref_audio ? 'ok' : 'warn',
      note: config.ref_audio ? '参考音已配对' : '未配置参考音',
      voiceId: 'current',
      versionLabel: '',
    })
  }
  return list
})

const filtered = computed(() => entries.value.filter((entry) => {
  if (filter.value !== 'all' && entry.kind !== filter.value) return false
  const keyword = search.value.trim().toLowerCase()
  if (!keyword) return true
  return `${entry.name}${entry.sub}`.toLowerCase().includes(keyword)
}))

function useVoice(entry: VoiceEntry) {
  if (entry.kind === 'system') {
    config.voice_profile_id = 'current'
    toast.success('已切换当前音色，回工作台即可生成。')
    setPage('workbench')
    return
  }
  void activateCard(entry)
}

async function activateCard(entry: VoiceEntry) {
  activating.value = entry.voiceId
  try {
    const data = await activateVoiceCard(entry.voiceId)
    await loadConfig()
    await loadVoiceOptions()
    config.voice_profile_id = 'current'
    toast.success(data.message)
    setPage('workbench')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    activating.value = ''
  }
}

// —— 新建声音 ——
const createOpen = ref(false)

// —— 音色转换（kNN-VC）——
const toolOpen = ref(false)
const sources = ref<TimbreSource[]>([])
const sourcePath = ref('')
const topk = ref(4)
const converting = ref(false)
const convertProgress = ref(0)
const convertStage = ref('')
const resultUrl = ref('')
const resultName = ref('')
const resultError = ref('')

async function loadSources() {
  try {
    const data = await api.get<{ rows?: TimbreSource[] }>('/api/timbre/sources?limit=8')
    sources.value = data.rows ?? []
    if (!sourcePath.value && sources.value[0]) sourcePath.value = sources.value[0].path
  } catch {
    sources.value = []
  }
}

function onConvertFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  void convertUpload(file)
  input.value = ''
}

async function convertUpload(file: File) {
  resetConvert()
  converting.value = true
  convertStage.value = '上传源音频'
  try {
    const body = new FormData()
    body.append('audio', file)
    body.append('topk', String(topk.value))
    const started = await api.post<{ job_id: string }>('/api/timbre/convert', body)
    pollConvert(started.job_id)
  } catch (error) {
    converting.value = false
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

async function convertSelected() {
  if (!sourcePath.value) {
    toast.error('先选择一段源音频，或直接上传。')
    return
  }
  resetConvert()
  converting.value = true
  convertStage.value = '提交转换'
  try {
    const started = await api.post<{ job_id: string }>('/api/timbre/convert-file', {
      source_path: sourcePath.value,
      topk: topk.value,
    })
    pollConvert(started.job_id)
  } catch (error) {
    converting.value = false
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

function resetConvert() {
  resultUrl.value = ''
  resultName.value = ''
  resultError.value = ''
  convertProgress.value = 0
}

// 音色转换进度。旧实现用 setTimeout 自链，任何一次请求失败就永久停在
// 错误态（服务端任务其实还在跑）。usePolling 会重试瞬时错误、避免请求重叠，
// 并在组件卸载时自动停下。
const convertJobId = ref('')
const convertPoller = usePolling({
  interval: 1200,
  run: async () => {
    const jobId = convertJobId.value
    if (!jobId) return false
    const job = await api.get<TimbreJob>(`/api/timbre/status/${encodeURIComponent(jobId)}`)
    convertProgress.value = Math.round(Math.min(1, Math.max(0, Number(job.progress) || 0)) * 100)
    convertStage.value = job.phase === 'encode' ? '编码参考音并转换' : job.message || '处理中'
    if (job.status === 'done') {
      const result = asTimbreConvertResult(job.result)
      if (result?.url) {
        resultUrl.value = result.url
        resultName.value = result.file_name
        toast.success(`转换完成，用时 ${formatTimbreSeconds(result.seconds_taken)}。`)
      } else {
        resultError.value = '转换完成但未返回音频。'
      }
      converting.value = false
      return false
    }
    if (job.status === 'failed') {
      resultError.value = timbreErrorMessage(job) || job.error || '转换失败。'
      converting.value = false
      return false
    }
    return true
  },
  onGiveUp: (error: unknown) => {
    resultError.value = errorMessage(error) || '无法读取转换进度，请稍后重试。'
    converting.value = false
  },
})

function pollConvert(jobId: string) {
  convertJobId.value = jobId
  convertPoller.start()
}

async function releaseVram() {
  try {
    await api.post('/api/timbre/unload')
    toast.success('已释放音色转换显存。')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

onMounted(() => {
  void loadVoiceOptions()
  void loadSources()
})

onUnmounted(() => {
  convertPoller.stop()
})
</script>

<template>
  <div class="page-wrap">
    <div class="page-h">
      <div>
        <h1>声音库</h1>
        <p class="sub">所有可用于合成的声音 —— 一段自然说话录音即可建档。</p>
      </div>
      <button class="btn-primary" type="button" @click="createOpen = true">
        <svg class="ic sm" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>新建声音
      </button>
    </div>

    <div class="toolbar">
      <label class="search">
        <svg class="ic sm" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>
        <input v-model="search" placeholder="搜索声音…">
      </label>
      <button class="filter-chip" :class="{ on: filter === 'all' }" type="button" @click="filter = 'all'">全部 · {{ voiceCount + 1 }}</button>
      <button class="filter-chip" :class="{ on: filter === 'card' }" type="button" @click="filter = 'card'">声线卡 · {{ voiceCount }}</button>
      <button class="filter-chip" :class="{ on: filter === 'system' }" type="button" @click="filter = 'system'">系统 · 1</button>
    </div>

    <div class="vgrid">
      <div v-for="entry in filtered" :key="entry.key" class="vcard">
        <div class="top">
          <span class="v-avatar" :class="{ dk: entry.kind === 'card' }">{{ entry.name.slice(0, 1) }}</span>
          <span class="grow">
            <b>{{ entry.name }}</b>
            <span>{{ entry.sub }}</span>
          </span>
          <span class="tag" :class="entry.tone">{{ entry.note }}</span>
        </div>
        <WaveForm :seed="entry.key" :bars="30" :height="22" />
        <div v-if="entry.versionLabel" class="meta">
          <span class="mono">{{ entry.versionLabel }}</span>
        </div>
        <div class="acts">
          <button class="mini dark" type="button" :disabled="activating === entry.voiceId || entry.tone === 'mut' && entry.kind === 'card'" @click="useVoice(entry)">
            {{ activating === entry.voiceId ? '激活中…' : '用于合成' }}
          </button>
        </div>
      </div>
      <div v-if="!filtered.length" class="empty-card">没有匹配的声音。</div>
    </div>

    <div class="tool-duo">
      <div class="tool-card">
        <h3>
          <svg class="ic sm" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20a6.5 6.5 0 0 1 13 0" /><circle cx="17.5" cy="9.5" r="2.5" /><path d="M15.5 14.5a5 5 0 0 1 6 4.9" /></svg>
          声音工作坊 <span class="tag mut">进阶</span>
        </h3>
        <p>多人素材分离 → 校对 → 训练微调，从「采集」到「出模」的一条引导流，不再分散在多个页面。</p>
        <div style="margin-top:10px"><button class="btn-secondary" type="button" @click="setPage('workshop')">打开工作坊 →</button></div>
      </div>
      <div class="tool-card">
        <h3>
          <svg class="ic sm" viewBox="0 0 24 24"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.2 2.2M16.2 16.2l2.2 2.2M5.6 18.4l2.2-2.2M16.2 7.8l2.2-2.2" /><circle cx="12" cy="12" r="3.5" /></svg>
          音色转换 <span class="tag mut">kNN-VC</span>
        </h3>
        <p>把任意已配音的人声素材转成当前声音 —— 适合翻唱、歌词或替换素材重新定音。{{ TIMBRE_TOPK_EXPLANATION }}</p>
        <div style="margin-top:10px">
          <button class="btn-secondary" type="button" @click="toolOpen = !toolOpen">{{ toolOpen ? '收起工具' : '打开转换工具 →' }}</button>
        </div>
        <div v-if="toolOpen" class="tool-body">
          <label class="tool-field">
            <span class="k">源音频</span>
            <select v-model="sourcePath">
              <option value="" disabled>选择最近素材…</option>
              <option v-for="source in sources" :key="source.path" :value="source.path">{{ source.label }}（{{ source.origin === 'synthesis' ? '合成记录' : '上传' }}）</option>
            </select>
          </label>
          <label class="tool-field">
            <span class="k">或上传文件</span>
            <input type="file" accept="audio/*" @change="onConvertFile">
          </label>
          <div class="tool-field">
            <span class="k">topk</span>
            <div class="seg" style="flex:1">
              <button v-for="option in TIMBRE_TOPK_OPTIONS" :key="option" type="button" :class="{ on: topk === option }" @click="topk = option">{{ option }}</button>
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <button class="btn-primary" type="button" :disabled="converting" @click="convertSelected">{{ converting ? `转换中 ${convertProgress}%` : '开始转换' }}</button>
            <button class="mini" type="button" @click="releaseVram">释放显存</button>
          </div>
          <div v-if="converting" style="display:flex;align-items:center;gap:8px;margin-top:4px">
            <span class="gen-progress" style="height:4px"><span :style="{ width: `${convertProgress}%` }"></span></span>
            <span class="mono" style="font-size:10.5px;color:var(--text3)">{{ convertStage }}</span>
          </div>
          <p v-if="resultError" style="font-size:11.5px;color:var(--err);margin-top:6px">{{ resultError }}</p>
          <div v-if="resultUrl" style="margin-top:8px">
            <AudioPlayer :src="resultUrl" :title="resultName" />
            <a class="mini" style="margin-left:8px" :href="resultUrl" :download="resultName">下载</a>
          </div>
        </div>
      </div>
    </div>

    <!-- 新建声音 -->
    <div v-if="createOpen" class="overlay" @click.self="createOpen = false">
      <div class="modal" role="dialog" aria-label="新建声音">
        <div class="mh">
          <h2>新建声音</h2>
          <p>选一种来源，之后全部自动处理，你只在关键结果处确认。</p>
        </div>
        <div class="mb">
          <button class="opt" type="button" @click="setPage('wizard'); createOpen = false">
            <span class="oi"><svg class="ic sm" viewBox="0 0 24 24"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /></svg></span>
            <span><b>一段自然说话录音</b><span class="d">访谈、播客、语音备忘都行 —— 自动切片、质检、转写，确认后存为一张声线卡。</span></span>
            <svg class="ic sm go" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" /></svg>
          </button>
          <button class="opt" type="button" @click="setPage('workshop'); createOpen = false">
            <span class="oi"><svg class="ic sm" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20a6.5 6.5 0 0 1 13 0" /><circle cx="17.5" cy="9.5" r="2.5" /><path d="M15.5 14.5a5 5 0 0 1 6 4.9" /></svg></span>
            <span><b>多人素材，分离某人声音</b><span class="d">会议、剧里有多个说话人？自动分清谁在何时说话，选一个人建声并训练。</span></span>
            <svg class="ic sm go" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" /></svg>
          </button>
          <button class="opt" type="button" disabled title="训练完成后在下面点击「扫描训练产物并启用」">
            <span class="oi"><svg class="ic sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" /></svg></span>
            <span><b>导入已训练的模型</b><span class="d">训练完成后在工作坊「完成」一步扫描启用；暂不支持手动导入。</span></span>
            <svg class="ic sm go" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" /></svg>
          </button>
        </div>
        <div class="mf">
          <span>所有方式都要求素材来源合法合规</span>
          <button style="color:var(--text2)" @click="createOpen = false">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.vgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.vcard{border:1px solid var(--border);border-radius:var(--r);background:var(--surface);padding:12px 13px;display:flex;flex-direction:column;gap:9px}
.vcard:hover{border-color:var(--border2)}
.vcard .top{display:flex;align-items:center;gap:9px}
.vcard .top .grow{flex:1;min-width:0}
.vcard .top b{font-size:13px;font-weight:600;display:block}
.vcard .top span{font-size:11px;color:var(--text3)}
.vcard .meta{font-size:10.5px;color:var(--text3)}
.vcard .acts{display:flex;gap:5px;align-items:center}
.empty-card{border:1px dashed var(--border2);border-radius:var(--r);padding:26px;text-align:center;color:var(--text3);font-size:12.5px}
.tool-duo{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.tool-card{border:1px solid var(--border);border-radius:var(--r);background:var(--surface);padding:14px 15px}
.tool-card h3{font-size:13px;font-weight:600;display:flex;align-items:center;gap:7px}
.tool-card p{font-size:11.5px;color:var(--text3);margin-top:4px;line-height:1.6}
.tool-body{border-top:1px solid var(--border);margin-top:11px;padding-top:11px;display:flex;flex-direction:column;gap:8px}
.tool-field{display:flex;align-items:center;gap:8px;font-size:12px}
.tool-field .k{width:60px;color:var(--text2);flex:none}
.tool-field select,.tool-field input[type="file"]{flex:1;height:30px;border:1px solid var(--border);border-radius:var(--rs);background:var(--surface);padding:0 9px;font:inherit;font-size:12px;color:var(--text)}
@media (max-width:900px){.vgrid{grid-template-columns:1fr 1fr}.tool-duo{grid-template-columns:1fr}}
</style>
