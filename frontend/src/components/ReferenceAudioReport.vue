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
} from '@/composables/referenceAudio'

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
  <section v-if="busy" class="ref-check idle" aria-live="polite">
    <p class="ref-summary"><Gauge :size="14" aria-hidden="true" /> 正在检查参考音…</p>
  </section>

  <section v-else-if="report" class="ref-check" :class="status" aria-live="polite">
    <header class="ref-head">
      <component :is="glyph(status)" class="ref-glyph" :size="18" aria-hidden="true" />
      <div>
        <strong class="ref-title">{{ report.purpose_label || report.purpose }} · {{ referenceStatusLabel(status) }}</strong>
        <p class="ref-summary">{{ summary }}</p>
      </div>
    </header>

    <ul v-if="attention.length" class="ref-list">
      <li v-for="check in attention" :key="check.key" :class="check.status">
        <component :is="glyph(check.status)" class="ref-glyph" :size="14" aria-hidden="true" />
        <div>
          <strong>{{ check.label }}</strong>
          <span>{{ check.message }}</span>
          <small>实测 {{ check.measured }} · 判据 {{ check.threshold }}</small>
        </div>
      </li>
    </ul>

    <div class="ref-metrics">
      <div v-for="row in rows" :key="row.label" :title="row.hint">
        <span>{{ row.label }}</span>
        <strong>{{ row.value }}</strong>
      </div>
    </div>

    <div v-if="report.worst_file && isCorpus" class="ref-worst">
      最需要先修的一个：<code>{{ report.worst_file.path }}</code>
      （{{ referenceStatusLabel(report.worst_file.status) }}）
    </div>

    <details v-if="showPassing" class="ref-passed">
      <summary>{{ passing.length }} 项通过</summary>
      <ul>
        <li v-for="check in passing" :key="check.key">
          <CheckCircle2 :size="13" aria-hidden="true" />
          <span><strong>{{ check.label }}</strong>：{{ check.message }}</span>
        </li>
      </ul>
    </details>

    <details v-if="report.notes?.length || (isCorpus && report.per_file_counts)" class="ref-notes">
      <summary>判定说明</summary>
      <p v-if="isCorpus && report.per_file_counts">
        逐文件判定：{{ report.per_file_counts.ok || 0 }} 可用 /
        {{ report.per_file_counts.warn || 0 }} 有警告 /
        {{ report.per_file_counts.fail || 0 }} 不合格。门禁判的是语料整体（{{ formatSeconds(report.total_seconds) }}）。
      </p>
      <p v-for="note in report.notes || []" :key="note">
        <Info :size="12" aria-hidden="true" />{{ note }}
      </p>
    </details>
  </section>
</template>

<style scoped>
.ref-check {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--panel-2);
}

.ref-check.ok {
  border-color: var(--accent-line);
  background: var(--accent-soft);
}

.ref-check.warn {
  border-color: var(--warn-line);
  background: var(--warn-soft);
}

.ref-check.fail {
  border-color: var(--danger-line);
  background: var(--danger-soft);
}

.ref-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.ref-glyph {
  flex-shrink: 0;
  margin-top: 2px;
}

.ref-check.ok .ref-glyph {
  color: var(--accent);
}

.ref-check.warn .ref-glyph {
  color: var(--warn);
}

.ref-check.fail .ref-glyph {
  color: var(--danger);
}

.ref-check.idle .ref-glyph {
  color: var(--ink-3);
}

.ref-title {
  display: block;
  font-size: 13px;
  font-weight: 600;
}

.ref-summary {
  margin-top: 1px;
  color: var(--ink-2);
  font-size: var(--text-xs);
  line-height: 1.5;
  display: flex;
  align-items: center;
  gap: 5px;
}

.ref-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.ref-list li {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  padding: 7px 9px;
  border-radius: var(--radius);
  background: var(--panel-2);
  font-size: var(--text-xs);
}

.ref-list li.fail {
  color: var(--danger);
}

.ref-list li.warn {
  color: var(--warn);
}

.ref-list li div {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.ref-list strong {
  font-weight: 600;
}

.ref-list span {
  color: var(--ink-2);
  line-height: 1.5;
}

.ref-list small {
  color: var(--ink-3);
  font-size: 10px;
}

.ref-metrics {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}

.ref-metrics > div {
  display: flex;
  flex-direction: column;
  padding: 7px 9px;
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--panel-2);
}

.ref-metrics span {
  color: var(--ink-3);
  font-size: 10px;
}

.ref-metrics strong {
  font-size: var(--text-sm);
  font-weight: 550;
  font-variant-numeric: tabular-nums;
  word-break: break-word;
}

.ref-worst {
  color: var(--warn);
  font-size: var(--text-xs);
  line-height: 1.5;
  word-break: break-all;
}

.ref-worst code {
  font-family: var(--font-mono);
  font-size: 10.5px;
}

.ref-passed,
.ref-notes {
  border-top: 1px solid var(--rule);
  padding-top: 6px;
}

.ref-passed summary,
.ref-notes summary {
  color: var(--ink-3);
  font-size: var(--text-xs);
  cursor: pointer;
  user-select: none;
}

.ref-passed ul {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
}

.ref-passed li {
  display: flex;
  align-items: flex-start;
  gap: 5px;
  color: var(--ink-2);
  font-size: var(--text-xs);
}

.ref-passed svg {
  flex-shrink: 0;
  margin-top: 2px;
  color: var(--accent);
}

.ref-notes p {
  display: flex;
  gap: 5px;
  margin-top: 6px;
  color: var(--ink-3);
  font-size: 10.5px;
  line-height: 1.55;
}

.ref-notes svg {
  flex-shrink: 0;
  margin-top: 2px;
}
</style>
