import { computed, getCurrentInstance, onScopeDispose, ref } from 'vue'
import { createPolling } from './usePolling'
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

// One shared loop for the whole app (the task centre is the only consumer).
// `open` narrows the cadence, so the interval is a function rather than a value.
const controller = createPolling({
  interval: () => (open.value ? POLL_OPEN_MS : POLL_IDLE_MS),
  immediate: false,
  // Keeping the list fresh matters more than the traffic: a transient failure
  // is retried forever and only raises `loadFailed` for the badge.
  failLimit: 0,
  run: async () => {
    // A background tab does not need a 5 s poll; skip the round trip and pick up
    // again on the next tick once the tab is visible.
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return true
    await refresh()
    return true
  },
})

function schedule() {
  if (!controller.active()) controller.start()
}

function reschedule() {
  // The interval is re-read on the next schedule, so a toggle only needs the
  // loop running.
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
  if (refCount === 0) {
    controller.stop()
    initialized = false
  }
}

export function useJobs() {
  if (typeof window !== 'undefined') {
    startPolling()
    // `onScopeDispose` also fires for effect scopes and, unlike `onUnmounted`,
    // does not warn when there is no component instance — the previous code
    // threw-and-swallowed, which leaked the reference count so the timer never
    // stopped.
    if (getCurrentInstance()) onScopeDispose(stopPolling)
  }
  function setOpen(next: boolean) {
    open.value = next
    if (next) void refresh()
    reschedule()
  }
  return { jobs, activeJobs, cancelableJobs, counts, errors, badge, open, loadFailed, setOpen, refresh, cancel }
}
