import { describe, expect, it } from 'vitest'
// @ts-expect-error Vitest runs this test in Node, while the app typecheck does not bundle Node types.
import { readFileSync } from 'node:fs'
// @ts-expect-error Vitest runs this test in Node, while the app typecheck does not bundle Node types.
import { dirname, resolve } from 'node:path'
// @ts-expect-error Vitest runs this test in Node, while the app typecheck does not bundle Node types.
import { fileURLToPath } from 'node:url'
import appSource from './App.vue?raw'

const stylesSource = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), 'styles.css'), 'utf8')

describe('workspace density layout contracts', () => {
  it('exposes the synthesis history drawer workflow', () => {
    expect(appSource).toContain('historyOpen')
    expect(appSource).toContain('synthesis-history-drawer')
    expect(appSource).toContain('history-count-badge')
  })

  it('defines dashboard metrics and workbench regions', () => {
    expect(appSource).toContain('dashboard-metrics')
    expect(appSource).toContain('dashboard-workbench')
    expect(appSource).toContain('pending-training')
    expect(appSource).toContain('dashboard-alerts')
  })

  it('supports pushed desktop drawers and compact mobile sheets', () => {
    expect(appSource).toContain('history-drawer-open')
    expect(appSource).toContain('synthesis-history-drawer')
    expect(appSource).toContain('historyTouchEnd')
  })

  it('locks the document scroll while the synthesis history drawer is open', () => {
    expect(appSource).toContain('watch(historyOpen')
    expect(appSource).toContain('document.body.style.overflow')
    expect(appSource).toContain('overscrollBehavior')
  })

  it('synchronizes the history drawer motion with the squeezed workspace', () => {
    expect(appSource).toContain('<Transition name="history-backdrop">')
    expect(appSource).toContain('<Transition name="history-drawer">')
    expect(appSource).toContain('@wheel.stop')
    expect(appSource).toContain('@touchmove.stop')
  })

  it('keeps sampling expansion inside a fixed synthesis lyric panel', () => {
    expect(appSource).toContain('class="panel lyric-panel"')
    expect(appSource).toContain('class="panel output-panel"')
    expect(appSource).toContain('samplingOpen')
    expect(appSource).toContain('sampling-panel')
    expect(appSource).toContain('sampling-toggle')
    expect(appSource).toContain(":class=\"{ 'is-open': samplingOpen }\"")
    expect(appSource).toContain(':inert="!samplingOpen"')
  })

  it('places the sampling toggle beside auto conversion in the lyric footer', () => {
    const toggleIndex = appSource.indexOf('class="sampling-toggle"')
    const autoConvertIndex = appSource.indexOf('自动转轻中广普')
    expect(toggleIndex).toBeGreaterThan(-1)
    expect(autoConvertIndex).toBeGreaterThan(-1)
    expect(toggleIndex).toBeLessThan(autoConvertIndex)
    expect(appSource).toContain('class="inline-controls"')
    const headStart = appSource.indexOf('class="lyric-head"')
    const headEnd = appSource.indexOf('class="lyric-editor-row"', headStart)
    expect(appSource.slice(headStart, headEnd)).not.toContain('sampling-toggle')
  })

  it('wires synthesis jobs to polling and SSE progress updates', () => {
    expect(appSource).toContain('/api/synthesis/status/')
    expect(appSource).toContain('/api/synthesis/events/')
    expect(appSource).toContain('EventSource')
    expect(appSource).toContain('synthesisJob')
    expect(appSource).toContain('synthesisProgress')
  })

  it('shows and polls synthesis model readiness before accepting work', () => {
    expect(appSource).toContain('/api/synthesis/readiness')
    expect(appSource).toContain('模型预热中')
    expect(appSource).toContain('retrySynthesisWarmup')
    expect(appSource).toContain('synthesisReady')
  })

  it('passes synthesis progress into the audio orb display', () => {
    expect(appSource).toContain(':status="synthesisJob?.status"')
    expect(appSource).toContain(':completed="synthesisJob?.completed || 0"')
    expect(appSource).toContain(':total="synthesisJob?.total || 0"')
    expect(appSource).toContain(':progress="synthesisJob?.progress"')
  })

  it('keeps the output column stable and contains debug logs inside output', () => {
    expect(stylesSource).toContain('.synthesis-grid { grid-template-rows:auto auto;')
    expect(stylesSource).toContain('.synthesis-grid .lyric-panel { grid-column:1; grid-row:1;')
    expect(stylesSource).toContain('.synthesis-grid .output-panel { grid-column:2; grid-row:1 / span 2;')
    expect(stylesSource).toContain('.synthesis-grid .debug-panel { height:198px; min-height:0; overflow:hidden;')
    expect(stylesSource).toContain('.synthesis-grid .debug-log-list { height:154px; max-height:154px;')
    expect(stylesSource).toContain('.synthesis-grid .output-panel { height:663px;')
    expect(stylesSource).toContain('overflow-y:auto;')
  })

  it('removes the manual resize handle from the synthesis lyric input', () => {
    expect(stylesSource).toContain('.synthesis-grid .lyric-panel textarea { resize:none; }')
  })

  it('keeps model settings in the left column below lyrics', () => {
    const synthesisStart = appSource.indexOf('class="synthesis-grid"')
    const modelIndex = appSource.indexOf('<section class="panel model-panel">')
    const batchPageIndex = appSource.indexOf('<template v-else-if="page === \'batch\'">')
    expect(synthesisStart).toBeGreaterThan(-1)
    expect(modelIndex).toBeGreaterThan(synthesisStart)
    expect(appSource.lastIndexOf('</div>', batchPageIndex)).toBeGreaterThan(modelIndex)
    expect(appSource).not.toContain('</section></section></div><section class="panel voice-panel">')
  })

  it('keeps batch synthesis as a file upload on the synthesis page', () => {
    expect(appSource).toContain('class="synthesis-upload"')
    expect(appSource).toContain('@change="loadSynthesisBatchFile"')
    expect(appSource).not.toContain('class="integrated-batch"')
    expect(appSource).not.toContain('class="synth-mode-switch"')
  })

  it('keeps the model and reference audio controls on the synthesis page', () => {
    expect(appSource).toContain('class="panel model-panel"')
    expect(appSource).toContain('GPT 模型')
    expect(appSource).toContain('SoVITS 模型')
    expect(appSource).toContain('参考音频')
    expect(appSource).toContain('参考文本')
    expect(appSource).not.toContain('class="panel voice-panel"')
  })
})
