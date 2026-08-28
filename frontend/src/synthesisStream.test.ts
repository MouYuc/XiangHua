import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createSynthesisStream, shouldApplySynthesisRevision } from './synthesisStream'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  close() { this.closed = true }
}

describe('synthesis stream lifecycle', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeEventSource.instances = []
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('reconnects with bounded exponential backoff after transport errors', () => {
    const retries: number[] = []
    const controller = createSynthesisStream({
      jobId: 'job',
      createEventSource: url => new FakeEventSource(url),
      onEvent: () => {},
      onRetry: (_attempt, delay) => retries.push(delay),
    })

    FakeEventSource.instances[0].onerror?.()
    vi.advanceTimersByTime(500)
    FakeEventSource.instances[1].onerror?.()
    vi.advanceTimersByTime(1000)
    FakeEventSource.instances[2].onerror?.()
    vi.advanceTimersByTime(2000)

    expect(retries).toEqual([500, 1000, 2000])
    expect(FakeEventSource.instances).toHaveLength(4)
    controller.close()
  })

  it('resets backoff after a successful open and resets on every message', () => {
    const controller = createSynthesisStream({
      jobId: 'job',
      createEventSource: url => new FakeEventSource(url),
      onEvent: () => {},
    })

    FakeEventSource.instances[0].onerror?.()
    vi.advanceTimersByTime(500)
    FakeEventSource.instances[1].onopen?.()
    FakeEventSource.instances[1].onmessage?.({ data: '{"type":"snapshot"}' })
    FakeEventSource.instances[1].onerror?.()

    vi.advanceTimersByTime(500)
    expect(FakeEventSource.instances).toHaveLength(3)
    controller.close()
  })

  it('does not reconnect after close', () => {
    const controller = createSynthesisStream({
      jobId: 'job',
      createEventSource: url => new FakeEventSource(url),
      onEvent: () => {},
    })

    controller.close()
    FakeEventSource.instances[0].onerror?.()
    vi.advanceTimersByTime(10000)

    expect(FakeEventSource.instances[0].closed).toBe(true)
    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('ignores duplicate and stale revisions but accepts newer state', () => {
    expect(shouldApplySynthesisRevision(undefined, 7)).toBe(true)
    expect(shouldApplySynthesisRevision(8, 7)).toBe(true)
    expect(shouldApplySynthesisRevision(8, 8)).toBe(false)
    expect(shouldApplySynthesisRevision(6, 8)).toBe(false)
  })
})
