<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { activateVoiceCard, api, deleteVoiceCard, errorMessage, patchVoiceCard, UPLOAD_TIMEOUT_MS, type VoiceCard } from '@/lib/api'
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

defineOptions({ name: 'VoicesView' })

const { setPage } = usePage()
const { profiles, cards, loadVoiceOptions, voiceCount } = useVoices()
const { config, loadConfig } = useConfig()
const toast = useToast()

// —— 声音列表：声线卡（可命名建档）+ 内置当前音色（虚线 ghost 卡） ——
type CardEntry = {
  key: string
  card: VoiceCard
  name: string
  retired: boolean
  current: boolean
  refName: string
  licenseLabel: string
  searchText: string
}
type SystemEntry = { key: 'current'; searchText: string }
type Entry = { kind: 'card'; data: CardEntry } | { kind: 'system'; data: SystemEntry }

const search = ref('')
const filter = ref<'all' | 'card' | 'system'>('all')
const activating = ref('')
const patching = ref('')
/** 每张卡的展开状态：合并后的详情面板（基础信息 + 版本记录）。 */
const expanded = ref<Record<string, '' | 'detail'>>({})

function normPath(value?: string | null) {
  return (value || '').replace(/\\/g, '/').toLowerCase()
}

const cardEntries = computed<CardEntry[]>(() =>
  cards.value.map((card) => {
    const refAudio = card.active_timbre?.ref_audio || ''
    // 「正在用于合成」优先看配置里登记的 voice_card_id（后端删除守卫同源）；
    // 配置引用的是已删除的卡或未登记时，才退回参考音路径匹配。
    const referenced = config.voice_card_id && config.voice_card_id === card.voice_id
    const refMatch = Boolean(config.ref_audio) && normPath(refAudio) === normPath(config.ref_audio)
    const current = Boolean(referenced || (!config.voice_card_id && refMatch))
    return {
      key: card.voice_id,
      card,
      name: card.display_name,
      retired: card.status !== 'active',
      current,
      refName: refAudio.split(/[\\/]/).pop() || '',
      licenseLabel: card.license_id || '未关联（预留）',
      searchText: `${card.display_name} ${card.language} ${card.license_id || ''}`.toLowerCase(),
    }
  }),
)

const systemEntry = computed<SystemEntry | null>(() => {
  const current = profiles.value.find((item) => item.id === 'current')
  if (!current) return null
  return {
    key: 'current',
    searchText: `${current.display_name} 当前音色 内置`.toLowerCase(),
  }
})

const entries = computed<Entry[]>(() => {
  const list: Entry[] = cardEntries.value.map((data) => ({ kind: 'card' as const, data }))
  if (systemEntry.value) list.push({ kind: 'system', data: systemEntry.value })
  return list
})

const filtered = computed(() => entries.value.filter((entry) => {
  if (filter.value !== 'all' && entry.kind !== filter.value) return false
  const keyword = search.value.trim().toLowerCase()
  if (!keyword) return true
  return entry.data.searchText.includes(keyword)
}))

function toggleExpand(voiceId: string) {
  expanded.value = { ...expanded.value, [voiceId]: expanded.value[voiceId] ? '' : 'detail' }
}

function useVoice(entry: Entry) {
  if (entry.kind === 'system') {
    // 切回内置「当前音色」时清掉卡片引用：否则 voice_card_id 悬空，
    // 「当前使用」高亮与删除守卫都会引用一张不存在的卡。
    config.voice_card_id = ''
    config.voice_profile_id = 'current'
    toast.success('已切换当前音色，回工作台即可生成。')
    setPage('workbench')
    return
  }
  void activateCard(entry.data)
}

async function activateCard(entry: CardEntry) {
  activating.value = entry.key
  try {
    const data = await activateVoiceCard(entry.key)
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

// —— 0e 遗留补齐：停用 / 恢复（PATCH /api/voice-cards/{id}） ——
async function toggleCardStatus(entry: CardEntry) {
  console.debug('[xianghua] toggle status click:', entry.key, '→', entry.retired ? 'active' : 'retired')
  patching.value = entry.key
  try {
    const data = await patchVoiceCard(entry.key, { status: entry.retired ? 'active' : 'retired' })
    await loadVoiceOptions()
    toast.success(data.message)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    patching.value = ''
  }
}

// —— 删除声线卡：两步确认（与「清空记录」同款交互），永久删除卡片目录 ——
const deleteConfirm = ref('')
const deleting = ref('')
async function deleteCard(entry: CardEntry) {
  console.debug('[xianghua] delete click:', entry.key, entry.name,
    '| confirm state:', deleteConfirm.value, '| current:', entry.current)
  if (deleteConfirm.value !== entry.key) {
    deleteConfirm.value = entry.key
    toast.error('删除不可恢复。再点一次「删除」确认；取消就点别处或「详情」。')
    console.debug('[xianghua] delete → awaiting second confirm for', entry.key)
    return
  }
  deleteConfirm.value = ''
  deleting.value = entry.key
  try {
    const data = await deleteVoiceCard(entry.key)
    console.debug('[xianghua] delete ok:', entry.key, data.message)
    await loadVoiceOptions()
    toast.success(data.message)
  } catch (error) {
    console.error('[xianghua] delete failed:', entry.key, error)
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    deleting.value = ''
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
    const started = await api.post<{ job_id: string }>('/api/timbre/convert', body, {
      timeoutMs: UPLOAD_TIMEOUT_MS,
    })
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
    <div class="h-row">
      <span class="lbl">声音库</span>
      <span class="h-count">voices · {{ voiceCount + 1 }} 项</span>
      <span class="r">
        <button class="btn-primary" type="button" @click="createOpen = true">
          <svg class="ic sm" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>新建声音
        </button>
      </span>
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

    <div class="vc-grid">
      <template v-for="entry in filtered" :key="entry.data.key">
        <!-- 声线卡 -->
        <div v-if="entry.kind === 'card'" class="vc" :class="{ cur: entry.data.current, off: entry.data.retired }">
          <div class="vc-head">
            <b>{{ entry.data.name }}</b>
            <span class="ver">v{{ entry.data.card.active_version }}</span>
            <span v-if="entry.data.retired" class="tag mut">已停用</span>
            <span v-else-if="entry.data.card.timbre_available" class="tag ink">可用</span>
            <span v-else class="tag warn">参考音缺失</span>
          </div>

          <div v-if="expanded[entry.data.key] === 'detail'" class="vc-x">
            <div class="vc-x-row"><span class="k">voice_id</span><span class="mono">{{ entry.data.card.voice_id }}</span></div>
            <div class="vc-x-row"><span class="k">语言</span><span class="mono">{{ entry.data.card.language }}</span></div>
            <div class="vc-x-row" title="对外授权凭证字段；授权账本功能尚未上线，自有素材使用不受任何影响。"><span class="k">授权</span><span class="mono">{{ entry.data.licenseLabel }}</span></div>
            <div class="vc-x-row"><span class="k">参考音</span><span class="mono" :title="entry.data.card.active_timbre?.ref_audio || ''">{{ entry.data.refName || '缺失' }}</span></div>
            <div class="vc-x-sub mono">版本记录 · v1–v{{ entry.data.card.version_count }}</div>
            <div v-for="version in entry.data.card.versions" :key="version.version" class="vc-x-row">
              <span class="mono">{{ version.version }}</span>
              <span class="mono dim">{{ version.created_at?.slice(0, 10) }}</span>
              <span class="note-text">{{ version.notes || '—' }}</span>
            </div>
            <p v-if="!entry.data.card.versions.length" class="vc-x-row dim">暂无版本记录。</p>
          </div>

          <div class="vc-meta">
            <div v-if="entry.data.retired">停用后不参与新合成，恢复即可继续使用。</div>
            <div v-else-if="entry.data.refName">参考音 · {{ entry.data.refName }}</div>
            <div v-else>参考音缺失，补参考音后可用。</div>
            <div class="mono">{{ entry.data.card.version_count }} 个版本 · 授权 {{ entry.data.licenseLabel }} · 更新 {{ entry.data.card.updated_at?.slice(5, 10) || '—' }}</div>
          </div>

          <div class="vc-foot">
            <span v-if="entry.data.current" class="cur-tag">当前使用</span>
            <button
              class="mini"
              type="button"
              :disabled="activating === entry.data.key || entry.data.retired || !entry.data.card.timbre_available"
              :title="entry.data.retired ? '已停用，先恢复再激活' : !entry.data.card.timbre_available ? '参考音缺失，补参考音后可用' : ''"
              @click="useVoice(entry)"
            >
              {{ activating === entry.data.key ? '激活中…' : '用于合成' }}
            </button>
            <button class="btn-ghost" type="button" @click="toggleExpand(entry.data.key)">
              {{ expanded[entry.data.key] ? '收起详情' : '详情' }}
            </button>
            <button
              class="btn-ghost danger"
              type="button"
              :disabled="patching === entry.data.key"
              @click="toggleCardStatus(entry.data)"
            >
              {{ patching === entry.data.key ? '处理中…' : entry.data.retired ? '恢复' : '停用' }}
            </button>
            <button
              class="btn-ghost danger"
              type="button"
              :disabled="deleting === entry.data.key || entry.data.current"
              :title="entry.data.current ? '正在用于合成；先「用于合成」其他声音再删除' : ''"
              @click="deleteCard(entry.data)"
            >
              {{ deleting === entry.data.key ? '删除中…' : deleteConfirm === entry.data.key ? '再点一次确认' : '删除' }}
            </button>
          </div>
        </div>

        <!-- 内置当前音色：虚线 ghost 卡，非独立声线卡 -->
        <div v-else class="vc ghost">
          <div class="vc-head">
            <b>当前音色</b>
            <span class="tag">跟随配置</span>
          </div>
          <div class="vc-meta">
            <div>非独立声线卡，参数直接来自设置。</div>
            <div class="mono">参考音 {{ config.ref_audio ? `· ${config.ref_audio.split(/[\\/]/).pop()}` : '未配置' }} · 口音档 {{ config.accent_level }} · 均可在设置修改</div>
          </div>
          <div class="vc-foot">
            <button class="btn-ghost" type="button" @click="setPage('wizard')">另存为声线卡</button>
            <button class="btn-ghost" type="button" @click="useVoice(entry)">用于合成</button>
          </div>
        </div>
      </template>
    </div>
    <div v-if="!filtered.length" class="empty-card">没有匹配的声音。</div>

    <div class="h-row src-h">
      <span class="lbl">新建声音 · 三种来源</span>
    </div>
    <div class="src">
      <div class="src-col">
        <span class="lbl">upload</span>
        <h4>一段自然说话录音</h4>
        <p>上传一段自然说话的音频，自动切片、转写、选参考片段，确认后存为声线卡。</p>
        <button class="go" type="button" @click="setPage('wizard')">开始 →</button>
      </div>
      <div class="src-col">
        <span class="lbl">separate</span>
        <h4>多人素材分离</h4>
        <p>多人素材进入声音工作坊，自动分清谁在何时说话，选一个人建声并训练。</p>
        <button class="go" type="button" @click="setPage('workshop')">进入声音工作坊 →</button>
      </div>
      <div class="src-col">
        <span class="lbl">import</span>
        <h4>导入已训练模型</h4>
        <p>训练完成后在工作坊「完成」一步扫描启用；暂不支持手动导入。</p>
        <span class="go dim" title="训练完成后在工作坊「完成」一步扫描启用">暂不可手动导入</span>
      </div>
    </div>

    <div class="h-row steps-h">
      <span class="lbl">声音工作坊</span>
      <span class="h-count">一条声线 · 五步</span>
    </div>
    <button class="steps" type="button" title="打开声音工作坊" @click="setPage('workshop')">
      <span class="step on"><span class="n">01</span>上传素材</span>
      <svg class="ic step-arrow" viewBox="0 0 24 24"><path d="M4 12h15M13 6l6 6-6 6" /></svg>
      <span class="step"><span class="n">02</span>选择说话人</span>
      <svg class="ic step-arrow" viewBox="0 0 24 24"><path d="M4 12h15M13 6l6 6-6 6" /></svg>
      <span class="step"><span class="n">03</span>校对</span>
      <svg class="ic step-arrow" viewBox="0 0 24 24"><path d="M4 12h15M13 6l6 6-6 6" /></svg>
      <span class="step"><span class="n">04</span>训练</span>
      <svg class="ic step-arrow" viewBox="0 0 24 24"><path d="M4 12h15M13 6l6 6-6 6" /></svg>
      <span class="step"><span class="n">05</span>完成</span>
    </button>

    <!-- 音色转换（kNN-VC） -->
    <div class="panel timbre">
      <div class="tool-head">
        <span class="lbl">音色转换 · knn-vc</span>
        <button class="mini" type="button" @click="toolOpen = !toolOpen">{{ toolOpen ? '收起工具' : '打开工具' }}</button>
      </div>
      <p class="ph">把任意已配音的人声素材转成当前声音 —— 适合翻唱、歌词或替换素材重新定音。{{ TIMBRE_TOPK_EXPLANATION }}</p>
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
          <button class="opt" type="button" disabled title="训练完成后在工作坊「完成」一步扫描启用；暂不支持手动导入。">
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
/* 屏 2 / 声音库（design/D-focus.html）：h-row + .vc 卡 + .src 三列 + .steps */
.h-row{display:flex;align-items:center;gap:12px;padding:22px 0 18px}
.h-row.src-h{padding-top:26px;padding-bottom:10px}
.h-row.steps-h{padding-top:26px;padding-bottom:8px}
.h-row .r{margin-left:auto;display:flex;align-items:center;gap:14px}
.h-count{font-family:var(--mono);font-size:11px;color:var(--text3)}

.vc-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.vc{border:1px solid var(--border);border-radius:var(--r);background:var(--surface);padding:16px 16px 0;display:flex;flex-direction:column}
.vc:hover{border-color:var(--border2)}
.vc.cur{border-color:var(--text)}
.vc.ghost{border-style:dashed;background:transparent}
.vc.off{color:var(--text2)}
.vc-head{display:flex;align-items:center;gap:9px;padding-bottom:4px}
.vc-head b{font-size:14.5px;font-weight:600}
.vc-head .ver{font-family:var(--mono);font-size:11px;color:var(--text3)}
.tag.ink{border-color:var(--text);color:var(--text)}
.vc-x{border-top:1px solid var(--border);padding:9px 0 10px;display:flex;flex-direction:column;gap:4px}
.vc-x-row{display:flex;align-items:center;gap:10px;font-size:11.5px;color:var(--text2);min-width:0}
.vc-x-row .k{font-family:var(--mono);font-size:10.5px;color:var(--text3);flex:none}
.vc-x-row .mono{font-size:10.5px;color:var(--text2)}
.vc-x-row .dim{color:var(--text3)}
.vc-x-row .note-text{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vc-x-sub{font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--text3);padding-top:5px;border-top:1px dashed var(--border)}
.vc-x-row .mono:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vc-meta{padding:6px 0 14px;font-size:12.5px;color:var(--text2);flex:1}
.vc-meta div{padding:1.5px 0}
.vc-meta .mono{font-size:11px;color:var(--text3)}
.vc-foot{border-top:1px solid var(--border);margin:0 -16px;padding:10px 16px;display:flex;gap:8px;align-items:center;flex-wrap:nowrap}
.vc-foot .mini{padding:0 8px}
.vc-foot .btn-ghost{padding:0 5px;font-size:11.5px;white-space:nowrap}
.vc.ghost .vc-foot{border-top-style:dashed}
.vc-foot .btn-ghost.danger:hover{background:var(--err-bg);color:var(--err)}
.cur-tag{font-family:var(--mono);font-size:10.5px;color:var(--on-primary);background:var(--primary);border-radius:2px;padding:2px 6px}
.empty-card{border:1px dashed var(--border2);border-radius:var(--r);padding:26px;text-align:center;color:var(--text3);font-size:12.5px;margin-top:14px}

.src{border:1px solid var(--border);border-radius:var(--r);overflow:hidden;display:grid;grid-template-columns:repeat(3,1fr)}
.src-col{padding:18px 20px 20px;border-left:1px solid var(--border);background:var(--surface);text-align:left}
.src-col:first-child{border-left:none}
.src-col:hover{background:var(--surface2)}
.src-col h4{font-size:13.5px;font-weight:600;padding:8px 0 4px}
.src-col p{font-size:12.5px;color:var(--text2)}
.src-col .go{display:inline-block;margin-top:12px;font-size:12px;color:var(--text2);padding:0}
.src-col .go:hover{color:var(--text)}
.src-col .go.dim{color:var(--text3);cursor:default}

.steps{display:flex;align-items:center;gap:14px;padding:16px 0 8px;flex-wrap:wrap;text-align:left}
.step{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--text2)}
.step .n{font-family:var(--mono);font-size:10.5px;width:20px;height:20px;line-height:18px;text-align:center;border:1px solid var(--border2);border-radius:2px;color:var(--text2);background:var(--surface);flex:none}
.step.on{color:var(--text);font-weight:600}
.step.on .n{background:var(--primary);border-color:var(--primary);color:var(--on-primary)}
.step-arrow{width:13px;height:13px;color:var(--border2)}

/* 音色转换面板（能力保留，外观对齐 D） */
.timbre .tool-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:4px}
.timbre .ph{font-size:12px;color:var(--text3)}
.tool-body{border-top:1px solid var(--border);margin-top:11px;padding-top:11px;display:flex;flex-direction:column;gap:8px}
.tool-field{display:flex;align-items:center;gap:8px;font-size:12px}
.tool-field .k{width:60px;color:var(--text2);flex:none}
.tool-field select,.tool-field input[type="file"]{flex:1;height:30px;border:1px solid var(--border);border-radius:var(--rs);background:var(--surface);padding:0 9px;font:inherit;font-size:12px;color:var(--text)}
@media (max-width:900px){.vc-grid{grid-template-columns:1fr 1fr}.src{grid-template-columns:1fr}.src-col{border-left:none;border-top:1px solid var(--border)}.src-col:first-child{border-top:none}}
@media (max-width:600px){.vc-grid{grid-template-columns:1fr}}
</style>
