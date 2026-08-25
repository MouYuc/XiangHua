export type OrbitPoint = { x: number; y: number; z: number }

export function orbitPositions(count: number, radius: number): OrbitPoint[] {
  if (count <= 0) return []
  return Array.from({ length: count }, (_, index) => {
    const angle = (index / count) * Math.PI * 2
    return { x: Math.sin(angle) * radius, y: 0, z: Math.cos(angle) * radius }
  })
}

export function statusColor(status: string): string {
  return ({ done: '#5eb77a', running: '#e9b95f', fail: '#db6a6a', pending: '#7d9184' } as Record<string, string>)[status] || '#7d9184'
}
