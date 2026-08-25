export type OrbPhase = 'idle' | 'explode' | 'fill' | 'done'

export type OrbSnapshot = {
  active?: boolean
  status?: string
  phase?: string
  completed?: number
  total?: number
  progress?: number
}

function clamp(value: number, min = 0, max = 1) {
  return Math.min(max, Math.max(min, value))
}

export function resolveOrbProgress(completed = 0, total = 0) {
  if (!total || total <= 0) return 0
  return clamp(completed / total)
}

export function resolveOrbPhase(snapshot: OrbSnapshot): OrbPhase {
  const active = Boolean(snapshot.active)
  const status = String(snapshot.status || '')
  const phase = String(snapshot.phase || '')
  const completed = Number(snapshot.completed || 0)
  const total = Number(snapshot.total || 0)

  if (status === 'done' || (!active && total > 0 && completed >= total)) return 'done'
  if (!active) return 'idle'
  if (phase === 'generating') return 'fill'
  if (completed <= 0 || total <= 0) return 'explode'
  return 'fill'
}

export function resolveOrbChargeProgress(snapshot: OrbSnapshot) {
  const completed = Number(snapshot.completed || 0)
  const total = Number(snapshot.total || 0)
  if (!total || total <= 0) return 0
  if (snapshot.status === 'done') return 1
  if (typeof snapshot.progress === 'number' && Number.isFinite(snapshot.progress)) return clamp(snapshot.progress)
  return resolveOrbProgress(completed, total)
}

export function resolveOrbPresentationPhase(sourcePhase: OrbPhase, visualProgress: number): OrbPhase {
  if (sourcePhase === 'done' && clamp(visualProgress) < 0.985) return 'fill'
  return sourcePhase
}

export function resolveOrbAssemblyProgress(chargeProgress: number) {
  return clamp(chargeProgress)
}

export function resolveOrbFillFront(progress: number) {
  return -1.35 + clamp(progress) * 2.7
}

export function mixColor(from: [number, number, number], to: [number, number, number], t: number) {
  const value = clamp(t)
  return [
    from[0] + (to[0] - from[0]) * value,
    from[1] + (to[1] - from[1]) * value,
    from[2] + (to[2] - from[2]) * value,
  ] as [number, number, number]
}
