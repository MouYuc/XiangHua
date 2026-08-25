import { describe, expect, it } from 'vitest'
import orbSource from './AudioOrb.vue?raw'

describe('AudioOrb synthesis progress wiring', () => {
  it('accepts synthesis status and counters for phase-driven rendering', () => {
    expect(orbSource).toContain("status?: string")
    expect(orbSource).toContain("completed?: number")
    expect(orbSource).toContain("total?: number")
    expect(orbSource).toContain("progress?: number")
    expect(orbSource).toContain('resolveOrbPhase')
    expect(orbSource).toContain('resolveOrbChargeProgress')
  })

  it('exposes the active phase and progress to the visual surface', () => {
    expect(orbSource).toContain('orb-phase-')
    expect(orbSource).toContain('data-phase')
  })

  it('uses the same canvas particle charging renderer as the approved preview', () => {
    expect(orbSource).toContain("getContext('2d')")
    expect(orbSource).toContain('chargingBand')
    expect(orbSource).toContain('shadowBlur')
    expect(orbSource).toContain('resolveOrbAssemblyProgress')
    expect(orbSource).toContain('sphereScale')
    expect(orbSource).not.toContain('IcosahedronGeometry')
    expect(orbSource).not.toContain('orb-progress')
  })
})
