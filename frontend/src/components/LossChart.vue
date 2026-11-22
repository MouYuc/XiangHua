<script setup lang="ts">
/**
 * 训练 loss 曲线（纸面风）：横轴 global_step、纵轴 loss，
 * loss_g 实线 + loss_d 虚线，带网格、y 轴极值、s1→s2 分界标记与图例。
 * 数据来自 /api/training/metrics 的 metrics.jsonl 行。
 */
import { computed } from 'vue'

type MetricRow = {
  global_step?: number | null
  epoch?: number
  stage?: string
  loss_d?: number | null
  loss_g?: number | null
}

const props = defineProps<{ rows: MetricRow[] }>()

const W = 320
const H = 150
const M = { top: 14, right: 12, bottom: 22, left: 34 }

const series = computed(() =>
  props.rows
    .map((row, index) => ({
      index,
      step: Number(row.global_step ?? 0),
      epoch: Number(row.epoch ?? 0),
      stage: String(row.stage ?? ''),
      loss_d: Number(row.loss_d ?? 0),
      loss_g: Number(row.loss_g ?? 0),
    }))
    // 引擎每轮会先报一行无 loss 的占位，过滤掉；两线都为 0 的行也无法成图
    .filter((row) => row.loss_d > 0 || row.loss_g > 0),
)

const bounds = computed(() => {
  const rows = series.value
  if (!rows.length) return null
  const maxValue = Math.max(...rows.flatMap((row) => [row.loss_d, row.loss_g]), 0.01)
  const yMax = Math.ceil(maxValue * 10) / 10
  const stepMin = rows[0]!.step
  const stepMax = Math.max(rows[rows.length - 1]!.step, stepMin + 1)
  return { rows, yMax, stepMin, stepMax }
})

const plot = computed(() => {
  const b = bounds.value
  if (!b) return null
  const innerW = W - M.left - M.right
  const innerH = H - M.top - M.bottom
  const x = (step: number) => M.left + ((step - b.stepMin) / (b.stepMax - b.stepMin)) * innerW
  const y = (loss: number) => M.top + (1 - loss / b.yMax) * innerH
  const line = (key: 'loss_d' | 'loss_g') =>
    b.rows.map((row, index) => `${index ? 'L' : 'M'}${x(row.step).toFixed(2)} ${y(row[key]).toFixed(2)}`).join(' ')
  const gridRows = [0, 1, 2].map((i) => {
    const loss = (b.yMax / 2) * i
    return { y: y(loss), label: loss.toFixed(1) }
  })
  // s1 → s2 的切换点：第一条 stage 为 s2 的行
  const stageSplit = b.rows.findIndex((row) => row.stage === 's2')
  return {
    lineG: line('loss_g'),
    lineD: line('loss_d'),
    grid: gridRows,
    splitX: stageSplit > 0 ? x(b.rows[stageSplit]!.step) : null,
    firstEpoch: b.rows[0]!.epoch,
    lastEpoch: b.rows[b.rows.length - 1]!.epoch,
    lastStep: b.rows[b.rows.length - 1]!.step,
    lastG: b.rows[b.rows.length - 1]!.loss_g,
    lastD: b.rows[b.rows.length - 1]!.loss_d,
  }
})
</script>

<template>
  <div class="loss-chart">
    <div class="legend">
      <span><i class="line-demo solid" />loss_g 生成损失 <b>{{ plot ? plot.lastG.toFixed(3) : '—' }}</b></span>
      <span><i class="line-demo dash" />loss_d 判别损失 <b>{{ plot ? plot.lastD.toFixed(3) : '—' }}</b></span>
    </div>
    <svg v-if="plot" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" class="chart" role="img" aria-label="训练 loss 曲线">
      <g>
        <line v-for="(g, i) in plot.grid" :key="i" :x1="M.left" :x2="W - M.right" :y1="g.y" :y2="g.y" stroke="var(--border)" stroke-width="0.5" />
        <text v-for="(g, i) in plot.grid" :key="'t' + i" :x="M.left - 5" :y="g.y + 3" class="tick" text-anchor="end">{{ g.label }}</text>
      </g>
      <line v-if="plot.splitX" :x1="plot.splitX" :x2="plot.splitX" :y1="M.top - 4" :y2="H - M.bottom" stroke="var(--border2)" stroke-width="0.7" stroke-dasharray="3 3" />
      <text v-if="plot.splitX" :x="plot.splitX + 3" :y="M.top - 6" class="tick">s2</text>
      <path :d="plot.lineD" fill="none" stroke="var(--text3)" stroke-width="1" stroke-dasharray="4 3" />
      <path :d="plot.lineG" fill="none" stroke="var(--text)" stroke-width="1.4" />
      <text :x="M.left" :y="H - 6" class="tick">epoch {{ plot.firstEpoch }}</text>
      <text :x="W - M.right" :y="H - 6" class="tick" text-anchor="end">epoch {{ plot.lastEpoch }} · step {{ plot.lastStep }}</text>
    </svg>
  </div>
</template>

<style scoped>
.loss-chart{margin-top:10px}
.legend{display:flex;gap:14px;font-size:11px;color:var(--text2);margin-bottom:4px}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend b{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--text)}
.line-demo{width:14px;height:0;border-top:2px solid var(--text);display:inline-block}
.line-demo.dash{border-top:2px dashed var(--text3)}
.chart{width:100%;height:auto;display:block;background:var(--surface);border:1px solid var(--border);border-radius:var(--r)}
.tick{font-family:var(--mono);font-size:8px;fill:var(--text3)}
</style>
