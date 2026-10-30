import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'

const toastSuccess = vi.fn()
const toastError = vi.fn()
const streamClose = vi.fn()

vi.mock('@/lib/api', () => ({
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
