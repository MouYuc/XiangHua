<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { hexToRgb, mixColor, particleGroup, resolveOrbColors, resolvePlaybackToken, type OrbColorInput } from './orbMotion'

type OrbToken = {
  outputIndex: number
  tokenIndex: number
  semanticFrameRate?: number
}

type OrbPlayback = {
  outputIndex: number
  currentTime: number
  duration: number
  tokenCount: number
  frameRate?: number
  speed?: number
  paused?: boolean
}

type Particle = {
  group: number
  sphere: { x: number; y: number; z: number; angle: number; latitude: number }
  loose: { x: number; y: number; z: number }
  twinkle: number
  size: number
}

type Wave = {
  group: number
  serial: number
  source: 'synthesis' | 'playback'
  age: number
  longitude: number
  latitude: number
  direction: number
  width: number
}

const props = withDefaults(defineProps<{
  active?: boolean
  total?: number
  completedOutputIndexes?: number[]
  failedOutputIndexes?: number[]
  semanticToken?: OrbToken | null
  playback?: OrbPlayback | null
  colors?: OrbColorInput
  label?: string
}>(), {
  active: false,
  total: 0,
  completedOutputIndexes: () => [],
  failedOutputIndexes: () => [],
  label: '声音状态',
})

const canvas = ref<HTMLCanvasElement | null>(null)
const TAU = Math.PI * 2
const particles: Particle[] = []
const waves: Wave[] = []
const settledAt = new Map<number, number>()
let context: CanvasRenderingContext2D | null = null
let animation = 0
let resizeObserver: ResizeObserver | undefined
let width = 0
let height = 0
let lastFrame = performance.now()
let lastSemanticKey = ''
let lastPlaybackKey = ''
let activePlaybackOutput: number | null = null
let initialized = false

const completedGroups = computed(() => new Set(props.completedOutputIndexes.filter((index) => index >= 0)))
const failedGroups = computed(() => new Set(props.failedOutputIndexes.filter((index) => index >= 0)))
const phase = computed(() => {
  if (props.playback && !props.playback.paused) return 'playback'
  if (props.active) return 'synthesis'
  if (props.total > 0 && completedGroups.value.size >= props.total) return 'done'
  return 'idle'
})
const phaseTitle = computed(() => ({
  idle: props.label,
  synthesis: '语义 token 正在驱动粒子',
  playback: '音频播放 token 响应中',
  done: '合成完成，可逐条试听',
}[phase.value]))
const phaseDetail = computed(() => {
  if (phase.value === 'playback' && props.playback) return `第 ${props.playback.outputIndex + 1} 条音频正在唤醒对应粒子`
  if (phase.value === 'synthesis') return `${completedGroups.value.size} / ${props.total} 条已聚合，当前 token 只点亮散点`
  if (phase.value === 'done') return `${completedGroups.value.size} / ${props.total} 条已完成`
  return '等待合成任务'
})

function randomSphere() {
  const y = Math.random() * 2 - 1
  const angle = Math.random() * TAU
  const radius = Math.sqrt(1 - y * y)
  return { x: Math.cos(angle) * radius, y, z: Math.sin(angle) * radius, angle, latitude: Math.asin(y) }
}

function buildParticles() {
  particles.length = 0
  for (let index = 0; index < 720; index += 1) {
    const sphere = randomSphere()
    const burst = 1.48 + Math.random() * 1.28
    const group = particleGroup(index, props.total)
    particles.push({
      group,
      sphere,
      loose: { x: sphere.x * burst + (Math.random() - .5) * .38, y: sphere.y * burst + (Math.random() - .5) * .38, z: sphere.z * burst },
      twinkle: Math.random() * TAU,
      size: .66 + Math.random() * 1.45,
    })
  }
}

function resize() {
  if (!canvas.value || !context) return
  const ratio = Math.min(window.devicePixelRatio || 1, 2)
  width = canvas.value.clientWidth || 260
  height = canvas.value.clientHeight || 170
  canvas.value.width = Math.floor(width * ratio)
  canvas.value.height = Math.floor(height * ratio)
  context.setTransform(ratio, 0, 0, ratio, 0, 0)
}

function mix(from: { x: number; y: number; z: number }, to: { x: number; y: number; z: number }, amount: number) {
  return { x: from.x + (to.x - from.x) * amount, y: from.y + (to.y - from.y) * amount, z: from.z + (to.z - from.z) * amount }
}

function ease(value: number) {
  const t = Math.min(1, Math.max(0, value))
  return 1 - Math.pow(1 - t, 3)
}

function wrap(value: number) {
  return Math.atan2(Math.sin(value), Math.cos(value))
}

function project(point: { x: number; y: number; z: number }, spin: number, scale: number) {
  const x = point.x * Math.cos(spin) - point.z * Math.sin(spin)
  const z = point.x * Math.sin(spin) + point.z * Math.cos(spin)
  const depth = 1 / (3.72 - z)
  const size = Math.min(width * .45, height * .87) * scale
  return { x: width * .5 + x * size * depth, y: height * .57 + point.y * size * depth, z, depth }
}

function emitWave(group: number, serial: number, source: Wave['source']) {
  if (group < 0 || group >= props.total || failedGroups.value.has(group)) return
  waves.push({
    group,
    serial,
    source,
    age: 0,
    longitude: serial * 1.47 + group * .81,
    latitude: Math.sin(serial * .83 + group) * .52,
    direction: serial % 2 ? 1 : -1,
    width: .53 + (serial % 3) * .06,
  })
  if (waves.length > 20) waves.shift()
}

function syncSettlements(initial = false) {
  const now = performance.now()
  for (const group of completedGroups.value) {
    if (!settledAt.has(group)) settledAt.set(group, initial ? now - 1400 : now)
  }
}

function syncSemanticToken() {
  const token = props.semanticToken
  if (!token || completedGroups.value.has(token.outputIndex)) return
  const key = `${token.outputIndex}:${token.tokenIndex}`
  if (key === lastSemanticKey) return
  lastSemanticKey = key
  emitWave(token.outputIndex, token.tokenIndex, 'synthesis')
}

function syncPlayback() {
  const playback = props.playback
  if (!playback || playback.paused) return
  if (activePlaybackOutput !== playback.outputIndex) {
    for (let index = waves.length - 1; index >= 0; index -= 1) {
      if (waves[index].source === 'playback') waves.splice(index, 1)
    }
    lastPlaybackKey = ''
    activePlaybackOutput = playback.outputIndex
  }
  const tokenIndex = resolvePlaybackToken({
    currentTime: playback.currentTime,
    duration: playback.duration,
    tokenCount: playback.tokenCount,
    frameRate: playback.frameRate,
    speed: playback.speed,
  })
  const key = `${playback.outputIndex}:${tokenIndex}`
  if (key === lastPlaybackKey) return
  lastPlaybackKey = key
  emitWave(playback.outputIndex, tokenIndex + 1000, 'playback')
}

function draw(time: number) {
  if (!context) return
  const delta = Math.min(70, time - lastFrame)
  lastFrame = time
  syncSettlements()
  syncSemanticToken()
  syncPlayback()
  for (const wave of waves) wave.age += delta
  while (waves.length && waves[0].age > 1450) waves.shift()

  const colors = resolveOrbColors(props.colors || {})
  const synthesisColor = hexToRgb(colors.synthesisToken)
  const settledColor = hexToRgb(colors.settled)
  const playbackColor = hexToRgb(colors.playbackToken)
  const allSettled = props.total > 0 && completedGroups.value.size >= props.total
  const spin = time * .00027
  const breath = allSettled ? 1 + Math.sin(time * .0017) * .038 : 1
  context.clearRect(0, 0, width, height)

  for (const particle of particles) {
    const settledAmount = ease((time - (settledAt.get(particle.group) || Number.POSITIVE_INFINITY)) / 1300)
    const source = mix(particle.loose, particle.sphere, settledAmount)
    const point = project(source, spin, breath)
    let synthesisEnergy = 0
    let playbackEnergy = 0
    if (point.z > -.6) {
      for (const wave of waves) {
        if (wave.group !== particle.group) continue
        const travel = wave.age * .00245 * wave.direction
        const longitudeGap = Math.abs(wrap(particle.sphere.angle - wave.longitude - travel))
        const latitudeGap = Math.abs(particle.sphere.latitude - (wave.latitude + Math.sin(wave.age * .003) * .11))
        const energy = Math.max(0, 1 - longitudeGap / wave.width) * Math.max(0, 1 - latitudeGap / .35) * Math.pow(Math.max(0, 1 - wave.age / 1450), .68)
        if (wave.source === 'playback') playbackEnergy += energy
        else synthesisEnergy += energy
      }
    }
    synthesisEnergy = Math.min(1, synthesisEnergy)
    playbackEnergy = Math.min(1, playbackEnergy)
    const energy = Math.max(synthesisEnergy, playbackEnergy)
    const base = allSettled ? (Math.sin(point.z * 6 + particle.twinkle * 1.7 - time * .0028) + 1) / 2 : .17 + settledAmount * .22
    const quiet = phase.value === 'playback' && props.playback && particle.group !== props.playback.outputIndex ? .16 : 0
    const alpha = Math.min(1, Math.max(.12, .43 + base * .33 + settledAmount * .12 + energy * .2 - quiet))
    const radius = particle.size * (1.1 + point.depth * 1.88) * (1 + base * .12 + energy * .62)
    let particleColor = mixColor([57, 160, 120], settledColor, settledAmount * .82)
    particleColor = mixColor(particleColor, synthesisColor, synthesisEnergy)
    particleColor = mixColor(particleColor, playbackColor, playbackEnergy)
    if (energy > .34) {
      const glow = playbackEnergy > synthesisEnergy ? playbackColor : synthesisColor
      context.save()
      context.shadowColor = `rgba(${glow[0]}, ${glow[1]}, ${glow[2]}, ${energy})`
      context.shadowBlur = 17 * energy
    }
    context.beginPath()
    context.fillStyle = `rgba(${Math.round(particleColor[0])}, ${Math.round(particleColor[1])}, ${Math.round(particleColor[2])}, ${alpha})`
    context.arc(point.x, point.y, radius, 0, TAU)
    context.fill()
    if (energy > .34) context.restore()
  }

  if (allSettled) {
    const ringColor = phase.value === 'playback' ? playbackColor : settledColor
    const ring = Math.min(width, height) * (.224 + Math.sin(time * .0016) * .009)
    context.strokeStyle = `rgba(${ringColor[0]}, ${ringColor[1]}, ${ringColor[2]}, .25)`
    context.lineWidth = 1
    context.beginPath()
    context.arc(width * .5, height * .57, ring, 0, TAU)
    context.stroke()
  }
  animation = requestAnimationFrame(draw)
}

watch(() => props.total, () => {
  settledAt.clear()
  waves.length = 0
  lastSemanticKey = ''
  lastPlaybackKey = ''
  activePlaybackOutput = null
  buildParticles()
  syncSettlements(!initialized)
})
watch(completedGroups, () => syncSettlements())
watch(() => props.semanticToken, syncSemanticToken)
watch(() => props.playback, syncPlayback, { deep: true })

onMounted(() => {
  if (!canvas.value) return
  context = canvas.value.getContext('2d')
  if (!context) return
  buildParticles()
  syncSettlements(true)
  initialized = true
  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(canvas.value)
  window.addEventListener('resize', resize)
  resize()
  animation = requestAnimationFrame(draw)
})

onBeforeUnmount(() => {
  cancelAnimationFrame(animation)
  resizeObserver?.disconnect()
  window.removeEventListener('resize', resize)
  context = null
})
</script>

<template>
  <div class="orb-card" :class="'orb-phase-' + phase" :data-phase="phase" :data-completed="completedGroups.size">
    <div class="orb-copy"><span class="orb-eyebrow">LIVE AUDIO / {{ phase.toUpperCase() }}</span><strong>{{ phaseTitle }}</strong><small>{{ phaseDetail }}</small></div>
    <canvas ref="canvas" aria-label="合成与播放实时粒子球" role="img"></canvas>
    <span class="orb-dot"></span>
  </div>
</template>
