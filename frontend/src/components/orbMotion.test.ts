import { describe, expect, it } from 'vitest'
import { particleGroup, resolveOrbColors, resolvePlaybackToken } from './orbMotion'

describe('V11 orb motion helpers', () => {
  it('interleaves output ownership across the same sphere', () => {
    expect([0, 1, 2, 3, 4, 5].map((index) => particleGroup(index, 3))).toEqual([0, 1, 2, 0, 1, 2])
  })

  it('maps playback time through the semantic token rate before duration fallback', () => {
    expect(resolvePlaybackToken({ currentTime: 1, duration: 9, tokenCount: 50, frameRate: 25, speed: 1 })).toBe(25)
    expect(resolvePlaybackToken({ currentTime: 4.5, duration: 9, tokenCount: 50, frameRate: 0, speed: 1 })).toBe(25)
  })

  it('clamps playback tokens to the known semantic token range', () => {
    expect(resolvePlaybackToken({ currentTime: 30, duration: 9, tokenCount: 50, frameRate: 25, speed: 1 })).toBe(49)
    expect(resolvePlaybackToken({ currentTime: 0, duration: 9, tokenCount: 0, frameRate: 25, speed: 1 })).toBe(0)
  })

  it('uses V11 white as the default playback token color', () => {
    expect(resolveOrbColors({}).playbackToken).toBe('#F8FBFF')
  })

  it('normalizes accepted manual color values without trusting malformed input', () => {
    expect(resolveOrbColors({ playbackToken: '#ffffff', settled: 'green' })).toEqual({
      synthesisToken: '#75BBFF',
      settled: '#69DDA4',
      playbackToken: '#FFFFFF',
    })
  })
})
