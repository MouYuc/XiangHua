import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

const toastError = vi.fn()

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), put: vi.fn() },
}))

vi.mock('./useToast', () => ({
  useToast: () => ({ success: vi.fn(), error: toastError, info: vi.fn() }),
}))

const configPayload = {
  gpt_sovits_root: 'G:\\GPT-SoVITS',
  gpt_model: 's1.ckpt',
  sovits_model: 's2.pth',
  ref_audio: 'ref.wav',
  ref_text: '参考文本',
  text_lang: 'zh',
  prompt_lang: 'zh',
  top_k: 30,
  top_p: 0.88,
  temperature: 0.95,
  sample_steps: 64,
  speed: 1,
  pause_second: 0.15,
  mixed_eos_floor: null,
  mixed_token_cap: null,
  auto_lightmid: false,
  accent_level: 'off',
  theme: 'focus',
  orb_synthesis_token_color: '#fff',
  orb_settled_color: '#fff',
  orb_playback_token_color: '#fff',
}

async function setup() {
  vi.resetModules()
  const { api } = await import('@/lib/api')
  const { useConfig } = await import('./useConfig')
  const store = useConfig()
  return { api, store }
}

describe('useConfig', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    toastError.mockClear()
  })

  it('loads config and normalizes legacy theme/accent values without an immediate save', async () => {
    const { api, store } = await setup()
    vi.mocked(api.get).mockResolvedValue(configPayload)
    await store.loadConfig()

    expect(store.loaded.value).toBe(true)
    expect(store.config.theme).toBe('workbench')
    expect(store.config.ref_audio).toBe('ref.wav')

    await vi.advanceTimersByTimeAsync(800)
    expect(api.put).not.toHaveBeenCalled()
  })

  it('debounces edits and persists once with the new value', async () => {
    const { api, store } = await setup()
    vi.mocked(api.get).mockResolvedValue(configPayload)
    vi.mocked(api.put).mockResolvedValue(configPayload)
    await store.loadConfig()

    store.config.speed = 1.2
    await nextTick()
    await vi.advanceTimersByTimeAsync(400)
    expect(api.put).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(400)
    expect(api.put).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.put).mock.calls[0][1]).toMatchObject({ speed: 1.2 })
    expect(store.savedAt.value).not.toBeNull()
  })

  it('reports load failures through the toast channel', async () => {
    const { api, store } = await setup()
    vi.mocked(api.get).mockRejectedValue(new Error('连不上后端'))
    await store.loadConfig()

    expect(store.loaded.value).toBe(false)
    expect(store.loadError.value).toBe('连不上后端')
    expect(toastError).toHaveBeenCalledWith('读取配置失败：连不上后端')
  })
})
