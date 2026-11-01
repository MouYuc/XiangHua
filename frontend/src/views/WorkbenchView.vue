<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useConfig, ACCENT_OPTIONS, LANGUAGE_OPTIONS } from '@/composables/useConfig'
import { usePage } from '@/composables/usePage'
import { useReferenceAudio } from '@/composables/useReferenceAudio'
import { useSynthesis } from '@/composables/useSynthesis'
import { useToast } from '@/composables/useToast'
import { useVoices } from '@/composables/useVoices'
import { createDraftsStore } from '@/composables/drafts'
import { exportBatch, type EpisodeExportResult } from '@/lib/api'
import AudioPlayer from '@/components/AudioPlayer.vue'
import WaveForm from '@/components/WaveForm.vue'

defineOptions({ name: 'WorkbenchView' })

const configStore = useConfig()
const { config } = configStore
const { setPage } = usePage()
const voices = useVoices()
const synthesis = useSynthesis()
const reference = useReferenceAudio()
const draftsStore = createDraftsStore()

defineExpose({})

const SAMPLE = [
  '今天天气真不错，我们出去走走吧。',
  '<en>Good morning</en>，各位观众晚上好，欢迎回到频道。',
].join('\n')

const initialScript = String(config.ref_text || '').trim() || SAMPLE
const lines = ref<string[]>(initialScript.split('\n'))
const lineEls = ref<Record<number, HTMLTextAreaElement | null>>({})
const importing = ref(false)
const drafts = ref(draftsStore.load())

const speech = computed(() =>
  lines.value
    .map((text, index) => ({ index, text: text.trim() }))
    .filter((line) => line.text !== '' && !line.text.startsWith('#')),
)

const scriptText = computed(() => speech.value.map((line) => line.text).join('\n'))

const charCount = computed(() => speech.value.reduce((sum, line) => sum + line.text.length, 0))

/**
 * 把一行台词映射到它在 `outputs` 里的下标（-1 表示没有对应输出）。
 *
 * 不能按下标直接对位：新增、合并或删除一行后，`speech` 的位置与后端返回的
 * `outputs` 顺序就错开了，界面会把上一句的音频挂到这一句上。这里用「文本 +
 * 同文本中的序号」作为键，编辑过的行自然找不到输出，于是显示为待生成。
 */
function mappedOutputIndex(lineIndex: number): number {
  const position = speech.value.findIndex((line) => line.index === lineIndex)
  if (position < 0) return -1
  const key = speech.value[position]?.text.trim() ?? ''
  if (!key) return -1
  const generated = synthesis.generatedLines.value
  let ordinal = 0
  for (let i = 0; i < position; i += 1) {
    if (speech.value[i]?.text.trim() === key) ordinal += 1
  }
  let seen = 0
  for (let i = 0; i < generated.length; i += 1) {
    if (generated[i] !== key) continue
    if (seen === ordinal) return i
    seen += 1
  }
  return -1
}

function outputFor(lineIndex: number): (typeof synthesis.outputs.value)[number] | null {
  const index = mappedOutputIndex(lineIndex)
  return index >= 0 ? synthesis.outputs.value[index] ?? null : null
}

type LineState = 'idle' | 'done' | 'warn' | 'failed' | 'generating' | 'queued'

function stateFor(lineIndex: number): LineState {
  const position = speech.value.findIndex((line) => line.index === lineIndex)
  const output = outputFor(lineIndex)
  if (output) {
    if (output.error) return 'failed'
    if (output.warning) return 'warn'
    return 'done'
  }
  if (synthesis.busy.value && synthesis.job.value) {
    if (position >= 0 && position === Number(synthesis.job.value.current || 0) - 1) return 'generating'
    if (position >= 0 && position < Number(synthesis.job.value.current || 0)) return 'queued'
    if (position >= Number(synthesis.job.value.current || 0)) return 'queued'
  }
  return 'idle'
}

const STATE_META: Record<LineState, { tag: string; tone: string } | null> = {
  idle: null,
  done: { tag: '已完成', tone: 'ok' },
  warn: { tag: '时长偏短', tone: 'warn' },
  failed: { tag: '合成失败', tone: 'err' },
  generating: { tag: '生成中', tone: 'mut' },
  queued: { tag: '排队中', tone: 'mut' },
}

const pendingCount = computed(() => speech.value.filter((line) => {
  const state = stateFor(line.index)
  return state === 'idle' || state === 'failed'
}).length)

async function generate() {
  if (synthesis.busy.value) return
  if (!scriptText.value.trim()) return
  drafts.value = draftsStore.add(scriptText.value)
  exportResult.value = null
  synthesis.clearResults()
  await synthesis.start(scriptText.value)
}

// —— 整集导出：SRT 字幕 + 拼接音轨，交付短剧剪辑 ——
const toast = useToast()
const exporting = ref(false)
const exportResult = ref<EpisodeExportResult | null>(null)

async function exportEpisode() {
  const jobId = synthesis.job.value?.job_id
  if (!jobId || exporting.value) return
  exporting.value = true
  try {
    exportResult.value = await exportBatch(jobId)
    toast.success(exportResult.value.message)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    exporting.value = false
  }
}

async function retryLine(lineIndex: number) {
  const line = lines.value[lineIndex]
  if (!line?.trim()) return
  // `retry` replaces one slot of `outputs`, so it takes an *output* index. The
  // speech position stops being the same number as soon as a line was inserted,
  // merged or deleted — passing it made retry write the new take into the wrong
  // slot (and therefore onto the wrong line).
  const outputIndex = mappedOutputIndex(lineIndex)
  if (outputIndex < 0) return
  await synthesis.retry(outputIndex)
}

function setLinesFromText(text: string) {
  lines.value = text.replace(/\r/g, '').split('\n')
}

async function importTxt(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  importing.value = true
  try {
    setLinesFromText(await file.text())
  } finally {
    importing.value = false
    input.value = ''
  }
}

function loadSample() {
  setLinesFromText(SAMPLE)
}

function clearScript() {
  synthesis.clearResults()
  exportResult.value = null
  lines.value = ['']
}

function restoreDraft(text: string) {
  setLinesFromText(text)
}

function onLineKeydown(event: KeyboardEvent, index: number) {
  const target = event.target as HTMLTextAreaElement
  // Ctrl/Cmd+Enter means "generate", so it must not fall into the split branch.
  const command = event.ctrlKey || event.metaKey
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !command) {
    event.preventDefault()
    const before = target.value.slice(0, target.selectionStart)
    const after = target.value.slice(target.selectionStart)
    lines.value.splice(index, 1, before, after)
    void nextTick(() => {
      const element = lineEls.value[index + 1]
      if (element) {
        element.focus()
        element.setSelectionRange(0, 0)
      }
    })
  } else if (event.key === 'Backspace' && target.selectionStart === 0 && target.selectionEnd === 0 && index > 0) {
    event.preventDefault()
    const previous = lines.value[index - 1] ?? ''
    lines.value.splice(index - 1, 2, previous + target.value)
    void nextTick(() => {
      const element = lineEls.value[index - 1]
      if (element) {
        element.focus()
        element.setSelectionRange(previous.length, previous.length)
      }
    })
  }
}

function autoResize(event: Event) {
  const target = event.target as HTMLTextAreaElement
  target.style.height = 'auto'
  target.style.height = `${target.scrollHeight}px`
}

// Ctrl+Enter 全局生成（仅工作台生效）。注册与注销成对，避免离开页面后仍然劫持按键。
const { page } = usePage()
function onGlobalKey(event: KeyboardEvent) {
  if (page.value !== 'workbench') return
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault()
    void generate()
  }
}
onMounted(() => window.addEventListener('keydown', onGlobalKey))
onUnmounted(() => window.removeEventListener('keydown', onGlobalKey))

// —— 检查器 ——
const currentProfile = computed(() => voices.profileById(config.voice_profile_id || 'current'))
const voiceName = computed(() => voices.displayName(currentProfile.value))
const voiceAvatar = computed(() => voices.avatarText(voiceName.value))
const refOk = computed(() => Boolean(config.ref_audio?.trim()))

// 后端没有 "自动" 档位（text_lang 恒为具体语种），所以这里只列出真实可选值。
const LANG_SEG = LANGUAGE_OPTIONS.map((item) => ({ key: item.value, label: item.label }))
const activeLang = computed(() => config.prompt_lang)
function setLang(key: string) {
  config.prompt_lang = (key === 'auto' ? 'zh' : key) as typeof config.prompt_lang
}

const ACCENT_SEG = ACCENT_OPTIONS.map((item) => ({ key: item.value, label: item.value === 'off' ? '关' : item.label }))

type PresetKey = 'clear' | 'expressive'
const PRESETS: Record<PresetKey, { label: string; desc: string; values: Partial<typeof config> }> = {
  clear: {
    label: '清晰优先',
    desc: '实测可懂度最好的一组参数',
    values: { speed: 1.0, temperature: 0.95, sample_steps: 64, top_p: 0.88, pause_second: 0.15 },
  },
  expressive: {
    label: '表现力优先',
    desc: '更有情绪起伏，偶有含糊',
    values: { speed: 1.05, temperature: 1.08, sample_steps: 32, top_p: 0.92, pause_second: 0.2 },
  },
}

const activePreset = computed<PresetKey | null>(() => {
  for (const [key, preset] of Object.entries(PRESETS) as [PresetKey, typeof PRESETS.clear][]) {
    const match = Object.entries(preset.values).every(([field, value]) => Number(config[field as keyof typeof config]) === Number(value))
    if (match) return key
  }
  return null
})

function applyPreset(key: PresetKey) {
  Object.assign(config, PRESETS[key].values)
}

const advOpen = ref(false)

const PARAM_ROWS = computed(() => ([
  { key: 'speed', label: '语速', min: 0.5, max: 3, step: 0.05, fmt: (v: number) => `${v.toFixed(2)}×` },
  { key: 'pause_second', label: '句间停顿', min: 0, max: 2, step: 0.05, fmt: (v: number) => `${v.toFixed(2)}s` },
  { key: 'temperature', label: '温度', min: 0.05, max: 2, step: 0.05, fmt: (v: number) => v.toFixed(2) },
  { key: 'sample_steps', label: '采样步数', min: 8, max: 128, step: 1, fmt: (v: number) => String(v) },
  { key: 'top_p', label: 'top_p', min: 0.05, max: 1, step: 0.01, fmt: (v: number) => v.toFixed(2) },
] as const))

function paramPercent(row: (typeof PARAM_ROWS.value)[number]) {
  const value = Number(config[row.key as keyof typeof config]) || row.min
  return Math.round(((value - row.min) / (row.max - row.min)) * 100)
}

function setParam(row: (typeof PARAM_ROWS.value)[number], event: Event) {
  const target = event.target as HTMLInputElement
  const value = Number(target.value)
  Object.assign(config, { [row.key]: value })
}

function resetParams() {
  applyPreset('clear')
}

watch(() => config.ref_text, (next) => {
  // 后端回填参考文本时，不覆盖用户正在编辑的台词
  if (!lines.value.some((line) => line.trim()) && next) setLinesFromText(next)
}, { once: true })

const estimatedSeconds = computed(() => Math.max(5, Math.round(charCount.value * 0.24 * Number(config.speed || 1))))
</script>

<template>
  <div class="wb">
    <div class="canvas-wrap">
      <div class="stage">
        <span class="plus tl"></span><span class="plus tr"></span><span class="plus bl"></span><span class="plus br"></span>
        <div class="stage-head">
          <span class="t">台词 · 一行一条，语言标记如 <span class="mono">&lt;en&gt;</span> 直接输入</span>
          <span class="r">
            <label class="btn-ghost" style="cursor:pointer">
              <svg class="ic sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" /></svg>
              {{ importing ? '导入中…' : '导入 TXT' }}
              <input type="file" accept=".txt,text/plain" style="display:none" @change="importTxt">
            </label>
            <button class="btn-ghost" type="button" @click="loadSample">示例台词</button>
          </span>
        </div>

        <div v-for="(line, index) in lines" :key="index" class="line">
          <div class="no">
            <span class="idx">{{ String(index + 1).padStart(2, '0') }}</span>
            <span v-if="STATE_META[stateFor(index)]" class="tag" :class="STATE_META[stateFor(index)]!.tone">{{ STATE_META[stateFor(index)]!.tag }}</span>
          </div>
          <textarea
            :ref="(el) => { lineEls[index] = el as HTMLTextAreaElement | null }"
            v-model="lines[index]"
            class="line-input"
            rows="1"
            :placeholder="index === 0 ? '在这里输入台词…' : ''"
            @keydown="onLineKeydown($event, index)"
            @input="autoResize"
          ></textarea>

          <div v-if="stateFor(index) === 'done' || stateFor(index) === 'warn'" class="audio-strip">
            <AudioPlayer :src="outputFor(index)!.url" :title="outputFor(index)!.text" />
            <span v-if="stateFor(index) === 'warn'" class="warn-text">{{ outputFor(index)!.warning || '时长偏短，建议改写或重试' }}</span>
            <span style="flex:1"></span>
            <span class="line-acts shown">
              <button type="button" title="重新生成这一句" @click="retryLine(index)">
                <svg class="ic sm" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v6h-6" /></svg>
              </button>
            </span>
          </div>
          <div v-else-if="stateFor(index) === 'failed'" class="audio-strip">
            <span class="err-text">{{ outputFor(index)!.error || '合成失败' }}</span>
            <span style="flex:1"></span>
            <button class="mini" style="height:24px" type="button" @click="retryLine(index)">
              <svg class="ic sm" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v6h-6" /></svg>重试
            </button>
          </div>
          <div v-else-if="stateFor(index) === 'generating'" class="audio-strip">
            <span class="gen-progress"><span :style="{ width: `${synthesis.progress.value}%` }"></span></span>
            <span class="mono pct">{{ synthesis.progress.value }}%</span>
          </div>
          <div v-else-if="stateFor(index) === 'queued'" class="audio-strip">
            <span class="queued-text">排队中 · 前一句完成后自动开始</span>
          </div>
        </div>

        <button class="add-line" type="button" @click="lines.push('')">
          <svg class="ic sm" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>添加一行台词 · 空行自动分段
        </button>

        <div v-if="drafts.length" class="drafts-row">
          <span class="drafts-label">最近草稿</span>
          <button v-for="draft in drafts.slice(0, 3)" :key="draft.id" class="draft-chip" type="button" :title="draft.text" @click="restoreDraft(draft.text)">
            {{ draft.text.slice(0, 14) }}{{ draft.text.length > 14 ? '…' : '' }}
          </button>
          <button class="btn-ghost" style="height:20px;font-size:10.5px" type="button" @click="drafts = []">清除</button>
        </div>
      </div>

      <div class="canvas-foot">
        <span class="foot-meta">
          <span>{{ speech.length }} 行 · {{ charCount }} 字</span>
          <button type="button" @click="clearScript">清空台词</button>
          <span><kbd>Ctrl</kbd>+<kbd>Enter</kbd> 全部生成</span>
        </span>
        <span style="flex:1"></span>
        <span class="foot-meta">
          <button
            v-if="synthesis.job.value?.status === 'done' && !synthesis.busy.value"
            class="mini"
            style="height:22px"
            type="button"
            :disabled="exporting"
            title="导出 episode.srt 字幕与整集音轨"
            @click="exportEpisode"
          >{{ exporting ? '导出中…' : '导出整集' }}</button>
          <template v-if="exportResult">
            <a class="mini" style="height:22px" :href="exportResult.srt_url" download>SRT</a>
            <a v-if="exportResult.full_url" class="mini" style="height:22px" :href="exportResult.full_url" download>音轨</a>
          </template>
          <span class="mono">预计 ~{{ estimatedSeconds }}s</span>
        </span>
      </div>
    </div>

    <!-- 检查器 -->
    <aside class="inspector">
      <div class="ins-pad">
        <div class="ins-sec">
          <div class="h">声音<button class="btn-ghost" style="height:20px;font-size:11px" type="button" @click="setPage('voices')">管理 →</button></div>
          <button class="voice-pick" type="button" @click="setPage('voices')">
            <span class="v-avatar">{{ voiceAvatar }}</span>
            <span class="t">
              <b>{{ voiceName }}</b>
              <span v-if="refOk">参考音已配对 · {{ config.ref_audio.split(/[\\/]/).pop() }}</span>
              <span v-else style="color:var(--warn)">未配置参考音 · 去声音库建档</span>
            </span>
            <svg class="ic sm" viewBox="0 0 24 24" style="color:var(--text3)"><path d="M6 9l6 6 6-6" /></svg>
          </button>
          <div v-if="reference.pair.value && reference.pair.value.status === 'mismatched'" class="note" style="margin-top:7px">
            参考音与参考文本不匹配：{{ reference.pair.value.message }}
          </div>
        </div>

        <div class="ins-sec">
          <div class="h">语言</div>
          <div class="seg">
            <button v-for="item in LANG_SEG" :key="item.key" type="button" :class="{ on: activeLang === item.key }" @click="setLang(item.key)">{{ item.label }}</button>
          </div>
        </div>

        <div class="ins-sec">
          <div class="h">广普程度</div>
          <div class="seg">
            <button v-for="item in ACCENT_SEG" :key="item.key" type="button" :class="{ on: config.accent_level === item.key }" @click="config.accent_level = item.key as typeof config.accent_level">{{ item.label }}</button>
          </div>
          <p class="ins-hint">只影响读音，不动音色；「目的」「的确」等固定搭配始终受保护。</p>
        </div>

        <div class="ins-sec">
          <div class="h">参数预设</div>
          <div style="display:flex;flex-direction:column;gap:6px">
            <button v-for="(preset, key) in PRESETS" :key="key" class="radio-row" :class="{ on: activePreset === key }" type="button" @click="applyPreset(key)">
              <span class="dot2"></span>
              <span><b>{{ preset.label }}</b><span class="d">{{ preset.desc }}</span></span>
            </button>
          </div>
        </div>

        <div class="disc" :class="{ open: advOpen }">
          <button class="hd" type="button" @click="advOpen = !advOpen">
            <svg class="ic sm chev" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" /></svg>高级参数
          </button>
          <div v-show="advOpen" class="bd">
            <div v-for="row in PARAM_ROWS" :key="row.key" class="param">
              <span class="k">{{ row.label }}</span>
              <input class="range" type="range" :min="row.min" :max="row.max" :step="row.step" :value="Number(config[row.key as keyof typeof config])" @input="setParam(row, $event)">
              <span class="v">{{ row.fmt(Number(config[row.key as keyof typeof config])) }}</span>
            </div>
            <div style="display:flex;gap:6px;margin-top:8px">
              <button class="mini" type="button" @click="resetParams">恢复默认</button>
              <span class="ins-hint" style="align-self:center">改动自动保存，仅对本项目生效</span>
            </div>
          </div>
        </div>
      </div>

      <div class="ins-gen">
        <div v-if="synthesis.readiness.value.status === 'warming'" class="ins-note warn">
          引擎预热中，生成会稍等片刻…
        </div>
        <div v-else-if="synthesis.readiness.value.status === 'failed'" class="ins-note err">
          引擎未就绪：{{ synthesis.readiness.value.error || synthesis.readiness.value.message }}
          <button class="mini" style="height:22px;margin-left:6px" type="button" @click="synthesis.retryWarmup()">重试预热</button>
        </div>
        <button class="btn-primary" style="width:100%" :disabled="synthesis.busy.value" @click="generate">
          <svg class="ic sm" viewBox="0 0 24 24" style="fill:currentColor;stroke:none"><path d="M8 5.5v13l11-6.5z" /></svg>
          {{ synthesis.busy.value ? '生成中…' : `生成 ${pendingCount || speech.length} 句` }}
        </button>
        <p class="ins-gen-hint">
          已完成 {{ synthesis.outputs.value.filter((item) => !item.error).length }} 句 · 显卡{{ synthesis.busy.value ? '占用中' : '空闲' }}
        </p>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.wb{display:grid;grid-template-columns:1fr 300px;align-items:start}
.canvas-wrap{padding:18px 24px 0;max-width:820px;border-right:1px solid var(--border);display:flex;flex-direction:column;min-width:0}
.stage{position:relative;border:1px solid var(--border);border-radius:10px;background:var(--surface);align-self:flex-start;width:100%}
.plus{position:absolute;width:11px;height:11px;color:var(--border2)}
.plus::before,.plus::after{content:"";position:absolute;background:currentColor}
.plus::before{left:5px;top:0;width:1px;height:11px}
.plus::after{left:0;top:5px;width:11px;height:1px}
.plus.tl{left:-6px;top:-6px}.plus.tr{right:-6px;top:-6px}.plus.bl{left:-6px;bottom:-6px}.plus.br{right:-6px;bottom:-6px}
.stage-head{display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--border)}
.stage-head .t{font-size:12px;color:var(--text3)}
.stage-head .r{margin-left:auto;display:flex;gap:2px}
.line{padding:10px 14px;border-bottom:1px solid var(--border)}
.line:hover{background:var(--surface2)}
.line .no{display:flex;gap:8px;align-items:center;margin-bottom:3px}
.line .idx{font:11px var(--mono);color:var(--text3)}
.line-input{width:100%;border:none;outline:none;background:none;resize:none;font:14px/1.7 var(--font);color:var(--text);overflow:hidden;display:block}
.audio-strip{display:flex;align-items:center;gap:9px;margin-top:7px}
.line-acts{display:flex;gap:1px}
.line:hover .line-acts{opacity:1}
.line-acts button{width:24px;height:24px;border-radius:5px;display:grid;place-items:center;color:var(--text3)}
.line-acts button:hover{background:var(--surface3);color:var(--text)}
.pct{font-size:10.5px;color:var(--text3)}
.err-text{font-size:11.5px;color:var(--err)}
.warn-text{font-size:11.5px;color:var(--warn)}
.queued-text{font-size:11.5px;color:var(--text3)}
.add-line{display:flex;align-items:center;gap:8px;width:100%;padding:10px 14px;color:var(--text3);font-size:12.5px;border-bottom:1px solid var(--border)}
.add-line:hover{color:var(--text);background:var(--surface2)}
.drafts-row{display:flex;align-items:center;gap:6px;padding:8px 14px;flex-wrap:wrap}
.drafts-label{font-size:10.5px;color:var(--text3)}
.draft-chip{font-size:11px;color:var(--text2);border:1px solid var(--border);border-radius:999px;padding:2px 9px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.draft-chip:hover{border-color:var(--border2);color:var(--text)}
.canvas-foot{display:flex;align-items:center;gap:10px;padding:10px 2px 12px}
.foot-meta{font-size:11.5px;color:var(--text3);display:flex;gap:10px;align-items:center}
.foot-meta button{color:var(--text2);font-size:11.5px}
.foot-meta button:hover{color:var(--text);text-decoration:underline}

.inspector{display:flex;flex-direction:column;position:sticky;top:48px;height:calc(100vh - 48px);overflow:auto}
.ins-pad{padding:16px;display:flex;flex-direction:column;gap:14px;flex:1}
.ins-sec .h{font-size:10.5px;font-weight:600;color:var(--text3);letter-spacing:.5px;margin-bottom:6px;display:flex;align-items:center;justify-content:space-between}
.voice-pick{display:flex;align-items:center;gap:9px;width:100%;padding:9px;border:1px solid var(--border);border-radius:var(--r);background:var(--surface);text-align:left}
.voice-pick:hover{border-color:var(--border2);background:var(--surface2)}
.voice-pick .t{flex:1;min-width:0}
.voice-pick .t b{display:block;font-size:12.5px;font-weight:550}
.voice-pick .t span{font-size:11px;color:var(--text3);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ins-hint{font-size:11px;color:var(--text3);margin-top:6px;line-height:1.6}
.disc{border-top:1px solid var(--border);padding-top:10px}
.disc .hd{display:flex;align-items:center;gap:5px;width:100%;font-size:11.5px;color:var(--text2)}
.disc .hd:hover{color:var(--text)}
.disc .hd .chev{margin-left:auto;transition:.15s}
.disc.open .hd .chev{transform:rotate(90deg)}
.disc .bd{padding-top:8px}
.param{display:flex;align-items:center;gap:9px;padding:4px 0}
.param .k{font-size:11.5px;color:var(--text2);width:60px;flex:none}
.param .v{font:11px var(--mono);color:var(--text);width:42px;text-align:right;flex:none}
.range{flex:1;appearance:none;height:3px;border-radius:2px;background:var(--surface3);outline:none}
.range::-webkit-slider-thumb{appearance:none;width:11px;height:11px;border-radius:50%;background:var(--surface);border:1.5px solid var(--text2);cursor:pointer}
.range::-moz-range-thumb{width:11px;height:11px;border-radius:50%;background:var(--surface);border:1.5px solid var(--text2);cursor:pointer}
.ins-gen{border-top:1px solid var(--border);padding:12px 16px;background:var(--bg)}
.ins-note{font-size:11.5px;border-radius:6px;padding:7px 9px;margin-bottom:8px;border:1px solid}
.ins-note.warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn-bd)}
.ins-note.err{color:var(--err);background:var(--err-bg);border-color:var(--err-bd)}
.ins-gen-hint{font-size:10.5px;color:var(--text3);margin-top:7px;text-align:center}

@media (max-width:1120px){
  .wb{grid-template-columns:1fr}
  .inspector{position:static;height:auto;border-top:1px solid var(--border)}
  .canvas-wrap{border-right:none;max-width:none;padding:16px}
}
</style>
