<script setup lang="ts">
/**
 * 工作台右侧「检查器」（设计稿 D：单屏极简 · 浅色纸面）。
 *
 * 五段结构：声音卡 / 语言 / 广普程度 / 参数预设 / 高级参数。
 * 全局单例 composables（useConfig / useVoices / useReferenceAudio / usePage）
 * 在这里直接复用，与主视图共享同一份状态；高级参数是 mono 数值输入
 * （右对齐、无边框、focus 时 1px 底线），改动仍走 useConfig 的 650ms
 * 防抖自动保存。移动端（≤900px）整体收进浮层，close 事件由主视图接。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ACCENT_OPTIONS, LANGUAGE_OPTIONS, useConfig } from '@/composables/useConfig'
import { usePage } from '@/composables/usePage'
import { useReferenceAudio } from '@/composables/useReferenceAudio'
import { useVoices } from '@/composables/useVoices'

defineOptions({ name: 'InspectorPanel' })

const emit = defineEmits<{ close: [] }>()

const { config, saving, savedAt } = useConfig()
const { setPage } = usePage()
const voices = useVoices()
const reference = useReferenceAudio()

const currentProfile = computed(() => voices.profileById(config.voice_profile_id || 'current'))
const currentCard = computed(
  () => voices.cards.value.find((item) => item.voice_id === config.voice_profile_id) ?? null,
)
const voiceName = computed(
  () => currentCard.value?.display_name || voices.displayName(currentProfile.value),
)
const refOk = computed(() => Boolean(config.ref_audio?.trim()))
const refBasename = computed(() => (config.ref_audio || '').split(/[\\/]/).pop() || '未配置')
const pairMismatch = computed(() => reference.pair.value?.status === 'mismatched')

const saveHint = computed(() => {
  if (saving.value) return '保存中…'
  if (savedAt.value) return '已自动保存'
  return ''
})

// —— 语言：「自动」只是显示态（后端 prompt_lang 恒为具体语种，自动即默认 zh），
// 行内 <zh>/<en>/<yue>/<ja>/<ko> 标记始终优先。 ——
const LANG_SEG = [{ key: 'auto', label: '自动' }, ...LANGUAGE_OPTIONS.map((item) => ({ key: item.value, label: item.label }))]
const autoLang = ref(true)
// 分区说明折叠：标题右侧 ⓘ，点开才显示小字说明（语言 / 广普程度）。
const infoOpen = ref<{ lang: boolean; accent: boolean }>({ lang: false, accent: false })

// 点空白处关闭说明气泡：点在 ⓘ 或气泡内不关（气泡内文字可选）。
function onDocumentClick(event: MouseEvent) {
  const target = event.target as HTMLElement | null
  if (target?.closest('.info-btn') || target?.closest('.info-pop')) return
  infoOpen.value = { lang: false, accent: false }
}
onMounted(() => document.addEventListener('click', onDocumentClick))
onUnmounted(() => document.removeEventListener('click', onDocumentClick))
const activeLangKey = computed(() => (autoLang.value ? 'auto' : config.prompt_lang))
function setLang(key: string) {
  if (key === 'auto') {
    autoLang.value = true
    return
  }
  autoLang.value = false
  config.prompt_lang = key as typeof config.prompt_lang
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

// —— 高级参数：mono 数值输入，提交（change/blur/Enter）时夹紧到合法区间，
// 落到 config 后由 useConfig 的 650ms 防抖自动保存。 ——
const PARAM_ROWS = computed(() => ([
  { key: 'speed', label: '语速', min: 0.5, max: 3, step: 0.05 },
  { key: 'pause_second', label: '句间停顿', min: 0, max: 2, step: 0.05 },
  { key: 'temperature', label: '采样温度', min: 0.05, max: 2, step: 0.05 },
  { key: 'sample_steps', label: '采样步数', min: 8, max: 128, step: 1 },
  { key: 'top_p', label: 'top_p', min: 0.05, max: 1, step: 0.01 },
] as const))

function setParam(row: (typeof PARAM_ROWS.value)[number], event: Event) {
  const target = event.target as HTMLInputElement
  const value = Number(target.value)
  if (!Number.isFinite(value)) return
  const clamped = Math.min(row.max, Math.max(row.min, value))
  Object.assign(config, { [row.key]: clamped })
  target.value = String(clamped)
}

function resetParams() {
  applyPreset('clear')
}
</script>

<template>
  <aside class="ins">
    <div class="ins-head">
      <b>检查器</b>
      <span class="r mono">{{ saveHint }}</span>
      <button class="ins-close" type="button" aria-label="收起参数" @click="emit('close')">✕</button>
    </div>

    <!-- 声音卡 -->
    <div class="ins-sec">
      <span class="lbl">声音卡</span>
      <div class="kv-row">
        <b class="ins-name">{{ voiceName }}</b>
        <span class="tag" :class="refOk ? 'tag-ink' : 'warn'">{{ refOk ? '可用' : '未配参考音' }}</span>
        <span v-if="currentCard" class="v">{{ currentCard.active_version }} · {{ currentCard.version_count }} 版</span>
      </div>
      <div class="kv-row"><span class="k">参考音</span><span class="v" :title="config.ref_audio">{{ refOk ? refBasename : '未配置 · 去声音库建档' }}</span></div>
      <div v-if="pairMismatch" class="pair-note">参考音与参考文本不匹配：{{ reference.pair.value?.message }}</div>
      <button class="alink" type="button" @click="setPage('voices')">在声音库中查看 →</button>
    </div>

    <!-- 语言 -->
    <div class="ins-sec">
      <div class="sec-head">
        <span class="lbl">语言</span>
        <button class="info-btn" :class="{ on: infoOpen.lang }" type="button" title="说明" aria-label="语言说明" @click="infoOpen.lang = !infoOpen.lang">
          <svg class="ic sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 10.6v5.6" /><path d="M12 7.6h.01" /></svg>
        </button>
        <div v-if="infoOpen.lang" class="info-pop">行内 &lt;zh&gt; / &lt;en&gt; / &lt;yue&gt; / &lt;ja&gt; / &lt;ko&gt; 标记优先；台词区输入 &lt; 会自动弹出语种标签菜单。</div>
      </div>
      <div class="seg">
        <button v-for="item in LANG_SEG" :key="item.key" type="button" :class="{ on: activeLangKey === item.key }" @click="setLang(item.key)">{{ item.label }}</button>
      </div>
    </div>

    <!-- 广普程度 -->
    <div class="ins-sec">
      <div class="sec-head">
        <span class="lbl">广普程度</span>
        <button class="info-btn" :class="{ on: infoOpen.accent }" type="button" title="说明" aria-label="广普程度说明" @click="infoOpen.accent = !infoOpen.accent">
          <svg class="ic sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 10.6v5.6" /><path d="M12 7.6h.01" /></svg>
        </button>
        <div v-if="infoOpen.accent" class="info-pop">只影响读音，不动音色；「目的」「的确」等固定搭配始终受保护。句子里需含触发词（是/的/没有/什么等）才会产生替换。</div>
      </div>
      <div class="seg">
        <button v-for="item in ACCENT_SEG" :key="item.key" type="button" :class="{ on: config.accent_level === item.key }" @click="config.accent_level = item.key as typeof config.accent_level">{{ item.label }}</button>
      </div>
    </div>

    <!-- 参数预设 -->
    <div class="ins-sec">
      <span class="lbl">参数预设</span>
      <button v-for="(preset, key) in PRESETS" :key="key" class="orow" :class="{ on: activePreset === key }" type="button" @click="applyPreset(key)">
        <span class="dot"></span>
        <span class="ot"><b>{{ preset.label }}</b></span>
        <span class="d">{{ preset.desc }}</span>
      </button>
    </div>

    <!-- 高级参数：滑杆 + 上下限可见（直接可调，拖动即改，650ms 防抖保存） -->
    <div class="ins-sec">
      <span class="lbl">高级参数 <span class="tag mut">拖动滑杆或输入数值</span></span>
      <div v-for="row in PARAM_ROWS" :key="row.key" class="adv-row" :title="`${row.label}：${row.min} – ${row.max}`">
        <div class="adv-top">
          <span class="k">{{ row.label }}</span>
          <input
            class="num"
            type="number"
            :min="row.min"
            :max="row.max"
            :step="row.step"
            :value="Number(config[row.key as keyof typeof config])"
            @change="setParam(row, $event)"
          >
        </div>
        <div class="adv-slider">
          <span class="bound">{{ row.min }}</span>
          <input
            class="range"
            type="range"
            :min="row.min"
            :max="row.max"
            :step="row.step"
            :value="Number(config[row.key as keyof typeof config])"
            :aria-label="`${row.label}（${row.min} – ${row.max}）`"
            @input="setParam(row, $event)"
          >
          <span class="bound">{{ row.max }}</span>
        </div>
      </div>
      <div class="kv-row"><span class="k">读回选句</span><span class="v">引擎自动</span></div>
      <div class="adv-foot">
        <button class="alink" type="button" @click="resetParams">恢复默认（清晰优先）</button>
      </div>
      <p class="ins-note">改动自动保存 · 仅对本项目生效</p>
    </div>
  </aside>
</template>

<style scoped>
/* 链接式次级操作（D .alink） */
.alink{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;color:var(--text2);border-bottom:1px solid transparent;padding:1px 0;cursor:pointer}
.alink:hover{color:var(--text);border-bottom-color:var(--text)}

.ins{border-left:1px solid var(--border);padding:22px 4px 48px 32px}
.ins-head{display:flex;align-items:baseline;gap:10px;padding-bottom:12px}
.ins-head b{font-size:14px;font-weight:600}
.ins-head .r{margin-left:auto;font-family:var(--mono);font-size:10.5px;color:var(--text3)}
.ins-close{display:none;width:26px;height:26px;place-items:center;color:var(--text3);border:1px solid var(--border);border-radius:var(--r);flex:none;align-self:center}
.ins-close:hover{color:var(--text);border-color:var(--border2)}

.ins-sec{border-top:1px solid var(--border);padding:14px 0}
.ins-sec .lbl{display:block;padding-bottom:10px}
.sec-head{display:flex;align-items:center;padding-bottom:10px;position:relative}
.sec-head .lbl{flex:1;padding-bottom:0}
.info-btn{width:22px;height:22px;display:grid;place-items:center;color:var(--text3);border:none;background:none;border-radius:var(--rs);cursor:pointer;padding:0}
.info-btn:hover{color:var(--text);background:var(--surface2)}
.info-btn.on{color:var(--text)}
.info-btn .ic{width:14px;height:14px}
/* ⓘ 悬浮说明气泡：绝对定位，不挤压布局 */
.info-pop{
  position:absolute;top:calc(100% + 2px);right:0;z-index:50;width:max-content;max-width:250px;
  background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
  box-shadow:var(--pop-shadow);padding:8px 10px;text-align:left;
  font-size:11.5px;line-height:1.6;color:var(--text2);
}
.ins-name{font-size:13px;color:var(--text);font-weight:600}
.kv-row{display:flex;align-items:center;gap:10px;padding:3px 0;font-size:12.5px;color:var(--text2)}
.kv-row .k{font-family:var(--mono);font-size:11px;color:var(--text3);flex:none}
.kv-row .v{margin-left:auto;font-family:var(--mono);font-size:11.5px;color:var(--text);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:168px}
.tag-ink{border-color:var(--text2);color:var(--text);background:transparent}
.pair-note{font-size:11.5px;color:var(--warn);background:var(--warn-bg);border:1px solid var(--warn-bd);border-radius:var(--r);padding:6px 8px;margin:6px 0;line-height:1.6}
.ins-note{font-family:var(--mono);font-size:10.5px;color:var(--text3);padding-top:8px;line-height:1.6}

/* 参数预设（D .opt：无边框单选行） */
.orow{display:flex;align-items:baseline;gap:9px;padding:5px 0;font-size:12.5px;width:100%;text-align:left;cursor:pointer}
.orow .dot{width:10px;height:10px;border:1px solid var(--border2);border-radius:50%;flex:none;position:relative;top:2px;background:var(--surface)}
.orow.on .dot{border-color:var(--text)}
.orow.on .dot::after{content:"";position:absolute;inset:2px;background:var(--text);border-radius:50%}
.orow .ot b{font-weight:600;color:var(--text)}
.orow:not(.on) .ot b{font-weight:400;color:var(--text2)}
.orow .d{margin-left:auto;font-family:var(--mono);font-size:10.5px;color:var(--text3);text-align:right}

/* 高级参数：滑杆行（上=label+数值输入，下=min 滑杆 max） */
.adv-row{padding:5px 0 7px}
.adv-top{display:flex;align-items:center;gap:10px}
.adv-top .k{font-family:var(--mono);font-size:11px;color:var(--text3);flex:none}
.adv-slider{display:flex;align-items:center;gap:7px;margin-top:3px}
.adv-slider .bound{font-family:var(--mono);font-size:9.5px;color:var(--text3);flex:none;min-width:16px}
.adv-slider .bound:last-child{text-align:right}
.range{flex:1;appearance:none;-webkit-appearance:none;height:3px;background:var(--surface3);border:1px solid var(--border);border-radius:1px;outline:none;cursor:pointer;padding:0}
.range::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:11px;height:11px;background:var(--text);border:none;border-radius:2px;cursor:pointer}
.range::-moz-range-thumb{width:11px;height:11px;background:var(--text);border:none;border-radius:2px;cursor:pointer}
.range:focus-visible{outline:2px solid var(--text2);outline-offset:2px}

/* 高级参数：mono 数值输入（右对齐、无边框、focus 1px 底线） */
.num{
  margin-left:auto;width:76px;flex:none;text-align:right;font-family:var(--mono);font-size:11.5px;font-variant-numeric:tabular-nums;
  color:var(--text);background:none;border:none;border-bottom:1px solid transparent;border-radius:0;outline:none;padding:1px 0;
}
.num:focus-visible{outline:none;border-radius:0}
.num:focus{border-bottom-color:var(--text2)}
.num::-webkit-inner-spin-button,.num::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.num{-moz-appearance:textfield;appearance:textfield}
.adv-foot{padding-top:8px}

@media (max-width:900px){
  .ins{border-left:none;padding:14px 16px 32px;height:100%}
  .ins-close{display:grid}
}
</style>
