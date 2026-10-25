import { describe, expect, it } from 'vitest'
import {
  REFERENCE_PURPOSES,
  attentionChecks,
  formatDb,
  formatDbfs,
  formatHertz,
  formatPercent,
  formatSeconds,
  referenceQualityFailed,
  passingChecks,
  referenceMetricRows,
  referenceStatusLabel,
  referenceSummary,
  type ReferenceReport,
} from './referenceAudio'

function report(overrides: Partial<ReferenceReport> = {}): ReferenceReport {
  return {
    purpose: 'synthesis',
    purpose_label: '合成参考音',
    status: 'ok',
    metrics: {
      duration_seconds: 4.2,
      sample_rate: 44100,
      channels: 1,
      clipping_ratio: 0,
      clipping_run: 0,
      silence_ratio: 0.12,
      estimated_snr_db: 28.9,
      high_band_db: -19.4,
      peak_dbfs: -3.1,
      rms_dbfs: -18.9,
    },
    checks: [],
    ...overrides,
  }
}

describe('reference audio verdicts', () => {
  it('only a fail blocks the user', () => {
    // A warning must never stop synthesis: a strict gate that blocks usable clips is
    // worse than no gate, because the user cannot override a disabled button.
    expect(referenceQualityFailed(report({ status: 'ok' }))).toBe(false)
    expect(referenceQualityFailed(report({ status: 'warn' }))).toBe(false)
    expect(referenceQualityFailed(report({ status: 'unknown' }))).toBe(false)
    expect(referenceQualityFailed(report({ status: 'fail' }))).toBe(true)
    expect(referenceQualityFailed(null)).toBe(false)
  })

  it('labels every status the backend can emit', () => {
    for (const status of ['ok', 'warn', 'fail', 'unknown']) {
      expect(referenceStatusLabel(status)).not.toBe('未知')
    }
    expect(referenceStatusLabel('something-new')).toBe('未知')
  })

  it('lists attention checks worst first and never hides a failure', () => {
    const checks = [
      { key: 'a', label: 'A', status: 'ok' as const, measured: '', threshold: '', message: '' },
      { key: 'b', label: 'B', status: 'warn' as const, measured: '', threshold: '', message: '' },
      { key: 'c', label: 'C', status: 'fail' as const, measured: '', threshold: '', message: '' },
      { key: 'd', label: 'D', status: 'warn' as const, measured: '', threshold: '', message: '' },
    ]
    const attention = attentionChecks(report({ checks }))
    expect(attention.map((check) => check.key)).toEqual(['c', 'b', 'd'])
    expect(passingChecks(report({ checks })).map((check) => check.key)).toEqual(['a'])
  })

  it('surfaces the first blocking reason in the summary', () => {
    const summary = referenceSummary(
      report({ status: 'fail', blocking: ['有 15 个连续样本停在满量程'] }),
    )
    expect(summary).toContain('15 个连续样本')
    expect(summary).toContain('不能直接用')
  })

  it('reports an analysis failure instead of claiming the clip is fine', () => {
    const summary = referenceSummary(report({ status: 'unknown', error: 'NoSuchFile' }))
    expect(summary).toContain('NoSuchFile')
    expect(summary).not.toContain('可以使用')
  })

  it('uses corpus wording for a corpus report', () => {
    const summary = referenceSummary(
      report({ purpose: 'timbre', status: 'ok', aggregate: { estimated_snr_db: 28.9 } }),
    )
    expect(summary).toContain('音色匹配池')
  })

  it('says why an empty report has nothing to show', () => {
    expect(referenceSummary(null)).toBe('')
  })
})

describe('reference audio formatting', () => {
  it('distinguishes a missing measurement from a zero measurement', () => {
    // 0 dB SNR and "could not be measured" are different facts; printing 0 for the
    // second would be a fabricated result.
    expect(formatDb(0)).toBe('0.0 dB')
    expect(formatDb(null)).toBe('—')
    expect(formatDb(undefined)).toBe('—')
    expect(formatPercent(0)).toBe('0.00%')
    expect(formatPercent(null)).toBe('—')
    expect(formatHertz(null)).toBe('—')
    expect(formatSeconds(null)).toBe('—')
  })

  it('formats the units the way a recording engineer reads them', () => {
    expect(formatDbfs(-3.14)).toBe('-3.1 dBFS')
    expect(formatHertz(44100)).toBe('44.1 kHz')
    expect(formatHertz(800)).toBe('800 Hz')
    expect(formatSeconds(4.25)).toBe('4.25 秒')
    expect(formatSeconds(236.3)).toBe('3 分 56.3 秒')
  })
})

describe('reference audio metric rows', () => {
  it('shows the file-level metrics for a single clip', () => {
    const rows = referenceMetricRows(report())
    const labels = rows.map((row) => row.label)
    expect(labels).toContain('时长')
    expect(labels).toContain('采样率 / 声道')
    expect(labels).not.toContain('参考音总时长')
  })

  it('shows the aggregate metrics for a corpus', () => {
    const rows = referenceMetricRows(
      report({
        purpose: 'timbre',
        total_seconds: 281.7,
        files: 64,
        aggregate: { estimated_snr_db: 28.9, high_band_db: -23.1, clipping_ratio: 0, silence_ratio: 0.1 },
      }),
    )
    const byLabel = Object.fromEntries(rows.map((row) => [row.label, row.value]))
    expect(byLabel['参考音总时长']).toBe('4 分 41.7 秒')
    expect(byLabel['估算信噪比']).toBe('28.9 dB')
    // A corpus has no single sample rate, so that row must not appear.
    expect(byLabel['采样率 / 声道']).toBeUndefined()
  })

  it('says a measurement is unavailable rather than showing a number', () => {
    const rows = referenceMetricRows(
      report({ metrics: { estimated_snr_db: null, high_band_db: null } }),
    )
    const byLabel = Object.fromEntries(rows.map((row) => [row.label, row.value]))
    expect(byLabel['估算信噪比']).toBe('无法估计')
    expect(byLabel['高频带宽']).toBe('无法测量')
  })

  it('mentions the clipping run when there is one', () => {
    const rows = referenceMetricRows(
      report({ metrics: { clipping_ratio: 0.0209, clipping_run: 15 } }),
    )
    const clipping = rows.find((row) => row.label === '削波')
    expect(clipping?.value).toContain('最长连续 15 个样本')
  })

  it('adds a pitch row only when the backend measured F0', () => {
    const withF0 = referenceMetricRows(
      report({
        metrics: {
          f0: {
            analyzed_seconds: 12,
            voiced_ratio: 0.62,
            f0_median_hz: 210,
            f0_p05_hz: 150,
            f0_p95_hz: 420,
            f0_range_semitones: 17.8,
          },
        },
      }),
    )
    expect(withF0.map((row) => row.label)).toContain('音高覆盖')
    expect(referenceMetricRows(report()).map((row) => row.label)).not.toContain('音高覆盖')
  })

  it('has nothing to show without a report', () => {
    expect(referenceMetricRows(null)).toEqual([])
  })
})

describe('reference audio purposes', () => {
  it('offers the three purposes the backend accepts, with matching values', () => {
    expect(REFERENCE_PURPOSES.map((option) => option.value)).toEqual([
      'synthesis',
      'timbre',
      'singing',
    ])
    for (const option of REFERENCE_PURPOSES) {
      expect(option.hint.length).toBeGreaterThan(0)
    }
  })
})
