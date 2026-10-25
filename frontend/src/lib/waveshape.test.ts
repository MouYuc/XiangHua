import { describe, expect, it } from 'vitest'
import { buildWaveBars, formatClock, splitScriptLines } from './waveshape'

describe('buildWaveBars', () => {
  it('is deterministic for the same seed', () => {
    const first = buildWaveBars('晓华', 24, 40)
    const second = buildWaveBars('晓华', 24, 40)
    expect(first).toEqual(second)
  })

  it('varies shapes across seeds and keeps heights positive', () => {
    const a = buildWaveBars('a', 24)
    const b = buildWaveBars('b', 24)
    expect(a.map((bar) => bar.height)).not.toEqual(b.map((bar) => bar.height))
    for (const bar of a) {
      expect(bar.height).toBeGreaterThanOrEqual(2)
      expect(bar.height).toBeLessThanOrEqual(24)
    }
  })

  it('marks exactly the played prefix', () => {
    const bars = buildWaveBars('seed', 10, 50)
    expect(bars.filter((bar) => bar.on)).toHaveLength(5)
    expect(buildWaveBars('seed', 10, 0).every((bar) => !bar.on)).toBe(true)
    expect(buildWaveBars('seed', 10, 100).every((bar) => bar.on)).toBe(true)
  })

  it('clamps count into a sane range', () => {
    expect(buildWaveBars('seed', 0)).toHaveLength(1)
    expect(buildWaveBars('seed', 5000)).toHaveLength(96)
  })
})

describe('formatClock', () => {
  it('renders m:ss', () => {
    expect(formatClock(0)).toBe('0:00')
    expect(formatClock(4.2)).toBe('0:04')
    expect(formatClock(75)).toBe('1:15')
  })
})

describe('splitScriptLines', () => {
  it('drops blanks and comments while keeping original line numbers', () => {
    const lines = splitScriptLines('第一句\n\n# 注释\n  第二句  \n')
    expect(lines).toEqual([
      { index: 0, text: '第一句' },
      { index: 3, text: '第二句' },
    ])
  })
})
