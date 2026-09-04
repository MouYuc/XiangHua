import { describe, expect, it } from 'vitest'
// @ts-expect-error Vitest runs this test in Node, while the app typecheck does not bundle Node types.
import { readFileSync } from 'node:fs'
// @ts-expect-error Vitest runs this test in Node, while the app typecheck does not bundle Node types.
import { dirname, resolve } from 'node:path'
// @ts-expect-error Vitest runs this test in Node, while the app typecheck does not bundle Node types.
import { fileURLToPath } from 'node:url'
import appSource from './App.vue?raw'
import streamSource from './synthesisStream.ts?raw'
const apiSource = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), 'api.ts'), 'utf8')
const languageInputSource = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), 'components', 'LanguageMarkerInput.vue'), 'utf8')

const stylesSource = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), 'styles.css'), 'utf8')
const dockerSource = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'Dockerfile'), 'utf8')
const setupPowershellSource = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'setup.ps1'), 'utf8')
const setupShellSource = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'setup.sh'), 'utf8')

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

  it('exposes optional mixed EOS and token cap controls in sampling parameters', () => {
    expect(appSource).toContain('config.mixed_eos_floor')
    expect(appSource).toContain('config.mixed_token_cap')
    expect(appSource).toContain('混合 EOS 下限')
    expect(appSource).toContain('混合 Token 上限')
    expect(appSource).toContain('placeholder="自动"')
    expect(appSource).toContain('readOptionalNumber')
    expect(apiSource).toContain('mixed_eos_floor: number | null')
    expect(apiSource).toContain('mixed_token_cap: number | null')
  })

  it('keeps an expanded sampling panel scrollable when controls exceed its height', () => {
    expect(appSource).toContain('class="sampling-controls"')
    expect(stylesSource).toContain('.sampling-controls {')
    expect(stylesSource).toContain('overflow-y:auto;')
    expect(stylesSource).toContain('overscroll-behavior:contain;')
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
    expect(streamSource).toContain('/api/synthesis/events/')
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

  it('passes typed synthesis and playback state into the V11 audio orb', () => {
    expect(appSource).toContain(':total="synthesisJob?.total || 0"')
    expect(appSource).toContain(':completed-output-indexes="completedOrbOutputs"')
    expect(appSource).toContain(':semantic-token="orbSemanticToken"')
    expect(appSource).toContain(':playback="orbPlayback"')
    expect(appSource).toContain(':colors="orbColors"')
    expect(appSource).not.toContain(':progress="synthesisJob?.progress"')
  })

  it('reduces typed synthesis stream events separately from status snapshots', () => {
    expect(appSource).toContain('applySynthesisStreamEvent')
    expect(appSource).toContain("event.type === 'semantic_token'")
    expect(appSource).toContain("event.type === 'output_completed'")
    expect(appSource).toContain("event.type === 'snapshot'")
  })

  it('guards synthesis state against stale revisions and reconnects the stream', () => {
    expect(appSource).toContain("from './synthesisStream'")
    expect(appSource).toContain('shouldApplySynthesisRevision')
    expect(appSource).toContain('latestSynthesisRevision')
    expect(streamSource).toContain('value > latestRevision')
    expect(appSource).toContain('createSynthesisStream({')
  })

  it('uses individual audio playback time instead of a global playing boolean', () => {
    expect(appSource).toContain('@timeupdate="updateOrbPlayback(index, $event)"')
    expect(appSource).toContain('@play="startOrbPlayback(index, $event)"')
    expect(appSource).not.toContain('orbPlaying')
  })

  it('exposes V11 synthesis, settled, and playback color inputs', () => {
    expect(appSource).toContain('orb_synthesis_token_color')
    expect(appSource).toContain('orb_settled_color')
    expect(appSource).toContain('orb_playback_token_color')
    expect(appSource).toContain('class="orb-color-controls"')
  })

  it('loads and auto-persists V11 orb colors independently from synthesis', () => {
    expect(appSource).toContain("api.get<Config>('/api/config')")
    expect(appSource).toContain('await refreshConfig()')
    expect(appSource).toContain('function scheduleOrbColorPersistence')
    expect(appSource).toContain("api.put<Config>('/api/config', orbColorConfig())")
    expect(appSource).toContain('if (!orbColorHydrating) scheduleOrbColorPersistence()')
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
    const trainingPageIndex = appSource.indexOf('<template v-else-if="page === \'training\'">')
    expect(synthesisStart).toBeGreaterThan(-1)
    expect(modelIndex).toBeGreaterThan(synthesisStart)
    expect(appSource.lastIndexOf('</div>', trainingPageIndex)).toBeGreaterThan(modelIndex)
    expect(appSource).not.toContain('</section></section></div><section class="panel voice-panel">')
  })

  it('keeps batch synthesis as a file upload on the synthesis page', () => {
    expect(appSource).toContain('class="synthesis-upload"')
    expect(appSource).toContain('@change="loadSynthesisBatchFile"')
    expect(appSource).not.toContain('class="integrated-batch"')
    expect(appSource).not.toContain('class="synth-mode-switch"')
  })

  it('runs integrated batch synthesis through the background SSE job', () => {
    expect(appSource).toContain('async function startSynthesis(text: string)')
    expect(appSource).toContain("api.post<SynthesisSnapshot>('/api/synthesis', { ...config, text })")
    expect(appSource).toContain('async function synthesizeBatch() { await startSynthesis(synthesisText.value) }')
    expect(appSource).toContain('synthesisText.value = await file.text()')
    expect(appSource).toContain('!synthesisText.trim()')
    expect(appSource).toContain("async function synthesize() { await startSynthesis(synthesisText.value) }")
    expect(appSource).not.toContain('batchText')
    expect(appSource).not.toContain('/api/batch-synthesis')
    expect(appSource).not.toContain("page === 'batch'")
    expect(appSource).not.toContain('batchOutputDir')
    expect(appSource).not.toContain('batchZip')
    expect(appSource).not.toContain('batchResult')
    expect(appSource).not.toContain('loadBatchFile')
  })

  it('keeps pronunciation patches user-imported and injects orb callbacks in Docker', () => {
    expect(dockerSource).not.toContain('pinyin.patch')
    expect(dockerSource).not.toContain('COPY patches')
    expect(dockerSource).toContain('gpt_sovits_orb_patch.py')
    expect(setupPowershellSource).not.toContain('pinyin.patch')
    expect(setupShellSource).not.toContain('pinyin.patch')
    expect(appSource).toContain('/api/engine/pronunciation-patch')
    expect(appSource).toContain('importPronunciationPatch')
  })

  it('keeps the model and reference audio controls on the synthesis page', () => {
    expect(appSource).toContain('class="panel model-panel"')
    expect(appSource).toContain('GPT 模型')
    expect(appSource).toContain('SoVITS 模型')
    expect(appSource).toContain('参考音频')
    expect(appSource).toContain('参考文本')
    expect(appSource).not.toContain('class="panel voice-panel"')
  })

  it('replaces the text language selector with explicit language markers', () => {
    expect(appSource).toContain('text_lang')
    expect(appSource).toContain('placeholder="一行一句，# 注释；可用 <en>Hi</en> 标记语种"')
    expect(appSource).toContain('<LanguageMarkerInput')
    expect(appSource).not.toContain('v-model="config.text_lang"')
    expect(appSource).toContain("config.text_lang = 'zh'")
  })

  it('indexes supported explicit language markers while typing', () => {
    expect(languageInputSource).toContain('/<([A-Za-z]*)$/')
    expect(languageInputSource).toContain('suggestionsOpen')
    expect(languageInputSource).toContain('selectSuggestion')
    expect(languageInputSource).toContain('option.value.startsWith(prefix)')
    expect(languageInputSource).toContain('String(index + 1)')
    expect(languageInputSource).toContain('suppressNextKeyup')
    expect(languageInputSource).toContain('@keyup="handleKeyup"')
    expect(languageInputSource).toContain('isInsideCompleteLanguageTag')
    expect(languageInputSource).toContain('/^<\\/?(?:zh|yue|en|ja|ko)>$/i')
  })

  it('offers the prompt language control with reference audio upload', () => {
    expect(appSource).toContain('prompt_lang')
    expect(appSource).toContain('v-model="config.prompt_lang"')
    expect(appSource).toContain("'/api/reference-audio'")
    expect(appSource).toContain('uploadReferenceAudio')
  })

  it('keeps reference audio upload in the same row as the reference path', () => {
    const modelPanel = appSource.slice(
      appSource.indexOf('<section class="panel model-panel">'),
      appSource.indexOf('</section></div></template>', appSource.indexOf('<section class="panel model-panel">')),
    )
    expect(modelPanel).toContain('class="reference-audio-control"')
    expect(modelPanel).toContain('class="reference-audio-row"')
    expect(modelPanel).toContain('class="reference-audio-upload"')
    expect(modelPanel).not.toContain('{{ referenceAudioUploading ? \'参考音频上传中…\' : \'上传参考音频\' }}')
  })
})
