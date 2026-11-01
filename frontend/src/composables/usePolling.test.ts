import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPolling } from './usePolling'

describe('createPolling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('runs immediately, keeps polling while the tick returns true', async () => {
    const run = vi.fn().mockResolvedValue(true)
    const controller = createPolling({ interval: 100, run })
    controller.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(run).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(100)
    expect(run).toHaveBeenCalledTimes(2)
    controller.stop()
  })

  it('stops when the tick returns false (job finished)', async () => {
    const run = vi.fn().mockResolvedValueOnce(true).mockResolvedValueOnce(false)
    const controller = createPolling({ interval: 100, run })
    controller.start()
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(100)
    await vi.advanceTimersByTimeAsync(500)

    expect(run).toHaveBeenCalledTimes(2)
    expect(controller.active()).toBe(false)
  })

  it('retries a transient failure instead of freezing the loop', async () => {
    const run = vi
      .fn()
      .mockRejectedValueOnce(new Error('blip'))
      .mockResolvedValue(true)
    const controller = createPolling({ interval: 100, run, failLimit: 3 })
    controller.start()
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(100)

    expect(run).toHaveBeenCalledTimes(2)
    expect(controller.active()).toBe(true)
    controller.stop()
  })

  it('gives up after the failure limit and reports it once', async () => {
    const run = vi.fn().mockRejectedValue(new Error('down'))
    const onGiveUp = vi.fn()
    const controller = createPolling({ interval: 100, run, failLimit: 2, onGiveUp })
    controller.start()
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(100)
    await vi.advanceTimersByTimeAsync(1000)

    expect(run).toHaveBeenCalledTimes(2)
    expect(controller.active()).toBe(false)
    expect(onGiveUp).toHaveBeenCalledTimes(1)
    expect(onGiveUp.mock.calls[0][1]).toBe(2)
  })

  it('never overlaps a slow tick with the next one', async () => {
    let inFlight = 0
    let maxInFlight = 0
    const run = vi.fn(async () => {
      inFlight += 1
      maxInFlight = Math.max(maxInFlight, inFlight)
      await new Promise((resolve) => window.setTimeout(resolve, 300))
      inFlight -= 1
      return true
    })
    const controller = createPolling({ interval: 50, run })
    controller.start()
    await vi.advanceTimersByTimeAsync(2000)
    controller.stop()

    expect(maxInFlight).toBe(1)
    expect(run.mock.calls.length).toBeGreaterThan(1)
  })

  it('re-evaluates a function interval each time it schedules', async () => {
    let delay = 100
    const run = vi.fn().mockResolvedValue(true)
    const controller = createPolling({ interval: () => delay, run })
    controller.start()
    await vi.advanceTimersByTimeAsync(0) // tick 1, schedules with 100ms
    expect(run).toHaveBeenCalledTimes(1)

    // Slow the loop down; the tick that is already scheduled still fires.
    delay = 1000
    await vi.advanceTimersByTimeAsync(100) // tick 2, now schedules with 1000ms
    expect(run).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(100) // too early for the 1000ms wait
    expect(run).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(900) // tick 3
    expect(run).toHaveBeenCalledTimes(3)
    controller.stop()
  })

  it('ignores a response that arrives after stop()/restart', async () => {
    const seen: string[] = []
    let release: (() => void) | undefined
    const run = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<boolean>((resolve) => {
            release = () => resolve(true)
          }),
      )
      .mockImplementation(() => {
        seen.push('second')
        return Promise.resolve(true)
      })

    const controller = createPolling({ interval: 100, run })
    controller.start()
    // Restart before the first tick settles: the stale tick must be dropped.
    controller.start()
    release?.()
    await vi.advanceTimersByTimeAsync(0)

    expect(seen).toEqual(['second'])
    controller.stop()
  })
})
