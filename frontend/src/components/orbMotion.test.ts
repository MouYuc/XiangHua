import { describe, expect, it } from 'vitest'
import { resolveOrbAssemblyProgress, resolveOrbChargeProgress, resolveOrbPhase, resolveOrbFillFront, resolveOrbPresentationPhase } from './orbMotion'

describe('orb motion state machine', () => {
  it('starts in explode mode when synthesis begins', () => {
    expect(resolveOrbPhase({ active: true, status: 'running', completed: 0, total: 6 })).toBe('explode')
  })

  it('switches to fill mode after the first sentence completes', () => {
    expect(resolveOrbPhase({ active: true, status: 'running', completed: 2, total: 6 })).toBe('fill')
  })

  it('enters fill mode while the current sentence is generating', () => {
    expect(resolveOrbPhase({ active: true, status: 'running', phase: 'generating', completed: 0, total: 1 })).toBe('fill')
  })

  it('does not invent progress while the backend has not reported any', () => {
    expect(resolveOrbChargeProgress({ active: true, status: 'running', phase: 'generating', completed: 0, total: 1 })).toBe(0)
    expect(resolveOrbChargeProgress({ active: true, status: 'running', phase: 'generating', completed: 0, total: 1, progress: .43 })).toBe(.43)
  })

  it('uses backend progress when the synthesis stream provides it', () => {
    expect(resolveOrbChargeProgress({ active: true, status: 'running', phase: 'generating', completed: 0, total: 1, progress: .43 })).toBe(.43)
  })

  it('keeps the charging presentation visible until the final charge reaches the sphere', () => {
    expect(resolveOrbPresentationPhase('done', 0.72)).toBe('fill')
    expect(resolveOrbPresentationPhase('done', 1)).toBe('done')
  })

  it('lets the fill phase begin immediately when the stream advances', () => {
    expect(resolveOrbPresentationPhase('fill', 0)).toBe('fill')
  })

  it('morphs scattered particles according to live charge progress', () => {
    expect(resolveOrbAssemblyProgress(0)).toBe(0)
    expect(resolveOrbAssemblyProgress(.5)).toBe(.5)
    expect(resolveOrbAssemblyProgress(1)).toBe(1)
  })


  it('switches to a finished blue sphere when synthesis is done', () => {
    expect(resolveOrbPhase({ active: false, status: 'done', completed: 6, total: 6 })).toBe('done')
  })

  it('moves the fill front from left to right as progress increases', () => {
    expect(resolveOrbFillFront(0)).toBeLessThan(resolveOrbFillFront(0.5))
    expect(resolveOrbFillFront(0.5)).toBeLessThan(resolveOrbFillFront(1))
  })
})
