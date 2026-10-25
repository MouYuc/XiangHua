import { beforeEach, describe, expect, it, vi } from 'vitest'

describe('useToast', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.useFakeTimers()
  })

  it('auto-dismisses success messages after a short delay', async () => {
    const { useToast } = await import('./useToast')
    const toast = useToast()
    toast.success('完成 2 条')
    expect(toast.toasts.value).toHaveLength(1)
    expect(toast.toasts.value[0].tone).toBe('ok')

    vi.advanceTimersByTime(4300)
    expect(toast.toasts.value).toHaveLength(0)
  })

  it('keeps errors visible longer and ignores empty messages', async () => {
    const { useToast } = await import('./useToast')
    const toast = useToast()
    toast.error('失败')
    toast.info('   ')
    expect(toast.toasts.value).toHaveLength(1)
    vi.advanceTimersByTime(5000)
    expect(toast.toasts.value).toHaveLength(1)
    vi.advanceTimersByTime(3200)
    expect(toast.toasts.value).toHaveLength(0)
  })

  it('caps the stack at four messages', async () => {
    const { useToast } = await import('./useToast')
    const toast = useToast()
    for (let index = 0; index < 6; index += 1) toast.info(`第 ${index} 条`)
    expect(toast.toasts.value).toHaveLength(4)
    expect(toast.toasts.value[3].message).toBe('第 5 条')
  })

  it('dismisses immediately by id', async () => {
    const { useToast } = await import('./useToast')
    const toast = useToast()
    toast.info('a')
    const id = toast.toasts.value[0].id
    toast.dismiss(id)
    expect(toast.toasts.value).toHaveLength(0)
  })
})
