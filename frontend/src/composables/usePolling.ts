import { getCurrentInstance, onScopeDispose } from 'vue'

/**
 * One polling loop, used by every view that waits on a backend job.
 *
 * Seven hand-rolled loops had drifted apart: different intervals, three of them
 * stopped forever after a single transient error, none of them guarded against a
 * slow response overlapping the next tick, and several leaked their timer when
 * the view unmounted.
 *
 * Contract:
 * - `run` performs one tick. Return `false` to stop (job finished); return
 *   anything else to keep polling. Throwing counts as a failure.
 * - A tick never overlaps the previous one: the next timer is scheduled only
 *   after the current `run` settles, so a slow backend cannot pile up requests.
 * - `failLimit` consecutive failures stop the loop and call `onGiveUp`; a single
 *   blip is retried instead of freezing the UI forever.
 */
export type PollingOptions = {
  /**
   * Delay between the end of one tick and the start of the next, in ms.
   * A function is re-evaluated before every tick, for loops whose cadence
   * depends on UI state (the task centre polls faster while its panel is open).
   */
  interval: number | (() => number)
  run: () => Promise<boolean | void> | boolean | void
  /** Run one tick immediately on `start()` (default true). */
  immediate?: boolean
  /** Consecutive failures tolerated before giving up (default 3; 0 = never). */
  failLimit?: number
  /** Called once, when the loop gives up after `failLimit` failures. */
  onGiveUp?: (error: unknown, failures: number) => void
}

export type PollingController = {
  start: () => void
  stop: () => void
  readonly active: () => boolean
  readonly failures: () => number
}

export function createPolling(options: PollingOptions): PollingController {
  const { interval, run, immediate = true, failLimit = 3, onGiveUp } = options
  let timer: number | undefined
  let generation = 0
  let failureCount = 0
  let active = false

  function clearTimer() {
    if (timer !== undefined) {
      window.clearTimeout(timer)
      timer = undefined
    }
  }

  function schedule(myGeneration: number) {
    if (!active || myGeneration !== generation) return
    clearTimer()
    const delay = typeof interval === 'function' ? interval() : interval
    timer = window.setTimeout(() => void tick(myGeneration), Math.max(0, delay))
  }

  async function tick(myGeneration: number) {
    if (!active || myGeneration !== generation) return
    let keepGoing = true
    try {
      const outcome = await run()
      if (myGeneration !== generation) return
      failureCount = 0
      keepGoing = outcome !== false
    } catch (error) {
      if (myGeneration !== generation) return
      failureCount += 1
      if (failLimit > 0 && failureCount >= failLimit) {
        active = false
        clearTimer()
        onGiveUp?.(error, failureCount)
        return
      }
    }
    if (keepGoing) schedule(myGeneration)
    else stop()
  }

  function start() {
    // `generation` invalidates any tick that is still in flight from a previous
    // start(), which is what stopped a late response from an old job overwriting
    // the state of the current one.
    generation += 1
    active = true
    failureCount = 0
    clearTimer()
    if (immediate) void tick(generation)
    else schedule(generation)
  }

  function stop() {
    active = false
    generation += 1
    clearTimer()
  }

  return {
    start,
    stop,
    active: () => active,
    failures: () => failureCount,
  }
}

/**
 * Component-scoped variant: stops automatically when the owning scope is
 * disposed (which `onUnmounted` does not guarantee outside a component).
 */
export function usePolling(options: PollingOptions): PollingController {
  const controller = createPolling(options)
  if (getCurrentInstance()) onScopeDispose(controller.stop)
  return controller
}
