import { describe, expect, it } from 'vitest'
import orbSource from './AudioOrb.vue?raw'

describe('AudioOrb V11 renderer contract', () => {
  it('accepts explicit per-output synthesis and playback state', () => {
    expect(orbSource).toContain('completedOutputIndexes')
    expect(orbSource).toContain('failedOutputIndexes')
    expect(orbSource).toContain('semanticToken')
    expect(orbSource).toContain('playback')
    expect(orbSource).toContain('colors')
  })

  it('assigns particles to interleaved output shares rather than a directional fill', () => {
    expect(orbSource).toContain('particleGroup(index, props.total)')
    expect(orbSource).toContain('settledAt')
    expect(orbSource).not.toContain('resolveOrbChargeProgress')
    expect(orbSource).not.toContain('resolveOrbFillFront')
    expect(orbSource).not.toContain('progress?: number')
  })

  it('keeps Canvas 2D rendering and color-specific token waves', () => {
    expect(orbSource).toContain("getContext('2d')")
    expect(orbSource).toContain('resolveOrbColors')
    expect(orbSource).toContain('playbackToken')
    expect(orbSource).toContain('shadowBlur')
    expect(orbSource).not.toContain('IcosahedronGeometry')
  })

  it('accepts empty transient token and playback state between interactions', () => {
    expect(orbSource).toContain('semanticToken?: OrbToken | null')
    expect(orbSource).toContain('playback?: OrbPlayback | null')
  })

  it('clears playback waves only when switching to another output', () => {
    expect(orbSource).toContain('activePlaybackOutput')
    expect(orbSource).toContain("waves[index].source === 'playback'")
  })
})
