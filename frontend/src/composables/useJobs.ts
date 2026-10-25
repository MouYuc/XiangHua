import { computed, onUnmounted, ref } from 'vue'
import { cancelJob, fetchJobs, type JobRecord } from '@/lib/api'
import { useToast } from './useToast'

export type { JobRecord }

/**
 * 全局任务中心：唯一轮询 GET /api/jobs 的地方。
 *
 * 所有页面共享同一份列表与徽标计数；只有打开下拉时才收窄轮询间隔。
 */

const POLL_OPEN_MS = 2000
const POLL_IDLE_MS = 5000
const POLL_FAILURE_LIMIT = 3

const jobs = ref<JobRecord[]>([])
const counts = ref<Record<string, number>>({})
const errors = ref<string[]>([])
const open = ref(false)
const loadFailed = ref(false)

let timer: number | undefined
let failures = 0
let initialized = false
let refCount = 0

const activeJobs = computed(() => jobs.value.filter((job) => job.status === 'running' || job.status === 'queued'))
const badge = computed(() => activeJobs.value.length)
const cancelableJobs = computed(() => jobs.value.filter((job) => job.cancelable && (job.status === 'running' || job.status === 'queued')))

async function refresh() {
  try {
    const data = await fetchJobs()
    failures = 0
    loadFailed.value = false
    jobs.value = Array.isArray(data.jobs) ? data.jobs : []
    counts.value = { ...(data.counts ?? {}) }
    errors.value = Array.isArray(data.errors) ? data.errors : []
  } catch {
    failures += 1
    if (failures >= POLL_FAILURE_LIMIT) loadFailed.value = true
  }
}

function schedule() {
  if (timer !== undefined) return
  timer = window.setInterval(() => {
    void refresh()
  }, open.value ? POLL_OPEN_MS : POLL_IDLE_MS)
}

function reschedule() {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
  schedule()
}

async function cancel(job: JobRecord) {
  const toast = useToast()
  try {
    await cancelJob(job.job_id)
    toast.info(`已请求取消：${job.title}`)
    await refresh()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : String(error))
  }
}

/** 首个挂载的消费者负责启动轮询；全部卸载后停表。 */
function startPolling() {
  refCount += 1
  if (!initialized) {
    initialized = true
    schedule()
    void refresh()
  }
}

function stopPolling() {
  refCount = Math.max(0, refCount - 1)
  if (refCount === 0 && timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
    initialized = false
  }
}

export function useJobs() {
  if (typeof window !== 'undefined') {
    startPolling()
    try {
      onUnmounted(stopPolling)
    } catch {
      /* 在非组件环境（单测）调用时没有生命周期，轮询由模块持有 */
    }
  }
  function setOpen(next: boolean) {
    open.value = next
    if (next) void refresh()
    reschedule()
  }
  return { jobs, activeJobs, cancelableJobs, counts, errors, badge, open, loadFailed, setOpen, refresh, cancel }
}
