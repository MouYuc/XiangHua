export type OrbColors = {
  synthesisToken: string
  settled: string
  playbackToken: string
}

export type OrbColorInput = Partial<OrbColors>

export type PlaybackPosition = {
  currentTime: number
  duration: number
  tokenCount: number
  frameRate?: number
  speed?: number
}

export const DEFAULT_ORB_COLORS: OrbColors = {
  synthesisToken: '#75BBFF',
  settled: '#69DDA4',
  playbackToken: '#F8FBFF',
}

function clamp(value: number, min = 0, max = 1) {
  return Math.min(max, Math.max(min, value))
}

function normalizeColor(value: unknown, fallback: string) {
  const candidate = String(value || '').toUpperCase()
  return /^#[0-9A-F]{6}$/.test(candidate) ? candidate : fallback
}

export function particleGroup(index: number, total: number) {
  return total > 0 ? Math.abs(Math.trunc(index)) % Math.trunc(total) : 0
}

export function resolvePlaybackToken(position: PlaybackPosition) {
  const tokenCount = Math.max(0, Math.trunc(Number(position.tokenCount) || 0))
  if (!tokenCount) return 0
  const currentTime = Math.max(0, Number(position.currentTime) || 0)
  const frameRate = Math.max(0, Number(position.frameRate) || 0)
  const speed = Math.max(0, Number(position.speed) || 1)
  const duration = Math.max(0, Number(position.duration) || 0)
  const fromRate = frameRate * speed * currentTime
  const fromDuration = duration > 0 ? currentTime / duration * tokenCount : 0
  return clamp(Math.floor(fromRate > 0 ? fromRate : fromDuration), 0, tokenCount - 1)
}

export function resolveOrbColors(input: OrbColorInput): OrbColors {
  return {
    synthesisToken: normalizeColor(input.synthesisToken, DEFAULT_ORB_COLORS.synthesisToken),
    settled: normalizeColor(input.settled, DEFAULT_ORB_COLORS.settled),
    playbackToken: normalizeColor(input.playbackToken, DEFAULT_ORB_COLORS.playbackToken),
  }
}

export function hexToRgb(value: string): [number, number, number] {
  const hex = normalizeColor(value, '#000000').slice(1)
  return [
    Number.parseInt(hex.slice(0, 2), 16),
    Number.parseInt(hex.slice(2, 4), 16),
    Number.parseInt(hex.slice(4, 6), 16),
  ]
}

export function mixColor(from: [number, number, number], to: [number, number, number], amount: number) {
  const t = clamp(amount)
  return [
    from[0] + (to[0] - from[0]) * t,
    from[1] + (to[1] - from[1]) * t,
    from[2] + (to[2] - from[2]) * t,
  ] as [number, number, number]
}
