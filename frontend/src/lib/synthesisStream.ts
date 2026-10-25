export type EventSourceLike = {
  close: () => void
  onopen: ((event: any) => void) | null
  onmessage: ((event: any) => void) | null
  onerror: ((event: any) => void) | null
}

export type SynthesisStreamOptions = {
  jobId: string
  createEventSource: (url: string) => EventSourceLike
  onEvent: (data: string) => void
  onOpen?: () => void
  onError?: () => void
  onRetry?: (attempt: number, delay: number) => void
  retryBaseMs?: number
  retryMaxMs?: number
}

export type SynthesisStreamController = { close: () => void }

export function shouldApplySynthesisRevision(revision: unknown, latestRevision: number) {
  const value = Number(revision)
  return !Number.isFinite(value) || value > latestRevision
}

export function createSynthesisStream(options: SynthesisStreamOptions): SynthesisStreamController {
  const retryBaseMs = Math.max(100, options.retryBaseMs ?? 500)
  const retryMaxMs = Math.max(retryBaseMs, options.retryMaxMs ?? 5000)
  let source: EventSourceLike | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let active = true
  let attempts = 0

  function clearRetry() {
    if (retryTimer === null) return
    clearTimeout(retryTimer)
    retryTimer = null
  }

  function open() {
    if (!active) return
    source = options.createEventSource(`/api/synthesis/events/${options.jobId}`)
    source.onopen = () => {
      attempts = 0
      options.onOpen?.()
    }
    source.onmessage = event => {
      attempts = 0
      options.onEvent(event.data)
    }
    source.onerror = () => {
      source?.close()
      source = null
      if (!active) return
      options.onError?.()
      attempts += 1
      const delay = Math.min(retryMaxMs, retryBaseMs * 2 ** (attempts - 1))
      options.onRetry?.(attempts, delay)
      retryTimer = setTimeout(() => {
        retryTimer = null
        open()
      }, delay)
    }
  }

  open()

  return {
    close() {
      active = false
      clearRetry()
      source?.close()
      source = null
    },
  }
}
