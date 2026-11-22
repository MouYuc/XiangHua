<script setup lang="ts">
import { computed, nextTick, onActivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useConfig } from '@/composables/useConfig'
import { usePage } from '@/composables/usePage'
import { useSynthesis } from '@/composables/useSynthesis'
import { useToast } from '@/composables/useToast'
import { useVoices } from '@/composables/useVoices'
import { createDraftsStore } from '@/composables/drafts'
import { exportBatch, type EpisodeExportResult } from '@/lib/api'
import AudioPlayer from '@/components/AudioPlayer.vue'
import WaveForm from '@/components/WaveForm.vue'
import InspectorPanel from '@/components/workbench/InspectorPanel.vue'

defineOptions({ name: 'WorkbenchView' })

const configStore = useConfig()
const { config } = configStore
const { page, setPage } = usePage()
const synthesis = useSynthesis()
const voices = useVoices()
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

// —— 行内语种标签自动补全 ——
// 后端把 `<zh>/<yue>/<en>/<ja>/<ko>` 当作行内语种标记（混读一行自动分段）。
// 输入 `<` 弹出菜单，继续打字按标签前缀过滤；选中一段文字后按 `<`，
// 确认语种即把该段包进 `<xx>…</xx>`。
const LANG_TAGS = [
  { tag: 'zh', label: '中文' },
  { tag: 'yue', label: '粤语' },
  { tag: 'en', label: '英语' },
  { tag: 'ja', label: '日语' },
  { tag: 'ko', label: '韩语' },
] as const
type TagMenu = {
  index: number
  /** `<` 字符在行内的位置（替换基准）。 */
  start: number
  /** 替换终点：普通模式=「< + 前缀」之后；包裹模式=选区终点。 */
  end: number
  /** `<` 之后已输入的前缀。 */
  query: string
  active: number
  /** true = 选中文字后按 `<` 的包裹模式；false = 普通插入模式。 */
  wrap: boolean
}
const tagMenu = ref<TagMenu | null>(null)
const tagItems = (menu: TagMenu) =>
  LANG_TAGS.filter((item) => item.tag.startsWith(menu.query.toLowerCase()))

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

const canExport = computed(() => synthesis.job.value?.status === 'done' && !synthesis.busy.value)

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
  // —— 语种标签菜单优先处理 ——
  const menu = tagMenu.value
  if (menu && menu.index === index) {
    const items = tagItems(menu)
    if (items.length) {
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        tagMenu.value = { ...menu, active: (menu.active + 1) % items.length }
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        tagMenu.value = { ...menu, active: (menu.active - 1 + items.length) % items.length }
        return
      }
      if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault()
        applyLangTag(items[menu.active] ?? items[0])
        return
      }
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      closeTagMenu()
      return
    }
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) {
      closeTagMenu()
    }
  }
  // 选中一段文字后按 `<`：不插入字符，直接弹菜单，确认语种即包裹该段
  if (event.key === '<' && !event.ctrlKey && !event.metaKey && !event.altKey) {
    const start = target.selectionStart ?? 0
    const end = target.selectionEnd ?? 0
    if (end > start) {
      event.preventDefault()
      tagMenu.value = { index, start, end, query: '', active: 0, wrap: true }
      return
    }
  }
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

// 窄视口下长台词会折行：行高必须在挂载、批量替换（导入/示例/分行合并）与窗口宽度变化时重算，
// 否则行保持 rows=1 的高度，折行文字被 overflow:hidden 竖向裁掉。
function resizeAllLines() {
  for (const element of Object.values(lineEls.value)) {
    if (!element) continue
    element.style.height = 'auto'
    element.style.height = `${element.scrollHeight}px`
  }
}

// Ctrl+Enter 全局生成（仅工作台生效）。注册与注销成对，避免离开页面后仍然劫持按键。
// ESC 用来收起移动端的「参数」浮层。
const insOpen = ref(false)
function onGlobalKey(event: KeyboardEvent) {
  if (page.value !== 'workbench') return
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault()
    void generate()
  } else if (event.key === 'Escape' && insOpen.value) {
    insOpen.value = false
  }
}
onMounted(() => {
  window.addEventListener('keydown', onGlobalKey)
  window.addEventListener('resize', resizeAllLines)
  void nextTick(resizeAllLines)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKey)
  window.removeEventListener('resize', resizeAllLines)
})
// KeepAlive 返回本页时视口宽度可能已变化，回流后重算一次行高。
onActivated(() => void nextTick(resizeAllLines))

watch(lines, () => void nextTick(resizeAllLines), { deep: true })

// —— 语种标签菜单的交互 ——

function closeTagMenu() {
  tagMenu.value = null
}

function applyLangTag(item: { tag: string; label: string }) {
  const menu = tagMenu.value
  if (!menu) return
  const el = lineEls.value[menu.index]
  if (!el) {
    closeTagMenu()
    return
  }
  const text = el.value
  let next: string
  let caret: number
  if (menu.wrap) {
    // 包裹模式：把选中文字包进 <xx>…</xx>
    const selected = text.slice(menu.start, menu.end)
    next = `${text.slice(0, menu.start)}<${item.tag}>${selected}</${item.tag}>${text.slice(menu.end)}`
    caret = menu.start + item.tag.length + 2 + selected.length + item.tag.length + 3
  } else {
    // 普通模式：替换「< + 前缀」，插入成对标签，光标落在中间
    next = `${text.slice(0, menu.start)}<${item.tag}></${item.tag}>${text.slice(menu.end)}`
    caret = menu.start + item.tag.length + 2
  }
  lines.value[menu.index] = next
  closeTagMenu()
  void nextTick(() => {
    el.focus()
    el.setSelectionRange(caret, caret)
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  })
}

/** input 事件：弹开菜单（刚输入 `<`）、刷新前缀过滤、或按规则关闭。 */
function onLineInput(event: Event, index: number) {
  autoResize(event)
  const el = event.target as HTMLTextAreaElement
  const caret = el.selectionStart ?? 0
  const text = el.value
  const menu = tagMenu.value
  
  if (menu && menu.index !== index) {
    closeTagMenu()
    return
  }
  if (menu) {
    if (menu.wrap) {
      // 包裹模式：菜单开着时用户又打字，直接放弃补全（输入原样保留）
      closeTagMenu()
      return
    }
    if (caret <= menu.start) {
      closeTagMenu()
      return
    }
    const query = text.slice(menu.start + 1, caret)
    if (query.length > 6 || /[<>\s]/.test(query) || !tagItems({ ...menu, query }).length) {
      closeTagMenu()
      return
    }
    
    tagMenu.value = { ...menu, query, end: caret, active: 0 }
    return
  }
  if (caret > 0 && text[caret - 1] === '<') {
    
    tagMenu.value = { index, start: caret - 1, end: caret, query: '', active: 0, wrap: false }
  }
}

function onLineBlur(index: number) {
  if (tagMenu.value?.index === index) closeTagMenu()
}

watch(() => config.ref_text, (next) => {
  // 后端回填参考文本时，不覆盖用户正在编辑的台词
  if (!lines.value.some((line) => line.trim()) && next) setLinesFromText(next)
}, { once: true })

const estimatedSeconds = computed(() => Math.max(5, Math.round(charCount.value * 0.24 * Number(config.speed || 1))))

// —— 生成条 meta：行数 · 预计时长 · GPU/预热状态 ——
const engineMeta = computed(() => {
  if (synthesis.busy.value) return 'GPU 合成中'
  const status = synthesis.readiness.value.status
  if (status === 'ready') return 'GPU 空闲'
  if (status === 'warming') return '引擎预热中…'
  if (status === 'failed') return '引擎未就绪'
  return '引擎待机'
})

const phaseText = computed(() => {
  const phase = String(synthesis.job.value?.phase || '')
  if (phase === 'prepare') return '准备中'
  if (phase === 'render') return '渲染音频'
  return '逐句合成中'
})

function fileBase(url: string) {
  return (url || '').split(/[\\/]/).pop() || 'audio.wav'
}

function queuePos(lineIndex: number) {
  return speech.value.findIndex((line) => line.index === lineIndex) + 1
}

// —— 顶部声音行（D 稿 voice-row）：当前声线卡 + 参考音状态 ——
const voiceName = computed(() => {
  const card = voices.cards.value.find((item) => item.voice_id === config.voice_profile_id)
  return card?.display_name || voices.displayName(voices.profileById(config.voice_profile_id || 'current'))
})
const refOk = computed(() => Boolean(config.ref_audio?.trim()))
const refBasename = computed(() => (config.ref_audio || '').split(/[\\/]/).pop() || '未配置')
</script>

<template>
  <div class="wb">
    <div class="wb-main">
      <!-- 选声音 -->
      <div class="voice-row">
        <span class="lbl">声音</span>
        <span class="voice-chip">
          <b>{{ voiceName }}</b>
          <span class="tag" :class="refOk ? 'tag-ink' : 'warn'">{{ refOk ? '可用' : '未配参考音' }}</span>
          <span class="voice-meta mono">{{ refOk ? refBasename : '去声音库建档' }}</span>
        </span>
        <button class="alink" type="button" @click="setPage('voices')">声音库 →</button>
      </div>

      <!-- 台词头部 -->
      <div class="script-head">
        <span class="lbl">台词 · 一行一句</span>
        <span class="r">
          <span class="fmeta">{{ speech.length }} 行 · 输入 &lt; 自动补全语种标签，可混读一行</span>
          <label class="alink" style="cursor:pointer">
            <svg class="ic sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" /></svg>
            {{ importing ? '导入中…' : '导入 TXT' }}
            <input type="file" accept=".txt,text/plain" style="display:none" @change="importTxt">
          </label>
          <button class="alink" type="button" @click="loadSample">示例台词</button>
          <button v-if="canExport" class="alink" type="button" :disabled="exporting" title="导出 episode.srt 字幕与整集音轨" @click="exportEpisode">
            {{ exporting ? '导出中…' : '导出整集' }}
          </button>
          <template v-if="exportResult">
            <a class="alink" :href="exportResult.srt_url" download>SRT</a>
            <a v-if="exportResult.full_url" class="alink" :href="exportResult.full_url" download>音轨</a>
          </template>
          <button class="alink" type="button" @click="clearScript">清空</button>
        </span>
      </div>

      <!-- 台词行 -->
      <div class="script">
        <div v-for="(line, index) in lines" :key="index" class="line">
          <span class="no mono">{{ String(index + 1).padStart(2, '0') }}</span>
          <div class="body">
            <textarea
              :ref="(el) => { lineEls[index] = el as HTMLTextAreaElement | null }"
              v-model="lines[index]"
              class="line-input"
              rows="1"
              :placeholder="index === 0 ? '在这里输入台词…' : ''"
              @keydown="onLineKeydown($event, index)"
              @input="onLineInput($event, index)"
              @blur="onLineBlur(index)"
            ></textarea>

            <!-- 语种标签自动补全菜单（输入 < 弹出；选中文字按 < 进入包裹模式） -->
            <div v-if="tagMenu && tagMenu.index === index && tagItems(tagMenu).length" class="tag-menu">
              <button
                v-for="(item, i) in tagItems(tagMenu)"
                :key="item.tag"
                type="button"
                :class="{ on: i === tagMenu.active }"
                @mousedown.prevent="applyLangTag(item)"
              >
                <span class="mono">&lt;{{ item.tag }}&gt;</span>{{ item.label }}
              </button>
            </div>

            <!-- 已完成 / 时长偏短：徽标 + 波形 + 试听 + 下载 + 单句重生成 -->
            <div v-if="stateFor(index) === 'done'" class="line-result">
              <span class="tag ok">已完成</span>
              <WaveForm :seed="outputFor(index)!.text" :bars="26" :height="24" />
              <span class="fmeta">{{ fileBase(outputFor(index)!.url) }}</span>
              <span class="acts">
                <AudioPlayer :src="outputFor(index)!.url" :title="outputFor(index)!.text" />
                <a class="alink" :href="outputFor(index)!.url" download>下载</a>
                <button class="alink" type="button" :disabled="synthesis.busy.value" title="重新生成这一句" @click="retryLine(index)">单句重生成</button>
              </span>
            </div>

            <!-- 时长偏短：黄色警告 + 试听 + 补停顿重生成 -->
            <div v-else-if="stateFor(index) === 'warn'" class="line-result">
              <span class="tag warn">
                <svg class="ic" style="width:11px;height:11px" viewBox="0 0 24 24"><path d="M12 3.5 2.8 19.5h18.4L12 3.5Z" /><path d="M12 9.5v4.5" /><path d="M12 16.8h.01" /></svg>
                时长偏短
              </span>
              <WaveForm :seed="outputFor(index)!.text" :bars="14" :height="16" />
              <span class="warn-text">{{ outputFor(index)!.warning || '时长偏短，建议补停顿或改写后重试' }}</span>
              <span class="acts">
                <AudioPlayer :src="outputFor(index)!.url" :title="outputFor(index)!.text" />
                <button class="alink" type="button" :disabled="synthesis.busy.value" @click="retryLine(index)">补停顿重生成</button>
              </span>
            </div>

            <!-- 生成中：实时进度细条 -->
            <div v-else-if="stateFor(index) === 'generating'" class="line-result">
              <span class="tag mut">生成中</span>
              <span class="gen-progress line-prog"><span :style="{ width: `${synthesis.progress.value}%` }"></span></span>
              <span class="fmeta">{{ synthesis.progress.value }}% · {{ phaseText }}</span>
            </div>

            <!-- 排队中 -->
            <div v-else-if="stateFor(index) === 'queued'" class="line-result">
              <span class="tag mut">排队中</span>
              <span class="fmeta">队列 #{{ queuePos(index) }} · 前一句完成后自动开始</span>
            </div>

            <!-- 合成失败：原因 + 重试 -->
            <div v-else-if="stateFor(index) === 'failed'" class="line-result">
              <span class="tag err">合成失败</span>
              <span class="fail-note">{{ outputFor(index)!.error || '合成失败' }}</span>
              <span class="acts">
                <button class="alink" type="button" :disabled="synthesis.busy.value" @click="retryLine(index)">
                  <svg class="ic sm" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v6h-6" /></svg>
                  重试
                </button>
              </span>
            </div>
          </div>
        </div>
      </div>

      <button class="add-line" type="button" @click="lines.push('')">
        <svg class="ic sm" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>添加一行台词 · 空行与 # 注释行自动跳过
      </button>

      <div v-if="drafts.length" class="drafts-row">
        <span class="drafts-label mono">最近草稿</span>
        <button v-for="draft in drafts.slice(0, 3)" :key="draft.id" class="draft-chip" type="button" :title="draft.text" @click="restoreDraft(draft.text)">
          {{ draft.text.slice(0, 14) }}{{ draft.text.length > 14 ? '…' : '' }}
        </button>
        <button class="alink" type="button" @click="drafts = []">清除</button>
      </div>

      <!-- 生成条 -->
      <div class="gen-bar">
        <span class="gmeta mono">
          {{ speech.length }} 行 · 预计 ≈ {{ estimatedSeconds }}s · {{ engineMeta }}
          <span v-if="pendingCount > 0 && !synthesis.busy.value">· 待生成 {{ pendingCount }}</span>
          <span v-if="synthesis.failedCount.value" class="gfail">· 失败 {{ synthesis.failedCount.value }}</span>
          <button
            v-if="synthesis.readiness.value.status === 'failed'"
            class="mini"
            style="height:22px"
            type="button"
            @click="synthesis.retryWarmup()"
          >重试预热</button>
          <span class="kbd-hint"><kbd>Ctrl</kbd>+<kbd>Enter</kbd></span>
        </span>
        <button class="gen" type="button" :disabled="synthesis.busy.value || !scriptText.trim()" @click="generate">
          {{ synthesis.busy.value ? '生成中…' : '生成' }}
        </button>
      </div>
    </div>

    <!-- 移动端「参数」浮层按钮与遮罩 -->
    <button class="ins-fab" type="button" @click="insOpen = true">参数</button>
    <div class="ins-mask" :class="{ show: insOpen }" @click="insOpen = false"></div>

    <!-- 检查器（D：右栏 296px；≤900px 收进浮层） -->
    <aside class="wb-ins" :class="{ open: insOpen }">
      <InspectorPanel @close="insOpen = false" />
    </aside>
  </div>
</template>

<style scoped>
.wb{display:grid;grid-template-columns:minmax(0,1fr) 296px;align-items:start}
.wb-main{padding:10px 36px 56px 0;min-width:0}
.wb-ins{min-width:0}

/* —— 链接式次级操作（D .alink） —— */
.alink{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;color:var(--text2);border-bottom:1px solid transparent;padding:1px 0;cursor:pointer}
.alink:hover{color:var(--text);border-bottom-color:var(--text)}
.alink:disabled{opacity:.5;cursor:default}
.alink:disabled:hover{color:var(--text2);border-bottom-color:transparent}

/* —— 声音行 —— */
.voice-row{display:flex;align-items:center;gap:14px;padding:18px 0 16px;flex-wrap:wrap}
.voice-chip{display:inline-flex;align-items:center;gap:10px;border:1px solid var(--border2);border-radius:var(--r);padding:8px 12px;background:var(--surface);max-width:100%}
.voice-chip b{font-size:13.5px;font-weight:600;white-space:nowrap}
.voice-meta{font-family:var(--mono);font-size:11px;color:var(--text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag-ink{border-color:var(--text2);color:var(--text);background:transparent}

/* —— 台词头部 —— */
.script-head{display:flex;align-items:baseline;gap:12px;padding:6px 0 10px;flex-wrap:wrap}
.script-head .r{margin-left:auto;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.fmeta{font-family:var(--mono);font-size:11px;color:var(--text3)}

/* —— 台词行（D .line） —— */
.script{padding:0 12px;border-bottom:1px solid var(--border)}
.line{border-top:1px solid var(--border);padding:14px 12px;margin:0 -12px;display:flex;gap:14px}
.line:hover{background:var(--surface2)}
.line .no{font-family:var(--mono);font-size:11.5px;color:var(--text3);width:22px;flex:none;padding-top:3px}
.line .body{flex:1;min-width:0;position:relative}
.line-input{width:100%;border:none;outline:none;background:none;resize:none;font:15px/1.55 var(--font);color:var(--text);overflow:hidden;display:block}

/* —— 语种标签自动补全菜单（纸面风：白底细边直角） —— */
.tag-menu{
  position:absolute;z-index:40;top:100%;left:34px;margin-top:2px;min-width:150px;
  background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
  box-shadow:var(--pop-shadow);padding:4px;display:flex;flex-direction:column;
}
.tag-menu button{display:flex;align-items:center;gap:8px;padding:6px 10px;font-size:12.5px;color:var(--text2);border-radius:var(--rs);text-align:left}
.tag-menu button:hover,.tag-menu button.on{background:var(--surface2);color:var(--text)}
.tag-menu button .mono{font-size:11px;color:var(--text3)}
.tag-menu button.on .mono,.tag-menu button:hover .mono{color:var(--text)}

/* —— 行内结果（D .line-result） —— */
.line-result{margin-top:10px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--text2)}
.line-result .acts{display:flex;align-items:center;gap:14px;margin-left:auto;flex-wrap:wrap}
.line-result .tag.ok{color:var(--text);background:transparent;border-color:var(--text2)}
.line-prog{flex:0 1 240px;min-width:120px;height:3px}
.warn-text{font-size:12px;color:var(--warn)}
.fail-note{font-family:var(--mono);font-size:11.5px;color:var(--err)}

/* —— 添加行 / 草稿 —— */
.add-line{display:flex;align-items:center;gap:8px;width:100%;padding:11px 12px;color:var(--text3);font-size:12px}
.add-line:hover{color:var(--text)}
.drafts-row{display:flex;align-items:center;gap:8px;padding:10px 12px 0;flex-wrap:wrap}
.drafts-label{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;color:var(--text3)}
.draft-chip{font-size:11px;color:var(--text2);border:1px solid var(--border);border-radius:var(--r);padding:2px 9px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.draft-chip:hover{border-color:var(--border2);color:var(--text)}

/* —— 生成条（D .gen-bar / .gen） —— */
.gen-bar{display:flex;align-items:center;gap:20px;margin-top:26px;padding:20px 0 4px;border-top:1px solid var(--border);flex-wrap:wrap}
.gmeta{display:inline-flex;align-items:center;gap:10px;font-family:var(--mono);font-size:11.5px;color:var(--text3);flex-wrap:wrap}
.gfail{color:var(--err)}
.kbd-hint{display:inline-flex;align-items:center;gap:3px}
.gen{
  margin-left:auto;background:var(--primary);color:var(--on-primary);border:1px solid var(--primary);
  border-radius:var(--r);font-size:15.5px;letter-spacing:.3em;padding:15px 60px 15px 66px;flex:none;
}
.gen:hover{background:var(--primary-hover);border-color:var(--primary-hover)}
.gen:disabled{opacity:.5;cursor:default}

/* —— 移动端浮层（≤900px：检查器收进「参数」抽屉） —— */
.ins-fab,.ins-mask{display:none}

@media (max-width:900px){
  .wb{grid-template-columns:1fr}
  .wb-main{padding:8px 0 56px}
  .script-head .r{margin-left:0}
  .gen{padding:13px 34px 13px 40px}
  .ins-fab{
    display:inline-flex;align-items:center;justify-content:center;position:fixed;right:16px;bottom:16px;z-index:56;
    height:40px;padding:0 20px;background:var(--primary);color:var(--on-primary);border-radius:var(--r);
    font-size:13px;letter-spacing:.24em;box-shadow:var(--pop-shadow);
  }
  .ins-mask{display:block;position:fixed;inset:0;z-index:78;background:rgba(25,24,23,.38);opacity:0;pointer-events:none;transition:opacity .18s ease}
  .ins-mask.show{opacity:1;pointer-events:auto}
  .wb-ins{
    position:fixed;top:0;right:0;bottom:0;width:min(320px,88vw);z-index:80;background:var(--bg);
    border-left:1px solid var(--border);transform:translateX(102%);transition:transform .2s ease;overflow-y:auto;
  }
  .wb-ins.open{transform:none}
}
</style>
