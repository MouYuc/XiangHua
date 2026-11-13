<script setup lang="ts">
/**
 * 工作台右侧「声音检查器」：参考音状态、语言/口音档位、参数预设与生成按钮。
 *
 * 从 WorkbenchView.vue 原样抽出（布局、样式值与文案不变）。全局单例
 * composables（useConfig / useVoices / useReferenceAudio / useSynthesis /
 * usePage）在这里直接复用，与主视图共享同一份状态；台词画布侧的编排
 * （台词行状态、待生成句数、generate 入口）通过 props / emit 传入。
 */
import { computed, ref } from 'vue'
import { ACCENT_OPTIONS, LANGUAGE_OPTIONS, useConfig } from '@/composables/useConfig'
import { usePage } from '@/composables/usePage'
import { useReferenceAudio } from '@/composables/useReferenceAudio'
import { useSynthesis } from '@/composables/useSynthesis'
import { useVoices } from '@/composables/useVoices'

defineOptions({ name: 'InspectorPanel' })

const props = defineProps<{
  /** 待生成句数（空行/注释行除外，由台词画布统计）。 */
  pendingCount: number
  /** 有效台词总句数（同上，由台词画布统计）。 */
  speechCount: number
}>()

const emit = defineEmits<{ generate: [] }>()

const { config } = useConfig()
const { setPage } = usePage()
const voices = useVoices()
const synthesis = useSynthesis()
const reference = useReferenceAudio()

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
</script>

<template>
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
      <button class="btn-primary" style="width:100%" :disabled="synthesis.busy.value" @click="emit('generate')">
        <svg class="ic sm" viewBox="0 0 24 24" style="fill:currentColor;stroke:none"><path d="M8 5.5v13l11-6.5z" /></svg>
        {{ synthesis.busy.value ? '生成中…' : `生成 ${pendingCount || speechCount} 句` }}
      </button>
      <p class="ins-gen-hint">
        已完成 {{ synthesis.outputs.value.filter((item) => !item.error).length }} 句 · 显卡{{ synthesis.busy.value ? '占用中' : '空闲' }}
      </p>
    </div>
  </aside>
</template>

<style scoped>
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
  .inspector{position:static;height:auto;border-top:1px solid var(--border)}
}
@media (max-width:700px){
  .inspector{padding:0}
}
</style>
