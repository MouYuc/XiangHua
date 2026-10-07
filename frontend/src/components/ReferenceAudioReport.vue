<!-- frontend/src/components/ReferenceAudioReport.vue -->
<script setup lang="ts">
import { computed } from 'vue'
import { AlertTriangle, CheckCircle2, Gauge, Info, XCircle } from '@lucide/vue'
import {
  attentionChecks,
  formatSeconds,
  passingChecks,
  referenceMetricRows,
  referenceStatusLabel,
  referenceSummary,
  type ReferenceReport,
} from '../composables/referenceAudio'

/**
 * 参考音质量门禁报告。
 *
 * 只负责呈现：所有阈值和判定都来自后端，前端不复制一套，避免两边漂移。
 * 不合格项一定全部列出（后端已保证），页面不做折叠，免得用户漏看。
 */

const props = defineProps<{
  report: ReferenceReport | null
  busy?: boolean
}>()

const status = computed(() => props.report?.status ?? 'unknown')
const summary = computed(() => referenceSummary(props.report))
const attention = computed(() => attentionChecks(props.report))
const passing = computed(() => passingChecks(props.report))
const rows = computed(() => referenceMetricRows(props.report))
const isCorpus = computed(() => props.report?.aggregate !== undefined)
const showPassing = computed(() => passing.value.length > 0 && status.value !== 'fail')

function glyph(kind: string) {
  if (kind === 'fail') return XCircle
  if (kind === 'warn') return AlertTriangle
  return CheckCircle2
}
</script>

<template>
  <section v-if="busy" class="ref-check" aria-live="polite">
    <p class="ref-check-summary"><Gauge class="tool-glyph" aria-hidden="true" />正在检查参考音…</p>
  </section>

  <section v-else-if="report" class="ref-check" :class="status" aria-live="polite">
    <header class="ref-check-head">
      <component :is="glyph(status)" class="ref-check-glyph" aria-hidden="true" />
      <div>
        <strong class="ref-check-title">
          {{ report.purpose_label || report.purpose }} · {{ referenceStatusLabel(status) }}
        </strong>
        <p class="ref-check-summary">{{ summary }}</p>
      </div>
    </header>

    <ul v-if="attention.length" class="ref-check-list">
      <li v-for="check in attention" :key="check.key" :class="check.status">
        <component :is="glyph(check.status)" class="ref-check-glyph" aria-hidden="true" />
        <div>
          <strong>{{ check.label }}</strong>
          <span>{{ check.message }}</span>
          <small>实测 {{ check.measured }} · 判据 {{ check.threshold }}</small>
        </div>
      </li>
    </ul>

    <div class="ref-check-metrics">
      <div v-for="row in rows" :key="row.label" :title="row.hint">
        <span>{{ row.label }}</span>
        <strong>{{ row.value }}</strong>
      </div>
    </div>

    <div v-if="report.worst_file && isCorpus" class="ref-check-worst">
      最需要先修的一个：<code>{{ report.worst_file.path }}</code>
      （{{ referenceStatusLabel(report.worst_file.status) }}）
    </div>

    <details v-if="showPassing" class="ref-check-passed">
      <summary>{{ passing.length }} 项通过</summary>
      <ul>
        <li v-for="check in passing" :key="check.key">
          <CheckCircle2 class="ref-check-glyph" aria-hidden="true" />
          <span><strong>{{ check.label }}</strong>：{{ check.message }}</span>
        </li>
      </ul>
    </details>

    <p v-if="isCorpus && report.per_file_counts" class="ref-check-note">
      逐文件判定：{{ report.per_file_counts.ok || 0 }} 可用 /
      {{ report.per_file_counts.warn || 0 }} 有警告 /
      {{ report.per_file_counts.fail || 0 }} 不合格。
      门禁判的是语料整体（{{ formatSeconds(report.total_seconds) }}），
      单个短切片不合格不代表语料不合格。
    </p>

    <p v-for="note in report.notes || []" :key="note" class="ref-check-note">
      <Info class="tool-glyph" aria-hidden="true" />{{ note }}
    </p>
  </section>
</template>
