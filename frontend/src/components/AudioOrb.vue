<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { resolveOrbAssemblyProgress, resolveOrbChargeProgress, resolveOrbPhase, resolveOrbPresentationPhase, type OrbPhase } from './orbMotion'

const props = withDefaults(defineProps<{
  active?: boolean
  status?: string
  phase?: string
  completed?: number
  total?: number
  progress?: number
  label?: string
}>(), { active: false, status: '', phase: '', completed: 0, total: 0, label: '声音状态' })

type Particle = {
  sphere: { x: number; y: number; z: number }
  loose: { x: number; y: number; z: number }
  twinkle: number
  size: number
}

const canvas = ref<HTMLCanvasElement | null>(null)
const sourcePhase = computed<OrbPhase>(() => resolveOrbPhase(props))
const phase = ref<OrbPhase>('idle')
const progress = computed(() => resolveOrbChargeProgress(props))
const displayProgress = ref(1)
const phaseTitle = computed(() => ({ idle: props.label, explode: '粒子正在展开', fill: '正在填充声场', done: '合成完成' })[phase.value])
const phaseDetail = computed(() => ({ idle: '等待合成任务', explode: '所有粒子离开球心', fill: String(props.completed) + ' / ' + String(props.total) + ' 句已完成', done: '蓝色粒子球已就绪' })[phase.value])

const TAU = Math.PI * 2
const particles: Particle[] = []
let context: CanvasRenderingContext2D | null = null
let animation = 0
let resizeObserver: ResizeObserver | undefined
let width = 0
let height = 0
let visualProgress = 1
let lastSourcePhase: OrbPhase = 'idle'

function randomSphere() {
  const y = Math.random() * 2 - 1
  const angle = Math.random() * TAU
  const radius = Math.sqrt(1 - y * y)
  return { x: Math.cos(angle) * radius, y, z: Math.sin(angle) * radius }
}

function buildParticles() {
  for (let index = 0; index < 620; index += 1) {
    const sphere = randomSphere()
    const burst = 1.35 + Math.random() * 1.35
    particles.push({
      sphere,
      loose: { x: sphere.x * burst + (Math.random() - .5) * .35, y: sphere.y * burst + (Math.random() - .5) * .35, z: sphere.z * burst },
      twinkle: Math.random() * TAU,
      size: .7 + Math.random() * 1.5,
    })
  }
}

function resize() {
  if (!canvas.value || !context) return
  const ratio = Math.min(window.devicePixelRatio || 1, 2)
  width = canvas.value.clientWidth || 260
  height = canvas.value.clientHeight || 150
  canvas.value.width = Math.floor(width * ratio)
  canvas.value.height = Math.floor(height * ratio)
  context.setTransform(ratio, 0, 0, ratio, 0, 0)
}

function project(point: { x: number; y: number; z: number }, spin: number) {
  const x = point.x * Math.cos(spin) - point.z * Math.sin(spin)
  const z = point.x * Math.sin(spin) + point.z * Math.cos(spin)
  const depth = 1 / (3.5 - z)
  const sphereScale = Math.min(width * .48, height * .82)
  return { x: width * .5 + x * sphereScale * depth, y: height * .53 + point.y * sphereScale * depth, depth }
}

function syncPresentation() {
  const incoming = sourcePhase.value
  if (incoming === 'explode') {
    if (lastSourcePhase !== 'explode') {
      visualProgress = 0
    }
    phase.value = 'explode'
  } else if (incoming === 'fill') {
    if (lastSourcePhase === 'idle' || lastSourcePhase === 'done') {
      visualProgress = 0
    } else if (lastSourcePhase !== 'fill') {
      visualProgress = Math.min(visualProgress, progress.value)
    }
    phase.value = resolveOrbPresentationPhase('fill', visualProgress)
  } else if (incoming === 'done') {
    phase.value = resolveOrbPresentationPhase('done', visualProgress)
  } else {
    visualProgress = 1
    phase.value = 'idle'
  }
  lastSourcePhase = incoming
  displayProgress.value = visualProgress
}

function draw(time: number) {
  if (!context) return
  syncPresentation()
  const target = phase.value === 'explode' ? 0 : sourcePhase.value === 'done' || sourcePhase.value === 'idle' ? 1 : progress.value
  const speed = phase.value === 'explode' ? .07 : sourcePhase.value === 'done' ? .18 : .035
  const assemblyProgress = phase.value === 'explode'
    ? 0
    : resolveOrbAssemblyProgress(visualProgress)
  visualProgress += (target - visualProgress) * speed
  if (sourcePhase.value === 'done' && visualProgress > .985) phase.value = 'done'
  displayProgress.value = visualProgress

  context.clearRect(0, 0, width, height)
  const spin = time * .00025
  const front = -1 + visualProgress * 2
  const pulse = phase.value === 'explode' ? 1 + Math.sin(time * .006) * .04 : 1
  const sorted = particles.slice().sort((a, b) => a.sphere.z - b.sphere.z)
  for (const particle of sorted) {
    const isFilled = phase.value === 'idle' || phase.value === 'done' || (phase.value === 'fill' && particle.sphere.x <= front)
    const chargingBand = phase.value === 'fill' && Math.abs(particle.sphere.x - front) < .085
    const source = phase.value === 'explode'
      ? particle.loose
      : {
          x: particle.loose.x + (particle.sphere.x - particle.loose.x) * assemblyProgress,
          y: particle.loose.y + (particle.sphere.y - particle.loose.y) * assemblyProgress,
          z: particle.loose.z + (particle.sphere.z - particle.loose.z) * assemblyProgress,
        }
    const point = project({ x: source.x * pulse, y: source.y * pulse, z: source.z * pulse }, spin)
    const alpha = phase.value === 'explode' ? .72 : phase.value === 'done' ? .98 : isFilled ? .92 : .28
    const color = phase.value === 'done' ? '78, 156, 255' : phase.value === 'fill' && isFilled ? '72, 190, 255' : phase.value === 'fill' ? '92, 132, 142' : '74, 194, 139'
    const radius = particle.size * (1.2 + point.depth * 1.8) * (chargingBand ? 1.7 : 1)
    if (chargingBand) {
      context.save()
      context.shadowColor = 'rgba(102, 220, 255, .95)'
      context.shadowBlur = 14
    }
    context.beginPath()
    context.fillStyle = 'rgba(' + color + ', ' + alpha + ')'
    context.arc(point.x, point.y, radius, 0, TAU)
    context.fill()
    if (chargingBand) context.restore()
  }
  animation = requestAnimationFrame(draw)
}

onMounted(() => {
  if (!canvas.value) return
  context = canvas.value.getContext('2d')
  if (!context) return
  buildParticles()
  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(canvas.value)
  window.addEventListener('resize', resize)
  resize()
  animation = requestAnimationFrame(draw)
})

watch([sourcePhase, progress], () => syncPresentation())

onBeforeUnmount(() => {
  cancelAnimationFrame(animation)
  resizeObserver?.disconnect()
  window.removeEventListener('resize', resize)
  context = null
})
</script>

<template>
  <div class="orb-card" :class="'orb-phase-' + phase" :data-phase="phase" :data-progress="displayProgress.toFixed(2)">
    <div class="orb-copy"><span class="orb-eyebrow">LIVE AUDIO / {{ phase.toUpperCase() }}</span><strong>{{ phaseTitle }}</strong><small>{{ phaseDetail }}</small></div>
    <canvas ref="canvas" aria-label="合成实时充电粒子球" role="img"></canvas>
    <span class="orb-dot"></span>
  </div>
</template>
