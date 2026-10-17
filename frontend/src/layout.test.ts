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
const setupPowershellSource = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'setup.ps1'), 'utf8')
const setupShellSource = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'setup.sh'), 'utf8')

describe('full-bleed editor layout contracts', () => {
  it('keeps the top bar while page navigation moves to IconRail', () => {
    expect(appSource).not.toContain('class="sidebar"')
    expect(appSource).not.toContain('<aside class="sidebar"')
    expect(appSource).toContain('class="top-bar"')
    expect(appSource).toContain('class="app-body"')
    expect(appSource).toContain('<IconRail')
    expect(stylesSource).toContain('.top-bar {')
  })

  it('defaults to the synthesis page with no dashboard', () => {
    // 新增第 5 页「音色」后，Page 联合类型按同一顺序扩展；断言同步更新。
    expect(appSource).toContain("type Page = 'synthesis' | 'training' | 'history' | 'settings' | 'timbre'")
    expect(appSource).toContain("const page = ref<Page>('synthesis')")
    expect(appSource).not.toContain("'dashboard'")
  })

  it('uses an overlay, settings, and IconRail instead of the side panel', () => {
    expect(appSource).not.toContain('sidePanelOpen')
    expect(appSource).not.toContain('sidePanelTab')
    expect(appSource).not.toContain('<aside class="side-panel')
    expect(appSource).toContain('class="results-overlay"')
    expect(appSource).toContain('class="settings-group"')
    expect(appSource).toContain('<IconRail')
    expect(stylesSource).not.toContain('.side-panel')
    expect(stylesSource).not.toContain('.side-panel-tab')
    expect(stylesSource).toContain('.app-body {')
    expect(stylesSource).toContain('.icon-rail {')
    const railStart = stylesSource.indexOf('.icon-rail {')
    const railStyle = stylesSource.slice(railStart, stylesSource.indexOf('}', railStart))
    expect(railStyle).toContain('width: 44px;')
    const railButtonStart = stylesSource.indexOf('.rail-btn {')
    const railButtonStyle = stylesSource.slice(railButtonStart, stylesSource.indexOf('}', railButtonStart))
    expect(railButtonStyle).toContain('display: inline-flex;')
    expect(railButtonStyle).toContain('align-items: center;')
    expect(railButtonStyle).toContain('justify-content: center;')
    const expandedToggleStart = stylesSource.lastIndexOf('.icon-rail.expanded .rail-toggle {')
    const expandedToggleStyle = stylesSource.slice(expandedToggleStart, stylesSource.indexOf('}', expandedToggleStart))
    expect(expandedToggleStyle).toContain('justify-content: center;')
    expect(railStyle).toContain('position: sticky;')
    expect(railStyle).toContain('top: var(--top-bar-h);')
    expect(stylesSource).toContain('.icon-rail.expanded {')
    expect(stylesSource).toContain('.icon-rail.expanded .rail-label {')
    expect(stylesSource).toContain('.icon-rail-nav {')
    expect(stylesSource).not.toContain('.rail-flyout')
    expect(stylesSource).not.toContain('.icon-rail-context')
    expect(stylesSource).not.toContain('.icon-rail-sep')
    expect(stylesSource).toContain('.rail-resizer {')
    expect(stylesSource).toContain('.rail-resizer.dragging::after {')
    expect(stylesSource).toContain('@keyframes rail-item-in {')
    expect(stylesSource).toContain('@keyframes rail-label-in {')
    expect(stylesSource).toContain('cubic-bezier(.22, 1, .36, 1)')
    expect(stylesSource).toContain('.voice-stage-status {')
    expect(stylesSource).toContain('.orb-swallow {')
    expect(stylesSource).toContain('.results-overlay {')
  })

  it('falls back to the snapshot revision when stream events omit one', () => {
    expect(appSource).toContain('streamEvent.revision ??')
    expect(appSource).toContain('streamEvent.snapshot?.revision')
    expect(appSource).toContain('Number.isFinite(Number(revision))')
  })

  it('manages the results overlay as a focus-trapped dialog', () => {
    const resultsStart = appSource.indexOf('class="results-overlay"')
    const resultsEnd = appSource.indexOf('\n            </div>', resultsStart)
    const resultsHtml = appSource.slice(resultsStart, resultsEnd)
    expect(resultsHtml).toContain('ref="resultsOverlayRef"')
    expect(resultsHtml).toContain('ref="resultsCloseButtonRef"')
    expect(resultsHtml).toContain('@click.self="closeResults()"')
    expect(resultsHtml).toContain('@keydown="onResultsKeydown"')
    const orbStart = appSource.indexOf('ref="voiceStageOrbRef"')
    const orbTag = appSource.slice(appSource.lastIndexOf('<div', orbStart), appSource.indexOf('>', orbStart) + 1)
    expect(orbTag).toContain('role="button"')
    expect(orbTag).toContain('tabindex="0"')
    expect(orbTag).toContain('aria-label="设置粒子颜色"')
    expect(orbTag).toContain('@keydown.enter.prevent')
    expect(orbTag).toContain('@keydown.space.prevent')
    expect(orbTag).toContain('activateSynthesisStage')
    expect(appSource).toContain('function openResults(')
    expect(appSource).toContain('function closeResults()')
    expect(appSource).toContain('function onResultsKeydown(')
    expect(appSource).toContain('function pauseActiveResultsAudio(')
    expect(appSource).toContain('resultsCloseButtonRef.value?.focus()')
    expect(appSource).toContain('resultsTriggerElement')
  })

  it('pauses result audio before closing or resetting results', () => {
    const closeBody = appSource.slice(appSource.indexOf('function closeResults()'), appSource.indexOf('function onResultsKeydown('))
    expect(closeBody.indexOf('pauseActiveResultsAudio()')).toBeLessThan(closeBody.indexOf('orbPlayback.value = null'))
    expect(closeBody).toContain('resultsOpen.value = false')
    expect(appSource).toContain(".querySelectorAll('audio')")
    const resetCalls = (appSource.match(/pauseActiveResultsAudio\(\)/g) || []).length
    expect(resetCalls).toBeGreaterThanOrEqual(3)
  })

  it('stops status polling after repeated failures or missing jobs', () => {
    expect(appSource).toContain('SYNTHESIS_POLL_FAILURE_LIMIT = 5')
    expect(appSource).toContain('synthesisPollFailures = 0')
    expect(appSource).toContain('合成任务不存在')
  })

  it('offers a warm-up retry when synthesis readiness has failed', () => {
    expect(appSource).toContain("synthesisReadiness.status === 'failed'")
    expect(appSource).toContain('重新预热')
    expect(appSource).toContain('retrySynthesisWarmup')
  })

  it('bounds sampling inputs through readBoundedNumber', () => {
    expect(appSource).toContain('function readBoundedNumber(')
    for (const field of ['top_k', 'top_p', 'temperature', 'sample_steps', 'speed', 'pause_second']) {
      expect(appSource).toContain(`config.${field} = readBoundedNumber(`)
    }
  })

  it('retires dead panel state and keeps batch metadata fresh', () => {
    expect(appSource).not.toContain('samplingOpen')
    expect(appSource).not.toContain('pendingTrainingSteps')
    expect(appSource).not.toContain('environmentAlerts')
    expect(appSource).not.toContain('availableModelCount')
    expect(appSource).toContain('function applyDraftText(')
    expect(appSource).toContain(':disabled="busy || !synthesisReady || !synthesisText.trim()"')
  })

  it('uses a full-bleed text editor without card wrapping', () => {
    expect(appSource).toContain('class="editor-canvas"')
    expect(appSource).toContain('class="editor-main"')
    expect(appSource).not.toContain('class="panel lyric-panel"')
    expect(stylesSource).toContain('.editor-canvas {')
    expect(stylesSource).not.toContain('.editor-textarea')
    expect(stylesSource).toContain('.language-marker-field textarea {')
    const languageFieldStart = stylesSource.indexOf('\n.language-marker-field {')
    const editorStart = stylesSource.indexOf('.language-marker-field textarea {', languageFieldStart)
    const editorStyle = stylesSource.slice(editorStart, stylesSource.indexOf('}', editorStart))
    expect(editorStyle).toContain('font-size: 17px;')
    expect(editorStyle).toContain('border: none;')
    expect(editorStyle).toContain('background: transparent;')
  })

  it('keeps the focus editor as one continuous writing surface', () => {
    const editorStart = appSource.indexOf('class="editor-canvas"', appSource.indexOf('page === \'synthesis\''))
    const editorEnd = appSource.indexOf('class="action-bar"', editorStart)
    const editorHtml = appSource.slice(editorStart, editorEnd)
    expect(editorHtml).toContain('class="task-tools"')
    const taskToolsStart = editorHtml.indexOf('class="task-tools"')
    const taskToolsHtml = editorHtml.slice(taskToolsStart, editorHtml.indexOf('</div>', taskToolsStart))
    expect(taskToolsHtml).toContain('导入 TXT')
    expect(taskToolsHtml).toContain('<FileUp')
    expect(taskToolsHtml).not.toContain('icon-trigger')
    expect(editorHtml).toContain('loadSynthesisBatchFile')
    expect(editorHtml).toContain(`v-if="config.theme !== 'focus'" class="editor-meta"`)

    const focusScrollStart = stylesSource.indexOf('[data-theme="focus"] .editor-scroll {')
    const focusScrollStyle = stylesSource.slice(focusScrollStart, stylesSource.indexOf('}', focusScrollStart))
    expect(focusScrollStyle).toContain('display: flex;')
    const focusTextareaStart = stylesSource.indexOf('[data-theme="focus"] .language-marker-field textarea {')
    const focusTextareaStyle = stylesSource.slice(focusTextareaStart, stylesSource.indexOf('}', focusTextareaStart))
    expect(focusTextareaStyle).toContain('flex: 1;')
    expect(focusTextareaStyle).toContain('resize: none;')
    expect(stylesSource).toContain('.editor-scroll:focus-within .editor-empty-guide {')
    const guideFocusStart = stylesSource.indexOf('.editor-scroll:focus-within .editor-empty-guide {')
    const guideFocusStyle = stylesSource.slice(guideFocusStart, stylesSource.indexOf('}', guideFocusStart))
    expect(guideFocusStyle).toContain('visibility: hidden;')
    expect(guideFocusStyle).toContain('pointer-events: none;')
  })

  it('shows synthesis results in an overlay and appearance in settings', () => {
    const resultsStart = appSource.indexOf('class="results-overlay"')
    const resultsEnd = appSource.indexOf('\n            </div>', resultsStart)
    const resultsHtml = appSource.slice(resultsStart, resultsEnd)
    expect(resultsHtml).toContain('v-for="(item, index) in synthOutputs"')
    expect(resultsHtml).toContain('@click="retryOutput(index)"')
    expect(resultsHtml).toContain('download>下载</a>')
    expect(resultsHtml).toContain('调试日志')
    const settingsHtml = appSource.slice(appSource.indexOf('<template v-else>'), appSource.indexOf('<IconRail'))
    expect(settingsHtml).not.toContain('粒子颜色')
    expect(settingsHtml).not.toContain('class="orb-color-controls"')
  })

  it('puts particle color settings directly on the orb stage', () => {
    expect(appSource).toContain('const orbColorPanelOpen = ref(false)')
    expect(appSource).toContain('function toggleOrbColorPanel(')
    expect(appSource).toContain('aria-label="设置粒子颜色"')
    expect(appSource).toContain('@click="activateSynthesisStage"')
    expect(appSource).not.toContain('class="orb-color-trigger"')
    const orbStart = appSource.indexOf('ref="voiceStageOrbRef"')
    const stageEnd = appSource.indexOf('class="voice-stage-status"', orbStart)
    const stageHtml = appSource.slice(orbStart, stageEnd)
    expect(stageHtml).toContain('class="orb-color-panel"')
    expect(stageHtml).toContain('合成色')
    expect(stageHtml).toContain('聚合色')
    expect(stageHtml).toContain('播放色')
    const activation = appSource.slice(
      appSource.indexOf('function activateSynthesisStage('),
      appSource.indexOf('function draftPreview('),
    )
    expect(activation).toContain('toggleOrbColorPanel()')
    expect(stylesSource).toContain('.orb-color-panel {')
  })

  it('shows sampling params including mixed EOS/token cap in the panel', () => {
    expect(appSource).toContain('class="synthesis-params-drawer"')
    expect(appSource).toContain('const synthesisParamsOpen = ref(false)')
    expect(appSource).toContain('Top-K')
    expect(appSource).toContain('Mixed EOS 下限')
    expect(appSource).toContain('Mixed Token 上限')
    expect(appSource).toContain('自动 LightMid')
    const actionBarStart = appSource.indexOf('class="action-bar"')
    const actionBarEnd = appSource.indexOf('class="synthesis-alert"', actionBarStart)
    const actionBarHtml = appSource.slice(actionBarStart, actionBarEnd)
    expect(actionBarHtml).toContain('合成参数')
    expect(appSource).toContain('config.mixed_eos_floor')
    expect(appSource).toContain('config.mixed_token_cap')
    expect(appSource).toContain('readOptionalNumber')
    expect(apiSource).toContain('mixed_eos_floor: number | null')
    expect(apiSource).toContain('mixed_token_cap: number | null')
  })

  it('retires the legacy sampling panel state', () => {
    expect(appSource).not.toContain('samplingOpen')
    expect(stylesSource).toContain('.synthesis-params-drawer {')
    expect(stylesSource).not.toContain('.sampling-panel {')
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

  it('passes typed synthesis and playback state into the audio orb', () => {
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

  it('exposes synthesis, settled, and playback color inputs', () => {
    expect(appSource).toContain('orb_synthesis_token_color')
    expect(appSource).toContain('orb_settled_color')
    expect(appSource).toContain('orb_playback_token_color')
    expect(appSource).toContain('class="orb-color-controls"')
    expect(appSource).toContain('颜色修改后自动保存')
  })

  it('loads and auto-persists orb colors independently from synthesis', () => {
    expect(appSource).toContain("api.get<Config>('/api/config')")
    expect(appSource).toContain('await refreshConfig()')
    expect(appSource).toContain('function scheduleOrbColorPersistence')
    expect(appSource).toContain("api.put<Config>('/api/config', orbColorConfig())")
    expect(appSource).toContain('if (!orbColorHydrating) scheduleOrbColorPersistence()')
  })

  it('keeps batch synthesis as a file upload on the synthesis page', () => {
    expect(appSource).toContain('class="file-trigger"')
    expect(appSource).toContain('@change="loadSynthesisBatchFile"')
  })

  it('runs integrated batch synthesis through the background SSE job', () => {
    expect(appSource).toContain('async function startSynthesis(text: string)')
    expect(appSource).toContain("api.post<SynthesisSnapshot>('/api/synthesis', { ...config, text })")
    expect(appSource).toContain('synthesisText.value = await file.text()')
    expect(appSource).toContain('!text.trim()')
    expect(appSource).toContain('swallowPlay.value += 1')
    expect(appSource).toContain('function onSwallowDone()')
    expect(appSource).toContain('addDraft(swallowText.value)')
    expect(appSource).toContain('void startSynthesis(swallowText.value)')
    expect(appSource).not.toContain('batchText')
    expect(appSource).not.toContain('/api/batch-synthesis')
  })

  it('keeps pronunciation patches user-imported', () => {
    expect(setupPowershellSource).not.toContain('pinyin.patch')
    expect(setupShellSource).not.toContain('pinyin.patch')
    expect(appSource).toContain('/api/engine/pronunciation-patch')
    expect(appSource).toContain('importPronunciationPatch')
  })

  it('keeps model and reference audio controls on the synthesis page', () => {
    expect(appSource).toContain('GPT 模型')
    expect(appSource).toContain('SoVITS 模型')
    expect(appSource).toContain('音色样本')
    expect(appSource).toContain('这段音频说了什么')
    expect(appSource).toContain('音频语言')
  })

  it('replaces the text language selector with explicit language markers', () => {
    expect(appSource).toContain('text_lang')
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

  it('keeps reference audio upload near the reference path', () => {
    expect(appSource).toContain('class="reference-audio-row"')
    expect(appSource).toContain('class="reference-audio-upload"')
  })

  it('runs training as a persistent five-step workbench', () => {
    expect(appSource).toContain('const trainingWorkflow = computed(() => [')
    expect(appSource).toContain("key: 'slice'")
    expect(appSource).toContain("key: 'asr'")
    expect(appSource).toContain("key: 'correct'")
    expect(appSource).toContain("key: 'feat'")
    expect(appSource).toContain("key: 'train'")
    expect(appSource).toContain('class="training-rail"')
    expect(appSource).toContain('class="training-panel"')
    expect(appSource).toContain('function focusTrainingStep(')
    expect(appSource).not.toContain('toggleStep')
    expect(appSource).toContain('先完成第 1 步录音切片')
    expect(appSource).toContain('需要先应用广普发音补丁')
    expect(appSource).toContain("pending: '未开始'")
    expect(appSource).toContain("running: '进行中'")
    expect(appSource).toContain("done: '已完成'")
    expect(appSource).toContain("fail: '失败'")
    expect(appSource).toContain('训练数据')
    expect(appSource).toContain('训练环境')
    expect(appSource).toContain('实时日志')
    expect(stylesSource).toContain('.training-workbench {')
    expect(stylesSource).toContain('.training-rail {')
    expect(stylesSource).toContain('.training-panel {')
    expect(stylesSource).toContain('.training-context {')
  })

  it('opens the synthesis onboarding once with a persistent skip entry', () => {
    expect(appSource).toContain("const SYNTHESIS_ONBOARDING_STORAGE_KEY = 'xianghua.synthesis-onboarded'")
    expect(appSource).toContain('const synthesisOnboardingOpen = ref(false)')
    expect(appSource).toContain('const synthesisOnboardingStep = ref(1)')
    expect(appSource).toContain('function writeSynthesisOnboardingState()')
    expect(appSource).toContain('function openSynthesisOnboarding()')
    expect(appSource).toContain('function skipSynthesisOnboarding()')
    expect(appSource).toContain('if (!synthesisOnboarded.value && !voiceConfigured.value) openSynthesisOnboarding()')
    expect(appSource).toContain('title="新手引导"')
    expect(appSource).toContain('class="synthesis-voice-hint"')
    expect(appSource).toContain('role="dialog"')
    expect(appSource).toContain('aria-modal="true"')
    expect(appSource).toContain('跳过引导')
    expect(appSource).toContain("'选择样本', '填写原文', '生成试听'")
    expect(stylesSource).toContain('.synthesis-onboarding {')
    expect(stylesSource).toContain('.onboarding-card {')
  })

  it('preserves training API endpoints', () => {
    expect(appSource).toContain('/api/training/slice')
    expect(appSource).toContain('/api/training/asr')
    expect(appSource).toContain('/api/training/transcript')
    expect(appSource).toContain('/api/training/features')
    expect(appSource).toContain('/api/training/run')
    expect(appSource).toContain('/api/training/stop')
    expect(appSource).toContain('/api/training/patch')
    expect(appSource).toContain('/api/training/dependencies')
    expect(appSource).toContain('/api/training/models/select')
  })

  it('shows environment status in the top bar', () => {
    expect(appSource).toContain('class="top-bar-status"')
    expect(appSource).toContain('statusLabel')
    expect(appSource).toContain('statusClass')
  })

  it('uses a clean design system with tokens', () => {
    expect(stylesSource).toContain('--bg:')
    expect(stylesSource).toContain('--stage-bg:')
    expect(stylesSource).toContain('--stage-glow:')
    expect(stylesSource).toContain('--accent:')
    expect(stylesSource).toContain('--border:')
    expect(stylesSource).toContain('[data-mode="light"]')
    expect(stylesSource).toContain('[data-mode="dark"]')
    expect(stylesSource).toContain('[data-theme="focus"]')
    expect(stylesSource).toContain('[data-theme="workbench"]')
    expect(stylesSource).not.toContain('.sidebar {')
  })

  it('defaults both layout themes to one shared light color mode', () => {
    expect(appSource).toContain("type ColorMode = 'light' | 'dark'")
    expect(appSource).toContain("const colorMode = ref<ColorMode>('light')")
    expect(appSource).toContain(':data-mode="colorMode"')
    expect(appSource).toContain('切换布局 · 当前')

    expect(stylesSource).toContain('[data-mode="light"] {')
    expect(stylesSource).toContain('[data-mode="dark"] {')
    expect(stylesSource).not.toContain('[data-theme="focus"] {')
    expect(stylesSource).not.toContain('[data-theme="workbench"] {')
  })

  it('synchronizes voice stage and editor backgrounds through theme tokens', () => {
    const stageStart = stylesSource.indexOf('.voice-stage {')
    const stageStyle = stylesSource.slice(stageStart, stylesSource.indexOf('}', stageStart))
    expect(stageStyle).toContain('var(--stage-bg)')
    expect(stageStyle).toContain('var(--stage-glow)')

    const editorStart = stylesSource.indexOf('[data-theme="focus"] .editor-canvas {')
    const editorStyle = stylesSource.slice(editorStart, stylesSource.indexOf('}', editorStart))
    expect(editorStyle).toContain('var(--stage-glow)')
    expect(editorStyle).not.toContain('rgba(16,185,129')

    const focusStageStart = stylesSource.indexOf('[data-theme="focus"] .voice-stage {')
    const focusStageStyle = stylesSource.slice(focusStageStart, stylesSource.indexOf('}', focusStageStart))
    expect(focusStageStyle).not.toContain('linear-gradient(160deg')
    expect(focusStageStyle).not.toContain('rgba(16,185,129,.07)')
  })

  it('fuses the state-driven stage and workbench column into two themes', () => {
    expect(appSource).toContain("type Theme = 'workbench' | 'focus'")
    expect(appSource).toContain('THEME_ALIASES')
    expect(appSource).toContain('config.theme = normalizeTheme(config.theme)')
    expect(appSource).toContain("config.theme === 'workbench' ? 'focus' : 'workbench'")
    expect(appSource).toContain('class="workbench-aside"')
    expect(appSource).toContain(':data-synth-stage="synthStage"')
    expect(stylesSource).toContain('.workbench-aside {')
    expect(stylesSource).toContain('grid-template-areas:')
    expect(stylesSource).toContain('"orb status"')
    expect(stylesSource).toContain('"orb state"')
    expect(stylesSource).toContain('[data-theme="workbench"] .voice-stage-orb .orb-card canvas {')
    expect(stylesSource).toContain('[data-theme="focus"] .voice-stage {')
    expect(stylesSource).toContain('[data-theme="focus"] .editor-main {')
    expect(stylesSource).toContain('"stage task"')
    expect(stylesSource).toContain('"action action"')
    expect(stylesSource).not.toContain('[data-theme="focus"][data-synth-stage="running"] .editor-canvas')
    expect(stylesSource).not.toContain('[data-theme="focus"][data-synth-stage="done"] .editor-canvas')
  })

  it('switches the synthesis stage across idle, running, and done faces', () => {
    expect(appSource).toContain("type SynthStage = 'idle' | 'running' | 'done'")
    expect(appSource).toContain('const synthStage = computed<SynthStage>')
    expect(appSource).toContain('stage-quickstart')
    expect(appSource).toContain('stage-running')
    expect(appSource).toContain('stage-summary')
    expect(appSource).toContain('SAMPLE_LINES')
    expect(appSource).toContain('draftPreview')
    expect(appSource).toContain('failedOutputCount')
    expect(appSource).toContain('dismissSynthSummary')
    expect(appSource).toContain('继续编辑')
  })

  it('keeps the focus stage and task column separated', () => {
    expect(appSource).not.toContain('<template #drafts>')
    expect(appSource).not.toContain('<template #queue>')
    expect(appSource).not.toContain('<template #refaudio>')
    expect(appSource).toContain('class="focus-queue"')
    expect(appSource).toContain('class="result-tray"')
    expect(appSource).toContain('class="task-head-action"')
    expect(appSource).toContain('class="reference-drawer"')
    expect(appSource).toContain('class="editor-empty-guide"')
    expect(appSource).toContain('const showFocusEditorGuide = computed')
    expect(appSource).toContain('const actionReason = computed')
    expect(appSource).toContain('const referencePanelOpen = ref(false)')
    expect(appSource).toContain('const voiceSampleHelpOpen = ref(false)')
    expect(appSource).toContain('function activateSynthesisStage(')
    expect(appSource).toContain('function openReferencePanel(')
    expect(appSource).toContain('function closeReferencePanel()')
    expect(appSource).toContain('function toggleVoiceSampleHelp(')
    expect(appSource).toContain('function onReferenceDrawerKeydown(')
    expect(appSource).toContain('referenceTriggerElement?.focus()')

    const drawerStart = appSource.indexOf('class="reference-drawer"')
    const drawerEnd = appSource.indexOf('</section>', drawerStart)
    const drawerHtml = appSource.slice(drawerStart, drawerEnd)
    expect(drawerHtml).toContain('aria-label="音色样本设置"')
    expect(drawerHtml).toContain('aria-label="查看音色样本说明"')
    expect(drawerHtml).toContain(':aria-expanded="voiceSampleHelpOpen"')
    expect(drawerHtml).toContain('<HelpCircle')
    expect(drawerHtml).toContain('v-if="voiceSampleHelpOpen"')
    expect(drawerHtml).toContain('这段样本决定最终音色')
    expect(drawerHtml).toContain('尽量使用清晰人声')
    expect(drawerHtml).toContain('逐字填写音频中实际说出的内容')
    expect(drawerHtml).toContain('音频文件')
    expect(drawerHtml).toContain('这段音频说了什么')
    expect(drawerHtml).toContain('音频语言')
    expect(drawerHtml).toContain('v-model="config.ref_audio"')
    expect(drawerHtml).toContain('uploadReferenceAudio')
    expect(drawerHtml).toContain('v-model="config.prompt_lang"')
    expect(drawerHtml).toContain('v-model="config.ref_text"')
    expect(drawerHtml).toContain('importPronunciationPatch')

    const drawerKeydown = appSource.slice(
      appSource.indexOf('function onReferenceDrawerKeydown('),
      appSource.indexOf('function openSynthesisParamsPanel('),
    )
    expect(drawerKeydown.indexOf('voiceSampleHelpOpen.value = false'))
      .toBeLessThan(drawerKeydown.indexOf('closeReferencePanel()'))

    const startBody = appSource.slice(appSource.indexOf('async function startSynthesis('), appSource.indexOf('async function retryOutput('))
    expect(startBody).not.toContain('openResults(')

    expect(stylesSource).toContain('[data-theme="focus"] .editor-main {')
    expect(stylesSource).toContain('grid-area: stage;')
    expect(stylesSource).toContain('grid-area: task;')
    expect(stylesSource).toContain('@media (max-width: 960px)')
    expect(stylesSource).not.toContain('.parameter-row')
    expect(stylesSource).toContain('.reference-drawer {')
    expect(stylesSource).toContain('.help-trigger {')
    expect(stylesSource).toContain('.sample-explainer {')
    expect(stylesSource).toContain('.task-head-action {')

    const focusStart = stylesSource.indexOf('[data-theme="focus"] .editor-main {')
    const focusEnd = stylesSource.indexOf('@media (max-width: 960px)', focusStart)
    const focusHtml = stylesSource.slice(focusStart, focusEnd)
    expect(focusHtml).toContain('grid-template-areas:')
    expect(focusHtml).toContain('grid-template-columns: minmax(340px, 42%) minmax(0, 1fr);')
    expect(focusHtml).not.toContain('display: none;')
  })
})
