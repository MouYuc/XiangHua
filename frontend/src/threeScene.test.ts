import { describe, expect, it } from 'vitest'
import { orbitPositions, statusColor } from './threeScene'

describe('three interaction scene helpers', () => {
  it('maps training steps to evenly spaced orbit positions', () => {
    const positions = orbitPositions(4, 2)
    expect(positions).toHaveLength(4)
    expect(positions[0]).toEqual({ x: 0, y: 0, z: 2 })
    expect(Math.round(positions[2].z * 100) / 100).toBe(-2)
  })

  it('uses semantic colors for training states', () => {
    expect(statusColor('done')).toBe('#5eb77a')
    expect(statusColor('running')).toBe('#e9b95f')
    expect(statusColor('fail')).toBe('#db6a6a')
    expect(statusColor('pending')).toBe('#7d9184')
  })
})
