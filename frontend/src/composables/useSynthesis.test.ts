import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'

const toastSuccess = vi.fn()
const toastError = vi.fn()
const streamClose = vi.fn()

vi.mock('@/lib/api', () => ({
  // The composable branches on `error instanceof ApiError`, so the mock has to
  // expose the real shape rather than a bare object.
  ApiError: class ApiError extends Error {
    status: number
    code: string
    constructor(message: string, status = 0, code = '') {
      super(message)
      this.status = status
      this.code = code
    }
  },
  api: { get: vi.fn(), post: vi.fn() },
}))

vi.mock('@/lib/synthesisStream', () => ({
  shouldApplySynthesisRevision: (revision: unknown, latest: number) => !Number.isFinite(Number(revision)) || Number(revision) > latest,
  createSynthesisStream: vi.fn(() => ({ close: streamClose })),
}))

vi.mock('./useConfig', () => ({
  useConfig: () => ({
    config: {
      ref_audio: 'ref.wav',
      speed: 1,
      // 后端激活声线卡后会把这些字段写进保存配置；合成请求不得原样回传。
      voice_card_id: 'vc_deleted_card',
      voice_card_version: '2026-10-25.1',
      style_id: 'natural',
    },
    loaded: { value: true },
  }),
}))

vi.mock('./useToast', () => ({
  useToast: () => ({ success: toastSuccess, error: toastError, info: vi.fn() }),
}))

async function setup() {
  vi.resetModules()
  const { api } = await import('@/lib/api')
  const stream = await import('@/lib/synthesisStream')
  const { useSynthesis } = await import('./useSynthesis')
  return { api, stream, synthesis: useSynthesis() }
}

describe('useSynthesis', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    toastSuccess.mockClear()
    toastError.mockClear()
    streamClose.mockClear()
  })

  it('starts a job, tracks it, and applies a completion snapshot', async () => {
    const { api, stream, synthesis } = await setup()
    vi.mocked(api.post).mockResolvedValue({
      job_id: 'job-1',
      status: 'running',
      phase: 'generate',
      current: 0,
      total: 1,
      completed: 0,
      current_text: '你好',
      rendered_text: '',
      output_dir: '',
      outputs: [],
      error: '',
    })

    await synthesis.start('你好')
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('/api/synthesis', expect.objectContaining({ text: '你好' }))
    expect(synthesis.busy.value).toBe(true)
    expect(vi.mocked(stream.createSynthesisStream)).toHaveBeenCalledTimes(1)

    const options = vi.mocked(stream.createSynthesisStream).mock.calls[0][0]
    options.onEvent(JSON.stringify({
      type: 'snapshot',
      snapshot: {
        job_id: 'job-1',
        status: 'done',
        phase: 'done',
        current: 1,
        total: 1,
        completed: 1,
        current_text: '',
        rendered_text: '',
        output_dir: 'out',
        error: '',
        outputs: [{ text: '你好', url: '/api/files/1', error: '', output_index: 0 }],
      },
    }))
    await flushPromises()

    expect(synthesis.busy.value).toBe(false)
    expect(synthesis.outputs.value).toHaveLength(1)
    expect(synthesis.outputs.value[0].url).toBe('/api/files/1')
    expect(toastSuccess).toHaveBeenCalledWith('完成 1/1 条配音。')
    expect(streamClose).toHaveBeenCalled()
  })

  it('does not echo server-managed voice card fields back to the synthesis API', async () => {
    const { api, synthesis } = await setup()
    vi.mocked(api.post).mockResolvedValue({
      job_id: 'job-2',
      status: 'done',
      phase: 'done',
      current: 1,
      total: 1,
      completed: 1,
      current_text: '',
      rendered_text: '',
      output_dir: 'out',
      error: '',
      outputs: [],
    })

    await synthesis.start('你好')
    await flushPromises()

    const body = vi.mocked(api.post).mock.calls[0][1] as Record<string, unknown>
    expect(body.text).toBe('你好')
    expect(body.ref_audio).toBe('ref.wav')
    expect('voice_card_id' in body).toBe(false)
    expect('voice_card_version' in body).toBe(false)
    expect('style_id' in body).toBe(false)
  })

  it('keeps the line mapping intact when a single line is retried', async () => {
    const { api, synthesis } = await setup()
    // 三行全部跑完，第二行失败（界面因此显示「重试」）。
    vi.mocked(api.post).mockResolvedValueOnce({
      job_id: 'job-all',
      status: 'done',
      phase: 'done',
      current: 3,
      total: 3,
      completed: 3,
      current_text: '',
      rendered_text: '',
      output_dir: 'out',
      error: '',
      outputs: [
        { text: '第一句', url: '/api/files/a', error: '', output_index: 0 },
        { text: '第二句', url: '', error: '合成失败', output_index: 1 },
        { text: '第三句', url: '/api/files/c', error: '', output_index: 2 },
      ],
    })
    await synthesis.start('第一句\n第二句\n第三句')
    await flushPromises()
    expect(synthesis.generatedLines.value).toEqual(['第一句', '第二句', '第三句'])

    // 只重试第二行。`retry` 只替换 outputs 的一个槽位，所以映射用的键列表
    // 必须保持三行；一旦被缩成一行，其余行全部失配、被重试那行还会解析到
    // outputs[0]（第一句的音频）——这就是回归测试要钉住的行为。
    vi.mocked(api.post).mockResolvedValueOnce({
      job_id: 'job-retry',
      status: 'done',
      phase: 'done',
      current: 1,
      total: 1,
      completed: 1,
      current_text: '',
      rendered_text: '',
      output_dir: 'out',
      error: '',
      outputs: [{ text: '第二句', url: '/api/files/b', error: '', output_index: 0 }],
    })
    await synthesis.retry(1)
    await flushPromises()

    expect(synthesis.generatedLines.value).toEqual(['第一句', '第二句', '第三句'])
    expect(synthesis.outputs.value.map((item) => item.url)).toEqual([
      '/api/files/a',
      '/api/files/b',
      '/api/files/c',
    ])
    expect(synthesis.retryingIndex.value).toBeNull()
  })

  it('rejects empty text without calling the backend', async () => {
    const { api, synthesis } = await setup()
    await synthesis.start('   ')
    expect(api.post).not.toHaveBeenCalled()
    expect(toastError).toHaveBeenCalledWith('请先输入台词。')
  })

  it('recovers when the submit request fails', async () => {
    const { api, synthesis } = await setup()
    vi.mocked(api.post).mockRejectedValue(new Error('显存不足'))

    await synthesis.start('测试')
    await flushPromises()

    expect(synthesis.busy.value).toBe(false)
    expect(toastError).toHaveBeenCalledWith('显存不足')
  })

  it('clears results and stops tracking', async () => {
    const { synthesis } = await setup()
    synthesis.outputs.value = [{
      text: '旧结果',
      rendered_text: '',
      url: '/api/files/old',
      error: '',
      output_index: 0,
      semantic_token_count: 0,
      semantic_frame_rate: 0,
    }]
    synthesis.clearResults()
    expect(synthesis.outputs.value).toHaveLength(0)
    expect(synthesis.job.value).toBeNull()
  })
})
