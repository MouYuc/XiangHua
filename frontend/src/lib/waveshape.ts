/**
 * 确定性波形条生成。
 *
 * 同一个 seed 永远得到同一组条高（设计稿同款算法），避免 "Math.random in
 * render" 造成的每次渲染抖动；played 部分染成前景色，其余留底色。
 */

export type WaveBar = { height: number; on: boolean }

export function buildWaveBars(seed: string, count: number, played = 0, height = 24): WaveBar[] {
  let state = 0
  for (const char of seed) state = (state * 31 + char.charCodeAt(0)) >>> 0
  const next = () => {
    state = (state * 1664525 + 1013904223) >>> 0
    return state / 4294967296
  }
  const total = Math.max(1, Math.min(96, Math.round(count)))
  const ratio = Math.max(0, Math.min(100, played))
  const bars: WaveBar[] = []
  for (let index = 0; index < total; index += 1) {
    const shape = 0.22 + 0.78 * Math.abs(Math.sin(index * 0.7 + (state % 9))) * (0.55 + 0.45 * next())
    const px = Math.max(2, Math.round(shape * height * 0.92))
    bars.push({ height: px, on: ratio > 0 && ((index / total) * 100) < ratio })
  }
  return bars
}

export function formatClock(seconds: number) {
  const total = Math.max(0, Number(seconds) || 0)
  const minutes = Math.floor(total / 60)
  const rest = Math.floor(total % 60)
  return `${minutes}:${String(rest).padStart(2, '0')}`
}

/** 拆台词：与后端 parse_lines 同规则（去空行、去 # 注释），并保留原行号。 */
export function splitScriptLines(text: string): { index: number; text: string }[] {
  return (text || '')
    .split('\n')
    .map((line, index) => ({ index, text: line.trim() }))
    .filter((line) => line.text !== '' && !line.text.startsWith('#'))
}
