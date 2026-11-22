<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useJobs, type JobRecord } from '@/composables/useJobs'
import { usePage } from '@/composables/usePage'
import { api } from '@/lib/api'

const { jobs, activeJobs, badge, open, loadFailed, setOpen, refresh, cancel } = useJobs()
const { setPage } = usePage()

// 分类：合成侧（工作台）与训练侧（声音工作坊）分开列。
type JobCategory = 'synthesis' | 'training'
const CATEGORY_LABEL: Record<JobCategory, string> = { synthesis: '合成', training: '训练' }
const CATEGORY_ORDER: JobCategory[] = ['synthesis', 'training']

function jobCategory(job: JobRecord): JobCategory {
  return job.kind === 'training' || job.kind === 'media_analysis' ? 'training' : 'synthesis'
}

/**
 * 就近为上：把 updated_at 解析成毫秒再比。
 * 后端各 kind 的时间格式不统一（空格分隔 vs ISO "T" 分隔），
 * 直接字符串比较会把不同类别的任务排错位。
 */
function jobTime(job: JobRecord): number {
  const raw = String(job.updated_at || job.started_at || '')
  if (!raw) return 0
  const parsed = Date.parse(raw) || Date.parse(raw.replace(' ', 'T'))
  return Number.isFinite(parsed) ? parsed : 0
}

function byNewestFirst(left: JobRecord, right: JobRecord): number {
  return jobTime(right) - jobTime(left)
}

const list = computed(() => [...activeJobs.value].sort(byNewestFirst))

// 分类切换：一个时刻只看一类任务记录（不分组堆叠）。
const catFilter = ref<JobCategory>('synthesis')

// 最近完成的任务：失败的任务必须可回查（原因/时间），不能一闪就没了
const finishedPool = computed(() =>
  jobs.value
    .filter((job) => job.status === 'done' || job.status === 'failed' || job.status === 'cancelled')
    .sort(byNewestFirst),
)

const visibleRunning = computed(() => list.value.filter((job) => jobCategory(job) === catFilter.value))
const visibleFinished = computed(() => finishedPool.value.filter((job) => jobCategory(job) === catFilter.value).slice(0, 8))
const rootEl = ref<HTMLElement | null>(null)

// GPU 队列状态：任务中心不只显示"谁在跑"，也显示"谁在排队等 GPU"。
type GpuState = { busy: boolean; holder: string; queue_length: number }
const gpu = ref<GpuState | null>(null)
const gpuTimer = ref<number | undefined>(undefined)

const kindTarget: Record<JobRecord['kind'], 'workbench' | 'workshop'> = {
  synthesis: 'workbench',
  voice_prepare: 'workbench',
  timbre: 'workbench',
  training: 'workshop',
  media_analysis: 'workshop',
}

function openTask(job: JobRecord) {
  setOpen(false)
  setPage(kindTarget[job.kind] ?? 'workbench')
}

/** GPU 依赖类任务：训练/媒体分析/合成/音色转换都要过租约。 */
function isGpuBound(job: JobRecord): boolean {
  return job.kind !== 'voice_prepare'
}

/** 该行是否在等 GPU：GPU 被别人占着，且本任务进度还是 0。 */
function isWaitingForGpu(job: JobRecord): boolean {
  if (!gpu.value?.busy || !isGpuBound(job)) return false
  return typeof job.progress === 'number' && job.progress <= 0
}

function gpuLine(): string {
  if (!gpu.value) return ''
  if (!gpu.value.busy) return 'GPU 空闲'
  const queue = gpu.value.queue_length ? ` · 排队 ${gpu.value.queue_length}` : ''
  return `GPU 占用中：${gpu.value.holder || '其他任务'}${queue}`
}

async function pollGpu() {
  try {
    const data = await api.get<{ gpu: GpuState }>('/api/overview')
    gpu.value = data.gpu
  } catch {
    /* 任务中心的附属信息，失败不打扰 */
  }
}

function statusTagClass(job: JobRecord) {
  return job.status === 'queued' ? 'mut' : ''
}

function statusText(job: JobRecord) {
  if (isWaitingForGpu(job)) return '排队 · 等 GPU'
  return job.status === 'queued' ? '排队中' : '进行中'
}

function progressText(job: JobRecord) {
  if (isWaitingForGpu(job)) return '等 GPU 空闲后自动开始'
  const stage = job.stage ? `${job.stage} · ` : ''
  const percent = typeof job.progress === 'number' && Number.isFinite(job.progress)
    ? ` ${Math.round(Math.min(1, Math.max(0, job.progress)) * 100)}%`
    : ''
  return `${stage}${job.status === 'queued' ? '排队中' : '进行中'}${percent}`
}

function onToggle() {
  setOpen(!open.value)
  if (open.value) void nextTick(placePop)
}

const popEl = ref<HTMLElement | null>(null)
const popStyle = ref<Record<string, string>>({})

/**
 * 弹层以按钮为锚水平居中；按钮太靠边时钳制在视口内。
 * 注意：弹层的包含块是 rootEl（position:relative），left/top 必须
 * 写成相对 rootEl 的偏移，不能按视口坐标算——否则宽窗口下会被推出屏幕。
 */
function placePop() {
  const root = rootEl.value
  if (!root || !open.value) return
  const anchor = root.getBoundingClientRect()
  const width = popEl.value?.offsetWidth || 352
  const margin = 8
  // 完美居中：相对 rootEl 的偏移 = rootEl 半宽 − 弹层半宽
  let left = anchor.width / 2 - width / 2
  // 视口钳制换算成相对偏移：目标视口 x = anchor.left + left ∈ [margin, innerWidth − width − margin]
  const minLeft = margin - anchor.left
  const maxLeft = window.innerWidth - width - margin - anchor.left
  left = Math.min(Math.max(left, minLeft), Math.max(minLeft, maxLeft))
  popStyle.value = {
    left: `${Math.round(left)}px`,
    top: `${Math.round(anchor.height + 8)}px`,
    transform: 'none',
  }
}

function onWindowResize() {
  if (open.value) placePop()
}

function onDocumentClick(event: MouseEvent) {
  if (!open.value) return
  const root = rootEl.value
  if (root && !root.contains(event.target as Node)) setOpen(false)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && open.value) setOpen(false)
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', onWindowResize)
  void pollGpu()
  gpuTimer.value = window.setInterval(() => void pollGpu(), 3000)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', onWindowResize)
  if (gpuTimer.value !== undefined) window.clearInterval(gpuTimer.value)
})
</script>

<template>
  <div ref="rootEl" style="position:relative">
    <button class="task-btn" type="button" aria-haspopup="true" :aria-expanded="open" @click="onToggle">
      <svg class="ic sm" viewBox="0 0 24 24"><path d="M22 12h-4l-3 8-4-16-3 8H2" /></svg>
      <span class="t">任务</span><span v-if="badge" class="b">{{ badge }}</span>
    </button>
    <div v-if="open" ref="popEl" class="task-pop" role="dialog" aria-label="任务中心" :style="popStyle">
      <div class="h">正在运行 · {{ visibleRunning.length }}</div>
      <div class="gpu-line mono">{{ gpuLine() }}</div>
      <div class="cat-row" role="tablist" aria-label="任务分类">
        <button
          v-for="cat in CATEGORY_ORDER"
          :key="cat"
          class="cat-btn"
          :class="{ on: catFilter === cat }"
          type="button"
          :aria-pressed="catFilter === cat"
          @click="catFilter = cat"
        >{{ CATEGORY_LABEL[cat] }}</button>
      </div>
      <template v-if="visibleRunning.length">
        <div v-for="job in visibleRunning" :key="job.job_id" class="task-row">
          <span class="task-t" role="button" tabindex="0" :title="`打开${job.kind === 'training' || job.kind === 'media_analysis' ? '声音工作坊' : '工作台'}`" @click="openTask(job)" @keydown.enter="openTask(job)">
            <span class="tt">
              <b class="mono">{{ job.title }}</b>
              <span class="tag" :class="statusTagClass(job)">{{ statusText(job) }}</span>
            </span>
            <span class="d mono">{{ job.error || progressText(job) }}</span>
            <span v-if="typeof job.progress === 'number' && job.status === 'running'" class="task-bar"><span :style="{ width: `${Math.round(Math.min(1, Math.max(0, job.progress)) * 100)}%` }" /></span>
          </span>
          <button v-if="job.cancelable && job.status !== 'done' && job.status !== 'failed'" class="task-x" type="button" @click.stop="cancel(job)">取消</button>
        </div>
      </template>
      <template v-else>
        <div class="task-empty">
          {{ loadFailed ? '暂时读不到任务状态。' : '当前没有进行中的任务。' }}
        </div>
      </template>
      <template v-if="visibleFinished.length">
        <div class="h" style="margin-top:8px">最近完成 · {{ visibleFinished.length }}</div>
        <div v-for="job in visibleFinished" :key="'f' + job.job_id" class="task-row">
          <span class="task-t" role="button" tabindex="0" :title="`打开${job.kind === 'training' || job.kind === 'media_analysis' ? '声音工作坊' : '工作台'}（${job.updated_at}）`" @click="openTask(job)" @keydown.enter="openTask(job)">
            <span class="tt">
              <b class="mono">{{ job.title }}</b>
              <span class="tag" :class="job.status === 'failed' ? 'err' : job.status === 'cancelled' ? 'mut' : 'ok'">{{ job.status === 'failed' ? '失败' : job.status === 'cancelled' ? '已取消' : '完成' }}</span>
            </span>
            <span class="d mono">{{ job.error || `${job.updated_at?.slice(11, 16) || ''}` }}</span>
          </span>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 任务行：mono 小字 + .tag 状态徽标 + 3px ink 细进度条（进度条本体在 base.css） */
.task-row {
  align-items: flex-start;
}
.task-row .tt {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.task-row .tt b {
  font-family: var(--mono);
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.task-row .tt .tag {
  flex: none;
}
.task-row .d {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text3);
}
/* 任务行可点击跳转：标题区 hover 提示可点 */
.task-t {
  cursor: pointer;
}
.task-t:hover b {
  text-decoration: underline;
  text-underline-offset: 2px;
}
.gpu-line {
  font-size: 10.5px;
  color: var(--text3);
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 6px;
}
/* 合成/训练 切换按钮：单列记录，点按切换显示哪一类 */
.cat-row {
  display: flex;
  gap: 4px;
  padding: 2px 9px 6px;
}
.cat-btn {
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: 0.08em;
  color: var(--text3);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--rs);
  padding: 3px 12px;
  cursor: pointer;
}
.cat-btn:hover {
  color: var(--text);
}
.cat-btn.on {
  color: var(--text);
  border-color: var(--text);
  background: var(--surface2);
}
</style>
