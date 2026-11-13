/**
 * 参考音质量门禁的前端纯逻辑（展示层）。
 *
 * 页面外壳在 `components/ReferenceAudioReport.vue`：这里只放能被单元测试直接
 * 调用的部分（状态文案、指标格式化、结论汇总）。这样「不合格的项必须
 * 一条都不漏地显示出来」这类产品约束可以脱离 DOM 验证。
 *
 * 判据全部来自后端 `backend/services/reference_audio.py` 的确定性 DSP 测量；
 * 前端不做任何自己的判断，只负责如实呈现，避免两套阈值。
 *
 * 报告类型（ReferenceReport 及其家族）定义在 `lib/api/types`——它们是后端
 * 响应契约的一部分；这里反向导入并 re-export，保持既有 import 路径可用，
 * 同时消除 lib→composables 的反向依赖。
 */

import type { ReferenceCheck, ReferenceReport } from '../lib/api'

export type {
  ReferenceCheck,
  ReferenceCheckStatus,
  ReferenceInspectKind,
  ReferenceMetrics,
  ReferenceReport,
  ReferenceStatus,
} from '../lib/api'

export const REFERENCE_STATUS_LABELS: Record<string, string> = {
  ok: '可用',
  warn: '可用但有警告',
  fail: '未达标',
  unknown: '无法检查',
}

export const REFERENCE_PURPOSES: { value: string; label: string; hint: string }[] = [
  { value: 'synthesis', label: '合成参考音', hint: '一段几秒的干净人声，用于零样本配音。' },
  { value: 'timbre', label: '音色匹配池', hint: '成分钟的语音，用于把任意音频换成你的音色。' },
  { value: 'singing', label: '歌声参考音', hint: '音高范围更宽的录音，用于唱歌转换。' },
]

export function referenceStatusLabel(status: unknown): string {
  return REFERENCE_STATUS_LABELS[String(status ?? '')] || '未知'
}

/** 门禁只提示、不拦截：fail 也照常填入配置，用户始终可以直接试合成。 */
export function referenceQualityFailed(report: ReferenceReport | null | undefined): boolean {
  return report?.status === 'fail'
}

/** 报告里所有需要用户注意的项，不合格在前。 */
export function attentionChecks(report: ReferenceReport | null | undefined): ReferenceCheck[] {
  const checks = report?.checks || []
  const order: Record<string, number> = { fail: 0, warn: 1, ok: 2 }
  return checks
    .filter((check) => check.status !== 'ok')
    .slice()
    .sort((left, right) => (order[left.status] ?? 3) - (order[right.status] ?? 3))
}

export function passingChecks(report: ReferenceReport | null | undefined): ReferenceCheck[] {
  return (report?.checks || []).filter((check) => check.status === 'ok')
}

/** 数字缺失时给「—」而不是 0：0 是一个测量结果，缺失不是。 */
function isMissing(value: unknown): boolean {
  return value === null || value === undefined || value === '' || Number.isNaN(Number(value))
}

export function formatDb(value: unknown, digits = 1): string {
  if (isMissing(value)) return '—'
  const number = Number(value)
  return `${number.toFixed(digits)} dB`
}

export function formatDbfs(value: unknown, digits = 1): string {
  if (isMissing(value)) return '—'
  return `${Number(value).toFixed(digits)} dBFS`
}

export function formatPercent(value: unknown, digits = 2): string {
  if (isMissing(value)) return '—'
  return `${(Number(value) * 100).toFixed(digits)}%`
}

export function formatHertz(value: unknown): string {
  if (isMissing(value)) return '—'
  const number = Number(value)
  if (number >= 1000) return `${(number / 1000).toFixed(1)} kHz`
  return `${Math.round(number)} Hz`
}

export function formatSeconds(value: unknown, digits = 2): string {
  if (isMissing(value)) return '—'
  const number = Number(value)
  if (number < 60) return `${number.toFixed(digits)} 秒`
  const minutes = Math.floor(number / 60)
  return `${minutes} 分 ${(number - minutes * 60).toFixed(1)} 秒`
}

export type ReferenceMetricRow = { label: string; value: string; hint: string }

/**
 * 指标行。语料报告把聚合值放在 `aggregate` 里，文件报告放在 `metrics` 里，
 * 两者字段名相同，所以这里统一取一次，避免页面写两套模板。
 */
export function referenceMetricRows(report: ReferenceReport | null | undefined): ReferenceMetricRow[] {
  if (!report) return []
  const isCorpus = report.aggregate !== undefined
  const metrics = isCorpus ? report.aggregate || {} : report.metrics || {}
  const rows: ReferenceMetricRow[] = []

  rows.push({
    label: isCorpus ? '参考音总时长' : '时长',
    value: formatSeconds(isCorpus ? report.total_seconds : metrics.duration_seconds),
    hint: isCorpus ? `来自 ${report.files ?? 0} 个文件` : '决定能提供多少音色信息',
  })
  rows.push({
    label: '估算信噪比',
    value: isMissing(metrics.estimated_snr_db) ? '无法估计' : formatDb(metrics.estimated_snr_db),
    hint: '语音段电平与噪声底之差，是估计值而非认证值',
  })
  rows.push({
    label: '高频带宽',
    value: isMissing(metrics.high_band_db)
      ? '无法测量'
      : `4 kHz 以上低 ${Math.abs(Number(metrics.high_band_db)).toFixed(0)} dB`,
    hint: '窄带来源（电话录音等）摩擦音已丢失',
  })
  rows.push({
    label: '削波',
    value: metrics.clipping_run
      ? `${formatPercent(metrics.clipping_ratio, 3)}，最长连续 ${metrics.clipping_run} 个样本`
      : formatPercent(metrics.clipping_ratio, 3),
    hint: '连续贴顶才是硬削波，孤立贴顶属正常归一化',
  })
  rows.push({
    label: '峰值 / 有效值',
    value: `${formatDbfs(metrics.peak_dbfs)} / ${formatDbfs(metrics.rms_dbfs)}`,
    hint: '峰值过低会被噪声底掩盖',
  })
  rows.push({
    label: '静音占比',
    value: formatPercent(metrics.silence_ratio, 1),
    hint: '静音太多说明可用语音很少',
  })
  if (!isCorpus) {
    rows.push({
      label: '采样率 / 声道',
      value: `${formatHertz(metrics.sample_rate)} / ${metrics.channels ?? '—'} 声道`,
      hint: '44.1 kHz 的文件也可能没有高频内容，所以带宽单独测',
    })
  }
  if (metrics.f0) {
    rows.push({
      label: '音高覆盖',
      value: `${metrics.f0.f0_p05_hz.toFixed(0)}–${metrics.f0.f0_p95_hz.toFixed(0)} Hz`
        + `（${metrics.f0.f0_range_semitones.toFixed(1)} 半音）`,
      hint: `浊音占比 ${formatPercent(metrics.f0.voiced_ratio, 0)}`,
    })
  }
  return rows
}

/** 一句话结论，放在最显眼的位置。 */
export function referenceSummary(report: ReferenceReport | null | undefined): string {
  if (!report) return ''
  if (report.error) return `无法检查这段音频：${report.error}`
  if (report.status === 'fail') {
    const first = (report.blocking || [])[0]
    return first ? `这段音频不能直接用：${first}` : '这段音频不能直接用。'
  }
  if (report.status === 'warn') {
    const first = (report.warnings || [])[0]
    return first ? `可以使用，但建议先处理：${first}` : '可以使用，但有警告。'
  }
  return report.purpose === 'timbre'
    ? '这份参考音可以用于音色匹配池。'
    : '这段参考音可以用于合成。'
}
