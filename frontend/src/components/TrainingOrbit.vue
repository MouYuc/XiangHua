<script setup lang="ts">
import * as THREE from 'three'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { orbitPositions, statusColor } from '../threeScene'

const props = defineProps<{ steps: Record<string, { title?: string; status?: string }> }>()
const canvas = ref<HTMLCanvasElement | null>(null)
const stepKeys = ['slice', 'asr', 'correct', 'feat', 'train']
let renderer: THREE.WebGLRenderer | undefined
let animation = 0
let resizeObserver: ResizeObserver | undefined
let scene: THREE.Scene
let camera: THREE.PerspectiveCamera
let orbit: THREE.Group
let nodes: THREE.Mesh[] = []
const activeIndex = computed(() => Math.max(0, stepKeys.findIndex(key => props.steps?.[key]?.status === 'running')))

function resize() {
  if (!canvas.value || !renderer) return
  const width = canvas.value.clientWidth || 320
  const height = canvas.value.clientHeight || 220
  renderer.setSize(width, height, false)
  camera.aspect = width / height
  camera.updateProjectionMatrix()
}

function updateNodes() {
  nodes.forEach((node, index) => {
    const status = props.steps?.[stepKeys[index]]?.status || 'pending'
    const material = node.material as THREE.MeshStandardMaterial
    material.color.set(statusColor(status))
    material.emissive.set(statusColor(status))
    material.emissiveIntensity = status === 'running' ? .95 : status === 'done' ? .3 : .08
    node.scale.setScalar(status === 'running' ? 1.25 : 1)
  })
}

function frame(time: number) {
  const elapsed = time * .001
  orbit.rotation.y = elapsed * .12
  nodes.forEach((node, index) => {
    const status = props.steps?.[stepKeys[index]]?.status
    if (status === 'running') node.scale.setScalar(1.18 + Math.sin(elapsed * 5) * .12)
  })
  renderer?.render(scene, camera)
  animation = requestAnimationFrame(frame)
}

onMounted(() => {
  if (!canvas.value) return
  scene = new THREE.Scene()
  camera = new THREE.PerspectiveCamera(32, 1, .1, 100)
  camera.position.set(0, 2.8, 6.8)
  camera.lookAt(0, 0, 0)
  renderer = new THREE.WebGLRenderer({ canvas: canvas.value, alpha: true, antialias: true, powerPreference: 'high-performance' })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75))
  renderer.outputColorSpace = THREE.SRGBColorSpace
  scene.add(new THREE.AmbientLight('#d8efde', 1.8))
  const light = new THREE.PointLight('#75b987', 4, 10)
  light.position.set(2, 3, 3)
  scene.add(light)
  orbit = new THREE.Group()
  scene.add(orbit)
  const track = new THREE.Mesh(new THREE.TorusGeometry(2.05, .026, 8, 128), new THREE.MeshBasicMaterial({ color: '#8ebca0', transparent: true, opacity: .8 }))
  track.rotation.x = Math.PI / 2
  orbit.add(track)
  const points = orbitPositions(stepKeys.length, 2.05)
  nodes = points.map((point, index) => {
    const node = new THREE.Mesh(new THREE.SphereGeometry(.15, 20, 20), new THREE.MeshStandardMaterial({ color: statusColor('pending'), emissive: statusColor('pending'), emissiveIntensity: .08 }))
    node.position.set(point.x, point.y, point.z)
    orbit.add(node)
    return node
  })
  updateNodes()
  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(canvas.value)
  resize()
  animation = requestAnimationFrame(frame)
})

watch(() => props.steps, updateNodes, { deep: true })

onBeforeUnmount(() => {
  cancelAnimationFrame(animation)
  resizeObserver?.disconnect()
  renderer?.dispose()
  scene?.traverse(object => {
    if (object instanceof THREE.Mesh) { object.geometry.dispose(); (object.material as THREE.Material).dispose() }
  })
})
</script>

<template>
  <div class="training-orbit-card"><div class="orbit-copy"><span class="orb-eyebrow">TRAINING FLOW</span><strong>训练轨道</strong><small>{{ activeIndex >= 0 ? `步骤 ${activeIndex + 1} / ${stepKeys.length}` : '准备开始' }}</small></div><canvas ref="canvas" aria-label="训练步骤 3D 轨道" role="img"></canvas><div class="orbit-legend"><span v-for="(key, index) in stepKeys" :key="key" :class="props.steps?.[key]?.status || 'pending'"><i></i>{{ props.steps?.[key]?.title || key }}</span></div></div>
</template>
