import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type FetchHandler = (path: string) => unknown

function stubFetch(handler: FetchHandler) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    const body = handler(path)
    return { ok: true, status: 200, json: async () => body } as Response
  })
}

describe('useJobs', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('aggregates jobs, badge counts only active rows, and cancel routes to the API', async () => {
    const calls: string[] = []
    let canceled = false
    vi.stubGlobal('fetch', stubFetch((path) => {
      calls.push(path)
      if (path.includes('/cancel')) {
        canceled = true
        return { message: '已请求取消。' }
      }
      return {
        jobs: [
          { job_id: 'j1', kind: 'synthesis', title: '合成', status: 'running', progress: 0.5, stage: '', error: '', cancelable: false },
          { job_id: 'j2', kind: 'training', title: '训练', status: 'queued', progress: null, stage: '', error: '', cancelable: true },
          { job_id: 'j3', kind: 'timbre', title: '转换', status: 'done', progress: 1, stage: '', error: '', cancelable: false },
        ],
        counts: { running: 1, queued: 1, done: 1 },
        errors: [],
      }
    }))

    const { useJobs } = await import('./useJobs')
    const store = useJobs()
    await vi.advanceTimersByTimeAsync(0)
    expect(store.jobs.value).toHaveLength(3)
    expect(store.badge.value).toBe(2)
    expect(store.cancelableJobs.value.map((job) => job.job_id)).toEqual(['j2'])
    expect(calls.some((path) => path.includes('/api/jobs?limit='))).toBe(true)

    const toast = (await import('./useToast')).useToast()
    await store.cancel(store.jobs.value[1])
    expect(canceled).toBe(true)
    expect(toast.toasts.value.some((item) => item.message.includes('取消'))).toBe(true)
  })

  it('survives backend failures without throwing and flags loadFailed after repeated errors', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('boom') }))
    const { useJobs } = await import('./useJobs')
    const store = useJobs()
    await vi.advanceTimersByTimeAsync(0)
    expect(store.jobs.value).toEqual([])
    expect(store.loadFailed.value).toBe(false)
    await vi.advanceTimersByTimeAsync(5000 * 3)
    expect(store.loadFailed.value).toBe(true)
  })
})
