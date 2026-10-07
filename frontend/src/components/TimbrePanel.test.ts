import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import appSource from '../App.vue?raw'
import railSource from './IconRail.vue?raw'
import TimbrePanel from './TimbrePanel.vue'
import panelSource from './TimbrePanel.vue?raw'
import timbreSource from '../composables/timbre.ts?raw'
import {
  DEFAULT_TIMBRE_TOPK,
  TIMBRE_AUTO_RELEASE_NOTE,
  TIMBRE_MODEL_VRAM_GB,
  TIMBRE_PROFILE_ID,
  TIMBRE_TOPK_EXPLANATION,
  TIMBRE_TOPK_OPTIONS,
  asTimbreConvertResult,
  asTimbreProfileResult,
  formatTimbreBytes,
  formatTimbreClock,
  formatTimbreCount,
  formatTimbreGb,
  formatTimbreSeconds,
  isTimbreJobSettled,
  pollTimbreJob,
  timbreErrorMessage,
  timbreJobKindLabel,
  timbreJobPhaseLabel,
  timbreJobStatusLabel,
  timbreProgressPercent,
  timbreSourceOriginLabel,
  timbreVramFrom,
  timbreVramSummary,
  type TimbreJob,
} from '../composables/timbre'

function job(overrides: Partial<TimbreJob> = {}): TimbreJob {
  return {
    job_id: 'timbre_1',
    kind: 'convert',
    status: 'running',
    phase: 'prepare',
    message: '音色转换任务已启动。',
    progress: 0,
    error: '',
    ...overrides,
  }
}

describe('音色页接线（App.vue / IconRail.vue）', () => {
  it('新增第 5 个导航页 timbre 并保留原有 4 页', () => {
    expect(appSource).toContain("type Page = 'synthesis' | 'training' | 'history' | 'settings' | 'timbre'")
    expect(appSource).toContain("page === 'timbre'")
    expect(appSource).toContain('<TimbrePanel />')
    expect(appSource).toContain("import TimbrePanel from './components/TimbrePanel.vue'")
    expect(railSource).toContain("type RailPage = 'synthesis' | 'training' | 'history' | 'settings' | 'timbre'")
    expect(railSource).toContain("{ key: 'timbre', icon: MicVocal, label: '音色' }")
  })

  it('保留设置页作为最后的兜底分支，音色页在它之前', () => {
    const timbreBranch = appSource.indexOf(`page === 'timbre'`)
    const settingsFallback = appSource.indexOf('<template v-else>', appSource.indexOf("page === 'history'"))
    expect(timbreBranch).toBeGreaterThan(-1)
    expect(settingsFallback).toBeGreaterThan(timbreBranch)
  })
})

describe('TimbrePanel 契约', () => {
  it('对接全部 8 个 /api/timbre 端点', () => {
    for (const endpoint of [
      '/api/timbre/profile?profile_id=',
      '/api/timbre/profile/build',
      '/api/timbre/sources?limit=30',
      '/api/timbre/convert',
      '/api/timbre/convert-file',
      '/api/timbre/status/',
      '/api/timbre/jobs?limit=20',
      '/api/timbre/unload',
    ]) {
      expect(panelSource).toContain(endpoint)
    }
  })

  it('上传走 multipart，磁盘文件走 convert-file，两者都不缺字段', () => {
    const uploadBody = panelSource.slice(
      panelSource.indexOf("sourceMode.value === 'upload'"),
      panelSource.indexOf("const row = selectedSource.value"),
    )
    expect(uploadBody).toContain('new FormData()')
    expect(uploadBody).toContain("body.append('audio', file)")
    expect(uploadBody).toContain("body.append('topk', String(topk.value))")
    expect(uploadBody).toContain("body.append('profile_id', TIMBRE_PROFILE_ID)")
    expect(panelSource).toContain('source_path: row.path')
    expect(panelSource).toContain('download_id: row.download_id')
    expect(panelSource).toContain('topk: topk.value')
    expect(panelSource).toContain('device: deviceForRequest.value')
  })

  it('音色档案卡片展示参考文件、体积、缓存帧/秒、摘要与空闲显存', () => {
    expect(panelSource).toContain('/api/timbre/profile?profile_id=')
    expect(panelSource).toContain('参考文件数')
    expect(panelSource).toContain('参考音频体积')
    expect(panelSource).toContain('已缓存帧数')
    expect(panelSource).toContain('已缓存秒数')
    expect(panelSource).toContain('缓存摘要')
    expect(panelSource).toContain('cache_digest')
    expect(panelSource).toContain('空闲显存')
    expect(panelSource).toContain('建立音色匹配池')
    expect(panelSource).toContain('刷新音色匹配池')
  })

  it('源选择同时支持合成结果与上传，并标出来源和时间', () => {
    expect(panelSource).toContain('timbreSourceOriginLabel(row.origin)')
    expect(panelSource).toContain('row.created_at')
    expect(panelSource).toContain('formatTimbreBytes(row.bytes)')
    expect(panelSource).toContain('sourceMode')
    expect(panelSource).toContain('上传本地文件')
  })

  it('topk 只提供 1/2/4/8、默认 4，并解释 k 的取舍', () => {
    expect(TIMBRE_TOPK_OPTIONS).toEqual([1, 2, 4, 8])
    expect(DEFAULT_TIMBRE_TOPK).toBe(4)
    expect(panelSource).toContain('TIMBRE_TOPK_OPTIONS')
    expect(panelSource).toContain('TIMBRE_TOPK_EXPLANATION')
    expect(panelSource).toContain('默认 {{ DEFAULT_TIMBRE_TOPK }}')
    expect(timbreSource).toContain('k 越大音色越贴近目标')
    expect(timbreSource).toContain('唱歌的颤音与旋律')
  })

  it('结果区可以试听（audio controls）并给出下载链接与关键指标', () => {
    expect(panelSource).toContain('<audio controls :src="convertResult.url"')
    expect(panelSource).toContain(':href="convertResult.url" download')
    expect(panelSource).toContain('音频时长')
    expect(panelSource).toContain('转换耗时')
    expect(panelSource).toContain('匹配帧数')
    expect(panelSource).toContain('topk {{ convertResult.topk }}')
    expect(panelSource).toContain('formatTimbreSeconds(convertResult.duration)')
    expect(panelSource).toContain('formatTimbreClock(convertResult.seconds_taken)')
  })

  it('显存区显示 free/total、释放按钮，并说明模型占用与自动释放', () => {
    expect(panelSource).toContain('formatTimbreGb(vram.free_gb)')
    expect(panelSource).toContain('formatTimbreGb(vram.total_gb)')
    expect(panelSource).toContain('释放显存')
    expect(panelSource).toContain('TIMBRE_MODEL_VRAM_GB')
    expect(panelSource).toContain('TIMBRE_AUTO_RELEASE_NOTE')
    expect(timbreSource).toContain('长时间不用会自动释放')
    expect(timbreSource).toContain('会与合成页争抢 GPU')
  })

  it('任务失败时显示后端返回的中文 error', () => {
    expect(panelSource).toContain('timbreErrorMessage(convertJob)')
    expect(panelSource).toContain('timbreErrorMessage(profileJob)')
    expect(panelSource).toContain('timbreErrorMessage(job)')
    expect(panelSource).toContain('role="alert"')
    expect(panelSource).toContain('转换失败')
  })

  it('用轮询跟踪任务，并在卸载时停止', () => {
    expect(panelSource).toContain('pollTimbreJob(')
    expect(panelSource).toContain('`/api/timbre/status/${jobId}`')
    expect(panelSource).toContain('onBeforeUnmount(() => {')
    expect(panelSource).toContain('poller?.stop()')
    expect(panelSource).toContain('isTimbreJobSettled(job)')
  })

  it('回到页面时接着轮询仍在运行的任务', () => {
    const refreshBody = panelSource.slice(
      panelSource.indexOf('async function refreshJobs()'),
      panelSource.indexOf('async function onJobSettled('),
    )
    expect(refreshBody).toContain("jobs.value.find((job) => job.status === 'running')")
    expect(refreshBody).toContain('watchJob(resume)')
  })

  it('历史区展示最近音色任务', () => {
    expect(panelSource).toContain('最近音色任务')
    expect(panelSource).toContain('v-for="job in jobs"')
    expect(panelSource).toContain('timbreJobKindLabel(job.kind)')
    expect(panelSource).toContain('timbreJobStatusLabel(job.status)')
  })
})

describe('TimbrePanel 服务端渲染冒烟', () => {
  it('初始状态真的能渲染出五个区块，而不是只有源码里写着', async () => {
    // 没有 jsdom：用 SSR 渲染一次，能同时证明模板没有运行时错误、文案真的进了输出。
    const html = await renderToString(createSSRApp(TimbrePanel))
    for (const text of [
      '音色档案',
      '参考文件数',
      '参考音频体积',
      '已缓存帧数',
      '缓存摘要',
      '建立音色匹配池',
      '音色转换',
      '最近音频',
      '上传本地文件',
      '开始转换',
      'k 越大音色越贴近目标',
      '转换结果',
      '还没有转换结果',
      '显存管理',
      '释放显存',
      '长时间不用会自动释放',
      '最近音色任务',
      '还没有音色任务。',
    ]) {
      expect(html).toContain(text)
    }
    expect(html.match(/timbre-topk-btn/g)?.length).toBe(4)
  })
})

describe('音色页文案与格式化', () => {
  it('省略 device 时按 cuda 探测结果降级到 cpu', () => {
    expect(panelSource).toContain("profile.value.cuda === false ? 'cpu' : null")
  })

  it('格式化体积、时长、帧数与显存', () => {
    expect(formatTimbreBytes(18034176)).toBe('17.2 MB')
    expect(formatTimbreBytes(512)).toBe('1 KB')
    expect(formatTimbreBytes(0)).toBe('—')
    expect(formatTimbreBytes(undefined)).toBe('—')
    expect(formatTimbreSeconds(236.3)).toBe('3 分 56.3 秒')
    expect(formatTimbreSeconds(2.48)).toBe('2.48 秒')
    expect(formatTimbreSeconds(12.5)).toBe('12.5 秒')
    expect(formatTimbreClock(0.18)).toBe('0.18s')
    expect(formatTimbreCount(11817)).toBe('11,817')
    expect(formatTimbreGb(6.94)).toBe('6.94 GB')
    expect(formatTimbreGb(null)).toBe('—')
  })

  it('给出中文任务状态、阶段与来源标签', () => {
    expect(timbreJobStatusLabel('running')).toBe('进行中')
    expect(timbreJobStatusLabel('done')).toBe('已完成')
    expect(timbreJobStatusLabel('failed')).toBe('失败')
    expect(timbreJobPhaseLabel('prepare')).toBe('准备源音频')
    expect(timbreJobPhaseLabel('encode')).toBe('编码参考音')
    expect(timbreJobKindLabel('convert')).toBe('音色转换')
    expect(timbreJobKindLabel('profile')).toBe('匹配池')
    expect(timbreSourceOriginLabel('synthesis')).toBe('合成结果')
    expect(timbreSourceOriginLabel('upload')).toBe('上传')
  })

  it('把 0~1 的 progress 换算成百分比并夹紧', () => {
    expect(timbreProgressPercent(job({ progress: 0.42 }))).toBe(42)
    expect(timbreProgressPercent(job({ progress: 3 }))).toBe(100)
    expect(timbreProgressPercent(job({ progress: -1 }))).toBe(0)
    expect(timbreProgressPercent(null)).toBe(0)
    expect(isTimbreJobSettled(job({ status: 'running' }))).toBe(false)
    expect(isTimbreJobSettled(job({ status: 'done' }))).toBe(true)
    expect(isTimbreJobSettled(job({ status: 'failed' }))).toBe(true)
  })

  it('优先展示后端的 error，其次 message，最后给兜底文案', () => {
    expect(timbreErrorMessage(job({ error: 'ValueError: 没有可用参考音频', message: '失败' })))
      .toBe('ValueError: 没有可用参考音频')
    expect(timbreErrorMessage(job({ error: '', message: '显存不足' }))).toBe('显存不足')
    expect(timbreErrorMessage(job({ error: '', message: '' }))).toBe('音色任务失败，但没有返回原因。')
    expect(timbreErrorMessage(null)).toBe('音色任务失败，但没有返回原因。')
  })

  it('显存摘要区分已加载/未加载/无 CUDA', () => {
    expect(timbreVramSummary(timbreVramFrom({ cuda: true, loaded: true, device: 'cuda', free_gb: 3.99, total_gb: 8 })))
      .toBe('模型已加载 · 空闲 3.99 GB / 共 8.00 GB')
    expect(timbreVramSummary(timbreVramFrom({ cuda: true, loaded: false, free_gb: 6.94, total_gb: 8 })))
      .toBe('模型未加载 · 空闲 6.94 GB / 共 8.00 GB')
    expect(timbreVramSummary(timbreVramFrom({ cuda: false }))).toContain('CPU')
  })

  it('保住实测的显存数字：模型约 2.95 GB，自动释放说明存在', () => {
    expect(TIMBRE_MODEL_VRAM_GB).toBe(2.95)
    expect(TIMBRE_AUTO_RELEASE_NOTE).toContain('10 分钟')
    expect(TIMBRE_PROFILE_ID).toBe('current')
  })
})

describe('结果解析', () => {
  it('只把带 url 的转换结果当成可播放结果', () => {
    const result = asTimbreConvertResult({
      file_name: 'x.wav', url: '/api/files/abc', duration: 2.48, seconds_taken: 0.18,
      topk: 4, profile_id: 'current', matching_frames: 11817,
    })
    expect(result?.url).toBe('/api/files/abc')
    expect(result?.matching_frames).toBe(11817)
    expect(result?.topk).toBe(4)
    expect(asTimbreConvertResult({ file_name: 'x.wav' })).toBeNull()
    expect(asTimbreConvertResult(undefined)).toBeNull()
  })

  it('只有带 frames 的结果才当成匹配池结果', () => {
    const result = asTimbreProfileResult({
      profile_id: 'current', reference_files: 64, frames: 11817, seconds: 236.3, cache_path: 'x.pt',
    })
    expect(result?.frames).toBe(11817)
    expect(result?.reference_files).toBe(64)
    expect(asTimbreProfileResult({ url: '/api/files/abc' })).toBeNull()
    expect(asTimbreProfileResult(null)).toBeNull()
  })
})

describe('pollTimbreJob', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('持续上报进度，任务完成即停止轮询', async () => {
    vi.useFakeTimers()
    const snapshots: TimbreJob[] = [
      job({ status: 'running', progress: 0.5, message: '转换中' }),
      job({ status: 'running', progress: 0.9, message: '写文件中' }),
      job({ status: 'done', progress: 1, phase: 'done', message: '完成', result: { url: '/api/files/abc' } }),
    ]
    let cursor = 0
    const seen: string[] = []
    const fetcher = vi.fn(async (jobId: string) => {
      seen.push(jobId)
      const snapshot = snapshots[Math.min(cursor, snapshots.length - 1)]
      cursor += 1
      return snapshot
    })
    const updates: TimbreJob[] = []
    const settled: TimbreJob[] = []
    const poller = pollTimbreJob('timbre_1', fetcher, {
      onUpdate: (next) => updates.push(next),
      onSettled: (next) => settled.push(next),
    }, { intervalMs: 100 })

    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(300)
    poller.stop()
    await vi.advanceTimersByTimeAsync(1000)

    expect(seen).toEqual(['timbre_1', 'timbre_1', 'timbre_1'])
    expect(updates.map((item) => item.progress)).toEqual([0.5, 0.9, 1])
    expect(settled).toHaveLength(1)
    expect(settled[0].status).toBe('done')
    expect(settled[0].result?.url).toBe('/api/files/abc')
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('任务不存在时立刻停止并上报', async () => {
    vi.useFakeTimers()
    const onError = vi.fn()
    const onUpdate = vi.fn()
    const fetcher = vi.fn(async () => { throw new Error('音色任务不存在。') })
    pollTimbreJob('timbre_missing', fetcher, { onUpdate, onError }, { intervalMs: 100 })
    await vi.advanceTimersByTimeAsync(500)

    expect(onError).toHaveBeenCalledWith('音色任务不存在。')
    expect(onUpdate).not.toHaveBeenCalled()
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('连续失败到上限后停止，成功一次即清零计数', async () => {
    vi.useFakeTimers()
    let calls = 0
    const onError = vi.fn()
    const fetcher = vi.fn(async () => {
      calls += 1
      if (calls === 2) return job({ status: 'running', progress: 0.2 })
      throw new Error('网络错误')
    })
    pollTimbreJob('timbre_flaky', fetcher, { onError }, { intervalMs: 100, failureLimit: 3 })
    await vi.advanceTimersByTimeAsync(1000)

    expect(calls).toBe(5)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith('网络错误')
  })

  it('stop() 之后不再发起请求', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn(async () => job({ status: 'running', progress: 0.1 }))
    const poller = pollTimbreJob('timbre_1', fetcher, {}, { intervalMs: 100 })
    await vi.advanceTimersByTimeAsync(250)
    poller.stop()
    poller.stop()
    const callsAtStop = fetcher.mock.calls.length
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetcher.mock.calls.length).toBe(callsAtStop)
  })
})