<script setup lang="ts">
import { computed, ref } from 'vue'
import { createVoiceCard } from '@/lib/api'
import { useConfig } from '@/composables/useConfig'
import { usePage } from '@/composables/usePage'
import { useReferenceAudio } from '@/composables/useReferenceAudio'
import { useVoices } from '@/composables/useVoices'
import { referenceQualityFailed } from '@/composables/referenceAudio'
import { useToast } from '@/composables/useToast'
import ReferenceAudioReport from '@/components/ReferenceAudioReport.vue'

defineOptions({ name: 'VoiceWizardView' })

const { config } = useConfig()
const { setPage } = usePage()
const reference = useReferenceAudio()
const voices = useVoices()
const toast = useToast()

const fileInput = ref<HTMLInputElement | null>(null)
const voiceName = ref('')
const saving = ref(false)
const confirmed = ref(false)

const step = computed<1 | 2 | 3 | 4>(() => {
  // 步进只由本次向导的上传/质检状态驱动。配置里永远有 ref_audio（「当前音色」也靠它），
  // 不能把它当作"本次已建档"——否则向导打开就直接跳到第 4 步，把旧参考音当成新素材。
  if (reference.uploading.value) return 2
  if (reference.report.value) return confirmed.value ? 4 : 3
  return 1
})

const steps = [
  { key: 1, label: '选择录音' },
  { key: 2, label: '自动处理' },
  { key: 3, label: '质检确认' },
  { key: 4, label: '命名完成' },
]

function pick() {
  fileInput.value?.click()
}

function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  confirmed.value = false
  voiceName.value = voiceName.value || file.name.replace(/\.[^.]+$/, '')
  void reference.upload(file)
  input.value = ''
}

// 质检未达标只提示不拦截：设备参差是常态，用户看了报告自己决定继续还是重录。
const qualityFailed = computed(() => Boolean(reference.report.value && referenceQualityFailed(reference.report.value)))
const refText = computed({
  get: () => config.ref_text,
  set: (value: string) => {
    config.ref_text = value
  },
})

async function finish() {
  const name = voiceName.value.trim()
  if (!name) {
    toast.error('先给这个声音起个名字。')
    return
  }
  if (!config.ref_audio) {
    toast.error('还没有完成录音处理。')
    return
  }
  saving.value = true
  try {
    const data = await createVoiceCard({
      display_name: name,
      language: config.prompt_lang,
    })
    await voices.loadVoiceOptions()
    toast.success(data.message)
    setPage('workbench')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="flow">
    <button class="backlink" type="button" @click="setPage('voices')">
      <svg class="ic sm" viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6" /></svg>返回声音库
    </button>

    <div class="page-h" style="margin-bottom:10px">
      <div>
        <span class="lbl pg-title">从录音建档声音</span>
        <p class="sub">你只需确认质检结果并命名，其余全自动。请确认素材来源合法合规。</p>
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

    <!-- 第 1 步：选择录音 -->
    <div v-if="step === 1" class="dropzone">
      <svg class="ic" viewBox="0 0 24 24" style="width:26px;height:26px;color:var(--text3)"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><path d="M12 19v3" /></svg>
      <h3>上传一段自然说话的录音</h3>
      <p class="d">访谈、播客、会议、语音备忘都行 · wav / mp3 / m4a / flac / ogg · ≤ 100 MB<br>5-15 分钟效果更好；系统自动切片、质检并转写，不需要照稿补录。</p>
      <button class="btn-primary" style="margin-top:12px" type="button" @click="pick">选择文件</button>
    </div>

    <!-- 第 2 步：自动处理 -->
    <div v-else-if="step === 2" class="panel">
      <h3>自动处理中 <span class="tag mut">{{ reference.uploadLabel.value || '准备中' }}</span></h3>
      <p class="ph">上传后全自动：提取音轨 → 切片 → 质检 → 转写。这一步不需要你做任何事。</p>
      <div style="display:flex;align-items:center;gap:9px;margin-top:12px">
        <span class="gen-progress" style="height:4px"><span :style="{ width: `${Math.round(reference.progress.value * 100)}%` }"></span></span>
        <span class="mono" style="font-size:10.5px;color:var(--text3)">{{ Math.round(reference.progress.value * 100) }}%</span>
      </div>
    </div>

    <!-- 第 3 步：质检确认 -->
    <template v-else-if="step === 3">
      <div class="panel">
        <h3>质检结果</h3>
        <p class="ph">系统从切片中选出最清晰的片段作为参考音，以下是客观检测。</p>
        <div style="margin-top:8px">
          <ReferenceAudioReport :report="reference.report.value" />
        </div>
      </div>
      <div class="panel" :class="{ 'panel-warn': qualityFailed }">
        <h3>参考文本 <span class="tag mut">自动转写 · 可修正错字</span></h3>
        <p class="ph">参考文本与参考音频必须匹配，合成前会自动校验配对。</p>
        <div class="input-line" style="margin-top:9px">
          <input v-model="refText" placeholder="参考文本">
        </div>
        <div style="display:flex;gap:8px;margin-top:12px;align-items:center">
          <button class="btn-primary" type="button" @click="confirmed = true">确认使用这段录音</button>
          <button class="btn-secondary" type="button" @click="pick">换一段录音</button>
          <span v-if="qualityFailed" style="font-size:11.5px;color:var(--warn)">质检未达标：可直接使用；介意音质可换一段更干净的录音。</span>
        </div>
      </div>
    </template>

    <!-- 第 4 步：命名完成 -->
    <div v-else class="panel">
      <h3>命名并完成</h3>
      <p class="ph">建档完成后即可在工作台使用；之后随时可以进工作坊训练微调。</p>
      <div class="input-line" style="margin-top:9px">
        <input v-model="voiceName" placeholder="声音名称">
        <button class="btn-primary" type="button" :disabled="saving" @click="finish">完成建档</button>
      </div>
      <p style="font-size:11px;color:var(--text3);margin-top:8px">
        参考音：{{ config.ref_audio.split(/[\\/]/).pop() }}
      </p>
    </div>

    <input ref="fileInput" type="file" accept="audio/*,.wav,.mp3,.m4a,.flac,.ogg" style="display:none" @change="onFile">
  </div>
</template>

<style scoped>
.flow{max-width:640px;margin:0 auto;padding:20px 20px 50px}
.panel-warn{border-color:var(--warn-bd)}
/* 皮肤对齐 design/D-focus.html：标题与分区标题用 .lbl（mono 11px），面板直角 2px。 */
.pg-title{font-size:12px;color:var(--text2);display:block;padding-bottom:2px}
.page-h .sub{font-family:var(--mono);font-size:11px;color:var(--text3);letter-spacing:.04em;margin-top:3px}
.panel h3{font-family:var(--mono);font-size:11px;font-weight:400;letter-spacing:.14em;color:var(--text3)}
.panel h3 b,.panel h3 .tag{letter-spacing:normal}
</style>
